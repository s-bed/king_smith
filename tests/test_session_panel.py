"""Validate the native session panel against Home Assistant's WebSocket API."""

import base64
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


HAS_CURRENT_HA = (
    importlib.util.find_spec("homeassistant") is not None
    and importlib.util.find_spec("probatio") is not None
)
SESSION_ID = "e47c8b25-370a-4957-88b1-964a71139879"


class PanelConnection:
    """Capture responses while retaining the real command permission wrappers."""

    def __init__(self, user):
        """Create a connection for an authenticated administrator or denied user."""
        self.user = user
        self.results = []
        self.errors = []

    def send_result(self, message_id, result=None):
        """Collect an isolated copy of a successful command result."""
        self.results.append((message_id, deepcopy(result)))

    def send_error(self, message_id, code, message, **kwargs):
        """Collect expected API errors without logging activity contents."""
        self.errors.append((message_id, code, message))

    def async_handle_exception(self, message, error):
        """Handle asynchronous wrapper exceptions as a real connection would."""
        self.send_error(message["id"], "exception", str(error))


@unittest.skipUnless(HAS_CURRENT_HA, "Requires the current-HA Linux CI environment")
class SessionPanelTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real HA command schemas, permissions and panel registration."""

    async def asyncSetUp(self):
        """Prepare a real HA instance with fake I/O and archive reads."""
        from homeassistant.components import frontend, panel_custom, websocket_api
        from homeassistant.config_entries import ConfigEntries
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import device_registry as dr, entity_registry as er

        from custom_components.king_smith import session_panel

        self.temporary = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temporary.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        # A bare HomeAssistant has neither config entries nor loaded registries.
        # Startup explicitly sets up the device registry before loading it;
        # entity loading then waits for that device registry to be ready.
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.hass.data["king_smith"] = {}
        self.hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
        self.summary = {
            "session_id": SESSION_ID, "device_id": "a" * 32, "entry_id": "entry_one",
            "name": "Desk treadmill", "status": "sent", "moving_seconds": 180,
            "elapsed_seconds": 210, "distance_m": 100, "steps": 301,
            "partial": True, "quality_flags": ["started_mid_walk"],
        }
        self.detail = {
            "session": self.summary,
            "recording": {"version": 1, **self.summary, "samples": []},
        }
        self.manager = SimpleNamespace(
            list_devices=Mock(return_value=[{"device_id": "a" * 32, "name": "Desk treadmill"}]),
            list_all_sessions=Mock(return_value=[self.summary]),
            async_session_detail=AsyncMock(return_value=self.detail),
            async_session_download=AsyncMock(return_value={
                "filename": f"{SESSION_ID}.fit", "mime_type": "application/octet-stream",
                "content": base64.b64encode(b"FIT test bytes").decode(),
            }),
            account_ready=Mock(return_value=False),
            last_error=None,
        )
        self.panel_module = session_panel
        self.websocket_api = websocket_api
        self.register_panel = AsyncMock()
        self.panel_exists = Mock(return_value=False)
        self.patches = [
            patch.object(panel_custom, "async_register_panel", self.register_panel),
            patch.object(frontend, "async_panel_exists", self.panel_exists),
        ]
        for item in self.patches:
            item.start()
        await self.panel_module.async_setup_panel(self.hass, self.manager)

    async def asyncTearDown(self):
        """Release the real HA instance without binding an HTTP server."""
        for item in reversed(self.patches):
            item.stop()
        await self.hass.async_stop(force=True)
        self.temporary.cleanup()

    def command(self, kind):
        """Read the actual registered handler and its HA-expanded schema."""
        return self.hass.data[self.websocket_api.DOMAIN][f"king_smith/sessions/{kind}"]

    async def dispatch(self, kind, user=None, **fields):
        """Dispatch a valid command through HA's actual decorator chain."""
        from homeassistant.exceptions import Unauthorized

        handler, schema = self.command(kind)
        message = {"id": 1, "type": f"king_smith/sessions/{kind}", **fields}
        message = schema(message) if schema else message
        connection = PanelConnection(user if user is not None else SimpleNamespace(is_admin=True))
        try:
            handler(self.hass, connection, message)
        except Unauthorized as error:
            connection.async_handle_exception(message, error)
        await self.hass.async_block_till_done()
        return connection

    async def test_registers_only_bundled_javascript_and_an_admin_panel(self):
        """Static serving never points to private recordings or the HA config dir."""
        configurations = self.hass.http.async_register_static_paths.await_args.args[0]
        self.assertEqual(len(configurations), 1)
        static = configurations[0]
        self.assertEqual(static.url_path, "/king_smith_static/king-smith-panel.js")
        self.assertEqual(Path(static.path).name, "king-smith-panel.js")
        self.assertEqual(Path(static.path).parent.name, "frontend")
        self.assertFalse(static.cache_headers)
        options = self.register_panel.await_args.kwargs
        self.assertEqual(options["frontend_url_path"], "walkingpad-sessions")
        self.assertEqual(options["webcomponent_name"], "king-smith-panel")
        self.assertTrue(options["require_admin"])
        self.assertFalse(options.get("embed_iframe", False))
        self.assertFalse(options.get("trust_external", False))
        self.assertTrue(options["module_url"].startswith(static.url_path + "?v="))

    async def test_repeated_setup_does_not_duplicate_panel_or_commands(self):
        """Reloading the integration keeps one sidebar panel and command set."""
        commands = dict(self.hass.data[self.websocket_api.DOMAIN])
        await self.panel_module.async_setup_panel(self.hass, self.manager)
        self.assertEqual(self.register_panel.await_count, 1)
        self.assertEqual(self.hass.http.async_register_static_paths.await_count, 1)
        self.assertEqual(self.hass.data[self.websocket_api.DOMAIN], commands)

    async def test_legacy_http_static_registration(self):
        """The old HA HTTP interface receives only the same packaged JS file."""
        self.hass.data["king_smith"].pop("session_panel_registered")
        self.hass.http = SimpleNamespace(register_static_path=Mock())
        await self.panel_module.async_setup_panel(self.hass, self.manager)
        args = self.hass.http.register_static_path.call_args.args
        kwargs = self.hass.http.register_static_path.call_args.kwargs
        self.assertEqual(args[0], "/king_smith_static/king-smith-panel.js")
        self.assertEqual(Path(args[1]).name, "king-smith-panel.js")
        self.assertFalse(args[2] if len(args) > 2 else kwargs["cache_headers"])

    async def test_administrator_can_list_and_inspect_saved_sessions(self):
        """The browser receives saved summaries and source data through WS only."""
        connection = await self.dispatch("list")
        self.assertFalse(connection.errors)
        self.assertEqual(connection.results[0][1]["sessions"], [self.summary])
        self.assertEqual(connection.results[0][1]["devices"], self.manager.list_devices.return_value)
        self.assertEqual(connection.results[0][1]["garmin_accounts"], [])
        self.assertFalse(connection.results[0][1]["garmin_upload_available"])
        connection = await self.dispatch("get", session_id=SESSION_ID)
        self.assertEqual(connection.results, [(1, self.detail)])
        self.manager.async_session_detail.assert_awaited_once_with(SESSION_ID)

    async def test_account_list_groups_only_enabled_garmin_sensors_and_prefers_available_alias(self):
        """Route each account through its own enabled sensor, retaining saved aliases."""
        from homeassistant.helpers import entity_registry as er

        def entity(entity_id, entry_id="account_one", platform="garmin_connect", disabled_by=None):
            return SimpleNamespace(
                entity_id=entity_id, config_entry_id=entry_id,
                platform=platform, disabled_by=disabled_by,
            )

        entities = [
            entity("sensor.garmin_a_unknown"), entity("sensor.garmin_z_steps"),
            entity("sensor.garmin_disabled", disabled_by="user"),
            entity("sensor.garmin_offline", "account_two"),
            entity("sensor.unrelated", platform="other_integration"),
            entity("button.garmin_sync"), entity("sensor.orphan", "missing"),
            entity("sensor.no_account", None),
        ]
        registry = SimpleNamespace(entities={item.entity_id: item for item in entities})
        entries = {
            "account_one": SimpleNamespace(domain="garmin_connect", title="First account"),
            "account_two": SimpleNamespace(domain="garmin_connect", title="Second account"),
        }
        self.hass.states.async_set("sensor.garmin_a_unknown", "unknown")
        self.hass.states.async_set("sensor.garmin_z_steps", "100")
        self.hass.states.async_set("sensor.garmin_offline", "unavailable")
        self.manager.account_ready.side_effect = lambda entity_id: entity_id != "sensor.garmin_offline"
        with (
            patch.object(er, "async_get", return_value=registry),
            patch.object(self.hass.config_entries, "async_get_entry", side_effect=entries.get),
        ):
            connection = await self.dispatch("list")
        self.assertFalse(connection.errors)
        self.assertEqual(connection.results[0][1]["garmin_accounts"], [
            {
                "entity_id": "sensor.garmin_z_steps", "name": "First account", "available": True,
                "entity_ids": ["sensor.garmin_a_unknown", "sensor.garmin_z_steps"],
            },
            {
                "entity_id": "sensor.garmin_offline", "name": "Second account", "available": False,
                "entity_ids": ["sensor.garmin_offline"],
            },
        ])

    async def test_loaded_account_state_and_public_upload_service_are_reported_independently(self):
        """A sensor value alone does not imply an unloaded account can dispatch uploads."""
        from homeassistant.core import callback
        from homeassistant.helpers import entity_registry as er

        sensor = SimpleNamespace(
            entity_id="sensor.garmin_steps", config_entry_id="account_one",
            platform="garmin_connect", disabled_by=None,
        )
        self.hass.states.async_set(sensor.entity_id, "100", {"friendly_name": "Garmin steps"})
        entry = SimpleNamespace(domain="garmin_connect", title="")

        @callback
        def upload(call):
            pass

        self.hass.services.async_register("garmin_connect", "upload_activity", upload)
        with (
            patch.object(er, "async_get", return_value=SimpleNamespace(entities={sensor.entity_id: sensor})),
            patch.object(self.hass.config_entries, "async_get_entry", return_value=entry),
        ):
            connection = await self.dispatch("list")
        payload = connection.results[0][1]
        self.assertTrue(payload["garmin_upload_available"])
        self.assertEqual(payload["garmin_accounts"][0]["name"], "Garmin steps")
        self.assertFalse(payload["garmin_accounts"][0]["available"])

    async def test_fit_and_json_downloads_use_identifiers_and_preserve_bytes(self):
        """Download responses can be decoded into exactly the stored file bytes."""
        for file_format in ("fit", "json"):
            with self.subTest(file_format=file_format):
                connection = await self.dispatch("download", session_id=SESSION_ID, format=file_format)
                self.assertFalse(connection.errors)
                payload = connection.results[0][1]
                self.assertEqual(base64.b64decode(payload["content"]), b"FIT test bytes")
                self.manager.async_session_download.assert_awaited_with(SESSION_ID, file_format)

    async def test_non_admin_or_unauthenticated_users_cannot_read_fitness_data(self):
        """Every command denies access before calling the session manager."""
        for user in (SimpleNamespace(is_admin=False), None):
            for kind, fields in (
                ("list", {}), ("get", {"session_id": SESSION_ID}),
                ("download", {"session_id": SESSION_ID, "format": "fit"}),
            ):
                handler, schema = self.command(kind)
                message = {"id": 1, "type": f"king_smith/sessions/{kind}", **fields}
                message = schema(message) if schema else message
                connection = PanelConnection(user)
                from homeassistant.exceptions import Unauthorized

                try:
                    handler(self.hass, connection, message)
                except Unauthorized as error:
                    connection.async_handle_exception(message, error)
                await self.hass.async_block_till_done()
                self.assertFalse(connection.results)
                self.assertTrue(connection.errors)
        self.manager.list_all_sessions.assert_not_called()
        self.manager.async_session_detail.assert_not_awaited()
        self.manager.async_session_download.assert_not_awaited()

    async def test_unknown_session_errors_do_not_expose_server_paths(self):
        """Missing saved activities return errors rather than an empty download."""
        from custom_components.king_smith.session_manager import SessionError

        self.manager.async_session_detail.side_effect = SessionError("Session not found")
        self.manager.async_session_download.side_effect = SessionError("Session not found")
        for kind, fields in (
            ("get", {"session_id": SESSION_ID}),
            ("download", {"session_id": SESSION_ID, "format": "fit"}),
        ):
            connection = await self.dispatch(kind, **fields)
            self.assertFalse(connection.results)
            self.assertTrue(connection.errors)

    async def test_command_schemas_reject_arbitrary_paths_and_unknown_formats(self):
        """Requests cannot provide a file path or select a different file type."""
        import probatio

        for kind, data in (
            ("get", {"session_id": "../../secrets.yaml"}),
            ("get", {"session_id": SESSION_ID, "file_path": "/config/secrets.yaml"}),
            ("download", {"session_id": SESSION_ID, "format": "yaml"}),
            ("download", {"session_id": SESSION_ID, "format": "fit", "file_path": "../../secrets.yaml"}),
            ("download", {"format": "fit"}),
        ):
            _, schema = self.command(kind)
            with self.subTest(kind=kind, data=data), self.assertRaises(probatio.Invalid):
                schema({"id": 1, "type": f"king_smith/sessions/{kind}", **data})
        self.manager.async_session_download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
