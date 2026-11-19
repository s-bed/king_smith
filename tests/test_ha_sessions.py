"""Run session actions, persistence and device triggers against real HA APIs."""

import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


HAS_HOME_ASSISTANT = (
    importlib.util.find_spec("homeassistant") is not None
    and importlib.util.find_spec("probatio") is not None
)
if HAS_HOME_ASSISTANT:
    import probatio

    from homeassistant.core import HomeAssistant, callback
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.storage import Store

    from custom_components.king_smith import device_trigger
    from custom_components.king_smith.session_manager import SessionManager
    from custom_components.king_smith.session_recorder import StatusSample
    from custom_components.king_smith.session_services import register_session_services


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires the current-HA Linux CI environment")
class HomeAssistantSessionTests(unittest.IsolatedAsyncioTestCase):
    """Use the real service registry, event bus and durable storage writer."""

    async def asyncSetUp(self):
        """Prepare an isolated HA instance without Bluetooth or Garmin hardware."""
        self.temporary = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temporary.name)
        self.hass.config.time_zone = "America/Toronto"
        self.manager = SessionManager(
            self.hass, Store(self.hass, 1, "king_smith_sessions"),
            lambda entity: entity == "sensor.garmin_steps",
        )
        register_session_services(self.hass, self.manager)
        await self.manager.async_initialize()
        self.manager.attach("entry_one", "a" * 32, "Desk treadmill")

    async def test_existing_device_identity_keeps_its_original_address_case(self):
        """A lowercase config entry must not replace old uppercase entity IDs."""
        from custom_components.king_smith import _registered_address

        registry = SimpleNamespace(devices={"device": SimpleNamespace(
            config_entries={"entry_one"},
            identifiers={("king_smith", "AA:BB:CC:DD:EE:FF")},
        )})
        self.assertEqual(
            _registered_address(registry, "entry_one", "aa:bb:cc:dd:ee:ff"),
            "AA:BB:CC:DD:EE:FF",
        )
        self.assertEqual(
            _registered_address(registry, "other_entry", "11:22:33:44:55:66"),
            "11:22:33:44:55:66",
        )

    async def asyncTearDown(self):
        """Flush recorder data and stop HA's background resources."""
        await self.manager.async_shutdown()
        await self.hass.async_stop(force=True)
        self.temporary.cleanup()

    async def record_session(self):
        """Finish a realistic recorded walk using the production state machine."""
        now = time.time() - 500
        for offset, duration, distance, steps, speed in (
            (0, 0, 0, 0, 0), (5, 5, 10, 10, 1),
            (65, 65, 50, 100, 1), (70, 70, 60, 110, 0),
        ):
            await self.manager.async_receive(
                "entry_one", StatusSample(now + offset, duration, distance, steps, speed)
            )
        await self.manager.async_tick()
        await self.hass.async_block_till_done()
        sessions = self.manager.list_sessions("a" * 32)
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    async def test_public_list_discard_and_real_store_roundtrip(self):
        """Register real response actions and retain discarded source recordings."""
        ready_events = []

        @callback
        def on_ready(event):
            ready_events.append(event.data)

        remove_listener = self.hass.bus.async_listen("king_smith_session_ready", on_ready)
        session = await self.record_session()
        self.assertEqual(ready_events[0]["session_id"], session["session_id"])
        result = await self.hass.services.async_call(
            "king_smith", "list_sessions", {"device_id": "a" * 32},
            blocking=True, return_response=True,
        )
        self.assertEqual(result["sessions"][0]["status"], "awaiting_review")
        result = await self.hass.services.async_call(
            "king_smith", "discard_session",
            {"device_id": "a" * 32, "session_id": session["session_id"]},
            blocking=True, return_response=True,
        )
        self.assertEqual(result["session"]["status"], "discarded")
        archive = Path(self.hass.config.path("walkingpad_sessions", "entry_one"))
        self.assertTrue((archive / f"{session['session_id']}.json").is_file())
        self.assertTrue((archive / f"{session['session_id']}.fit").is_file())
        await self.manager.async_shutdown()
        self.manager = SessionManager(
            self.hass, Store(self.hass, 1, "king_smith_sessions"), lambda entity: True
        )
        await self.manager.async_initialize()
        self.assertEqual(self.manager.list_sessions("a" * 32)[0]["status"], "discarded")
        remove_listener()

    def _setup_phone_review(self, options):
        from homeassistant.config_entries import ConfigEntries, ConfigEntryState
        from homeassistant.helpers import device_registry as dr, entity_registry as er

        from custom_components.king_smith.session_notifications import SessionNotifications

        self.hass.config_entries = ConfigEntries(self.hass, {})
        settings = SimpleNamespace(entry_id="entry_one", disabled_by=None, options=options)
        entries = {
            "entry_one": settings,
            "account_one": SimpleNamespace(domain="garmin_connect", title="Garmin A", state=ConfigEntryState.LOADED),
            "phone_entry": SimpleNamespace(domain="mobile_app", data={"user_id": "phone_owner"}),
        }
        sensor = SimpleNamespace(entity_id="sensor.garmin_steps", platform="garmin_connect", disabled_by=None, config_entry_id="account_one")
        devices = {"phone": SimpleNamespace(config_entries={"phone_entry"})}
        notify = AsyncMock()
        self.enterContext(patch.object(self.hass.config_entries, "async_get_entry", side_effect=entries.get))
        self.enterContext(patch.object(er, "async_get", return_value=SimpleNamespace(entities={sensor.entity_id: sensor})))
        self.enterContext(patch.object(dr, "async_get", return_value=SimpleNamespace(async_get=devices.get)))
        self.enterContext(patch(
            "custom_components.king_smith.session_notifications.async_get_device_automation_platform",
            return_value=SimpleNamespace(async_call_action_from_config=notify),
        ))
        notifications = SessionNotifications(self.hass, self.manager)
        notifications.async_listen()
        return settings, notifications, notify

    async def _tap_phone_prompt(self, action, user_id="phone_owner"):
        from homeassistant.core import Context, EventOrigin

        self.hass.bus.async_fire(
            "mobile_app_notification_action", {"action": action}, EventOrigin.remote,
            context=Context(user_id=user_id),
        )
        await self.hass.async_block_till_done()

    async def test_configured_phone_approves_only_its_current_prompt(self):
        """The options form preselects a destination and a phone tap queues it once."""
        from custom_components.king_smith.config_flow import WalkingpadConfigFlow

        settings, notifications, notify = self._setup_phone_review({})
        flow = WalkingpadConfigFlow.async_get_options_flow(settings)
        flow.hass = self.hass
        flow.handler = settings.entry_id
        form = await flow.async_step_init()
        choices = {"garmin_account": "account_one", "notification_device": "phone", "minimum_moving_seconds": 0}
        result = await flow.async_step_init(form["data_schema"](choices))
        self.assertEqual(result["data"], choices)
        settings.options = result["data"]

        uploads = []

        async def upload(call):
            uploads.append(call.data["entity_id"])

        self.hass.services.async_register("garmin_connect", "upload_activity", upload)
        session = await self.record_session()
        message = notify.await_args.args[1]
        self.assertEqual(message["device_id"], "phone")
        self.assertIn("Garmin A", message["message"])
        action = message["data"]["actions"][0]["action"]
        await self._tap_phone_prompt(action, "other_user")
        self.assertEqual(uploads, [])

        settings.options = {**choices, "garmin_account": "different_account"}
        await self._tap_phone_prompt(action)
        self.assertEqual(uploads, [])
        await notifications.async_options_updated(self.hass, settings)
        settings.options = choices
        await notifications.async_options_updated(self.hass, settings)
        await self._tap_phone_prompt(action)
        self.assertEqual(uploads, [])
        action = notify.await_args.args[1]["data"]["actions"][0]["action"]
        await self._tap_phone_prompt(action)
        await self.manager._worker_task
        await self._tap_phone_prompt(action)
        self.assertEqual(uploads, ["sensor.garmin_steps"])
        self.assertEqual(self.manager.get_summary(session["session_id"])["status"], "sent")

    async def test_lowering_review_threshold_can_restore_a_pending_walk(self):
        """A newly eligible saved walk can be declined without removing its FIT file."""
        settings, notifications, notify = self._setup_phone_review({
            "notification_device": "phone", "minimum_moving_seconds": 3600,
        })
        session = await self.record_session()
        notify.assert_not_awaited()
        settings.options["minimum_moving_seconds"] = 0
        await notifications.async_options_updated(self.hass, settings)
        actions = notify.await_args.args[1]["data"]["actions"]
        self.assertEqual([action["title"] for action in actions], ["Keep locally"])
        await self._tap_phone_prompt(actions[0]["action"])
        self.assertEqual(self.manager.get_summary(session["session_id"])["status"], "discarded")
        self.assertEqual(notify.await_args.args[1]["message"], "clear_notification")
        download = await self.manager.async_session_download(session["session_id"], "fit")
        self.assertTrue(download["content"])

    async def test_real_garmin_service_receives_only_a_durably_approved_upload(self):
        """The disk ledger is uploading before the public Garmin service executes."""
        calls = []

        async def upload(call):
            ledger_path = Path(self.hass.config.path(".storage", "king_smith_sessions"))
            persisted = await self.hass.async_add_executor_job(
                lambda: json.loads(ledger_path.read_text(encoding="utf-8"))
            )
            row = next(iter(persisted["data"]["sessions"].values()))
            self.assertEqual(row["status"], "uploading")
            calls.append(dict(call.data))

        self.hass.services.async_register("garmin_connect", "upload_activity", upload)
        session = await self.record_session()
        self.assertEqual(calls, [])
        arguments = {
            "device_id": "a" * 32, "session_id": session["session_id"],
            "garmin_entity_id": "sensor.garmin_steps",
        }
        await self.hass.services.async_call(
            "king_smith", "upload_session", arguments, blocking=True,
        )
        if self.manager._worker_task:
            await self.manager._worker_task
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["entity_id"], "sensor.garmin_steps")
        self.assertTrue(calls[0]["file_path"].endswith(f"{session['session_id']}.fit"))
        self.assertEqual(self.manager.list_sessions("a" * 32)[0]["status"], "sent")
        await self.hass.services.async_call(
            "king_smith", "upload_session", arguments, blocking=True,
        )
        if self.manager._worker_task:
            await self.manager._worker_task
        self.assertEqual(len(calls), 1)

    async def test_retry_action_rejects_a_stale_reviewed_attempt(self):
        """The public schema forwards the web review revision before dispatching."""
        from homeassistant.exceptions import HomeAssistantError

        calls = []

        async def upload(call):
            calls.append(call.data)
            raise HomeAssistantError("Uncertain test upload")

        self.hass.services.async_register("garmin_connect", "upload_activity", upload)
        session = await self.record_session()
        arguments = {
            "device_id": "a" * 32, "session_id": session["session_id"],
            "garmin_entity_id": "sensor.garmin_steps",
        }
        await self.hass.services.async_call("king_smith", "upload_session", arguments, blocking=True)
        await self.manager._worker_task
        self.assertEqual(self.manager.sessions[session["session_id"]]["status"], "needs_review")
        with self.assertRaisesRegex(HomeAssistantError, "attempt changed"):
            await self.hass.services.async_call(
                "king_smith", "upload_session",
                {**arguments, "retry": True, "expected_attempts": 0}, blocking=True,
            )
        self.assertEqual(len(calls), 1)
        await self.hass.services.async_call(
            "king_smith", "upload_session",
            {**arguments, "retry": True, "expected_attempts": 1}, blocking=True,
        )
        await self.manager._worker_task
        self.assertEqual(len(calls), 2)

    async def test_device_trigger_schemas_and_event_filters(self):
        """Both repaired device triggers attach and filter the selected pad."""
        triggers = await device_trigger.async_get_triggers(self.hass, "a" * 32)
        self.assertEqual({trigger["type"] for trigger in triggers}, {"session_start", "session_done"})
        fired = []

        async def action(variables, context):
            fired.append(variables["trigger"]["event"].event_type)

        removers = []
        for trigger in triggers:
            config = device_trigger.TRIGGER_SCHEMA(trigger)
            removers.append(await device_trigger.async_attach_trigger(
                self.hass, config, action,
                {"trigger_data": {"id": trigger["type"], "idx": "0"}, "variables": {}},
            ))
            event = f"king_smith_{trigger['type']}"
            self.hass.bus.async_fire(event, {"device_id": "b" * 32, "type": trigger["type"]})
            self.hass.bus.async_fire(event, {"device_id": "a" * 32, "type": trigger["type"]})
        await self.hass.async_block_till_done()
        self.assertEqual(sorted(fired), ["king_smith_session_done", "king_smith_session_start"])
        for remove in removers:
            remove()

    async def test_malformed_service_data_uses_home_assistant_validation_errors(self):
        """Invalid action payloads fail schema validation before touching sessions."""
        with self.assertRaises(probatio.Invalid):
            await self.hass.services.async_call(
                "king_smith", "upload_session",
                {"device_id": "a" * 32, "session_id": "missing-account"},
                blocking=True,
            )
        with self.assertRaises(probatio.Invalid):
            await self.hass.services.async_call(
                "king_smith", "upload_session",
                {"device_id": "a" * 32, "session_id": "invalid-account",
                 "garmin_entity_id": "not an entity id"},
                blocking=True,
            )
        with self.assertRaises(probatio.Invalid):
            await self.hass.services.async_call(
                "king_smith", "list_sessions",
                {"device_id": "a" * 32, "file_path": "/arbitrary/file.fit"},
                blocking=True, return_response=True,
            )
        self.assertEqual(self.manager.list_sessions("a" * 32), [])

    async def test_manual_setup_form_serializes_for_current_ha_frontend(self):
        """Keep the working manual setup form compatible with HA serialization."""
        from custom_components.king_smith.config_flow import WalkingpadConfigFlow
        from custom_components.king_smith.sensor import RecorderSensor

        flow = WalkingpadConfigFlow()
        flow.hass = self.hass
        flow.handler = "king_smith"
        flow.flow_id = "manual-flow"
        result = await flow.async_step_user()
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "device")
        schema = probatio.to_field_list(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        self.assertEqual({field["name"] for field in schema}, {"name", "mac"})
        json.dumps(schema)
        sensor = RecorderSensor(
            "Desk treadmill", SimpleNamespace(mac="AA:BB:CC:DD:EE:FF"),
            self.manager, "entry_one", True,
        )
        self.assertEqual(sensor.native_value, 0)


if __name__ == "__main__":
    unittest.main()
