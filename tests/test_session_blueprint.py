"""Validate the review blueprint's routing and restart-safe notification logic."""

import ast
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from jinja2.nativetypes import NativeEnvironment
import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = (
    ROOT / "blueprints/automation/king_smith/review_walkingpad_session.yaml"
)


@dataclass(frozen=True)
class BlueprintInput:
    """Represent a blueprint input without importing Home Assistant."""

    name: str


class BlueprintLoader(yaml.SafeLoader):
    """Read YAML while preserving Home Assistant input placeholders."""


def load_input(loader, node):
    """Load Home Assistant's input tag for isolated blueprint tests."""
    return BlueprintInput(loader.construct_scalar(node))


BlueprintLoader.add_constructor("!input", load_input)


class SessionBlueprintTests(unittest.TestCase):
    """Exercise the actual blueprint templates with representative event data."""

    def setUp(self):
        """Read the blueprint and prepare Companion App registry identifiers."""
        self.blueprint = yaml.load(
            BLUEPRINT_PATH.read_text(encoding="utf-8"), Loader=BlueprintLoader
        )
        self.inputs = {
            "walkingpad_device": "a" * 32,
            "notification_device": "b" * 32,
            "garmin_entity": "sensor.garmin_steps",
            "minimum_moving_seconds": 60,
        }
        self.environment = NativeEnvironment()
        self.environment.globals["device_attr"] = (
            lambda device, attribute: {("mobile_app", "phone-app-identifier")}
            if device == self.inputs["notification_device"]
            and attribute == "identifiers"
            else None
        )
        self.session_id = "e47c8b25-370a-4957-88b1-964a71139879"

    def render(self, value, variables):
        """Render one scalar template with Home Assistant's trimmed text behavior."""
        if isinstance(value, BlueprintInput):
            return self.inputs[value.name]
        if isinstance(value, str):
            result = self.environment.from_string(value).render(**variables)
            if isinstance(result, str):
                result = result.strip()
                try:
                    return ast.literal_eval(result)
                except (ValueError, SyntaxError):
                    return result
            return result
        return value

    def variables(self, trigger=None):
        """Render the ordered global variable definitions."""
        variables = {} if trigger is None else {"trigger": trigger}
        for name, value in self.blueprint["variables"].items():
            variables[name] = self.render(value, variables)
        return variables

    def mobile_variables(self, operation="upload", source=None, pad=None):
        """Build an event for a notification action on a particular phone and pad."""
        data = {
            "action": f"KING_SMITH|{pad or self.inputs['walkingpad_device']}|{self.session_id}|{operation}",
            "device_id": source or self.inputs["notification_device"],
        }
        trigger = SimpleNamespace(
            id="mobile_action", event=SimpleNamespace(data=data)
        )
        return self.variables(trigger)

    def session(self, status="awaiting_review", moving_seconds=60):
        """Return the public summary fields consumed by the blueprint."""
        return {
            "session_id": self.session_id,
            "name": "Desk treadmill",
            "device_id": self.inputs["walkingpad_device"],
            "moving_seconds": moving_seconds,
            "distance_m": 100,
            "steps": 301,
            "partial": False,
            "status": status,
        }

    def notification_branches(self):
        """Locate the actual resolved/unresolved notification branches."""
        return self.blueprint["action"][-1]["repeat"]["sequence"][-1]["choose"]

    def test_selectors_and_restart_triggers(self):
        """Expose explicit device/account selectors and persistent recovery triggers."""
        inputs = self.blueprint["blueprint"]["input"]
        self.assertEqual(
            inputs["walkingpad_device"]["selector"]["device"]["integration"],
            "king_smith",
        )
        self.assertEqual(
            inputs["notification_device"]["selector"]["device"]["integration"],
            "mobile_app",
        )
        self.assertEqual(
            inputs["garmin_entity"]["selector"]["entity"]["integration"],
            "garmin_connect",
        )
        self.assertEqual(inputs["minimum_moving_seconds"]["default"], 60)
        triggers = self.blueprint["trigger"]
        self.assertEqual(
            {trigger["id"] for trigger in triggers},
            {"session_ready", "session_updated", "recorder_ready", "startup", "mobile_action"},
        )
        for trigger in triggers:
            if trigger["id"] in {"session_ready", "session_updated"}:
                self.assertEqual(trigger["event_data"]["device_id"], BlueprintInput("walkingpad_device"))
        self.assertNotIn("wait_for_trigger", BLUEPRINT_PATH.read_text())
        self.assertEqual(self.blueprint["mode"], "queued")

    def test_mobile_actions_require_selected_pad_and_phone(self):
        """Notification actions from another pad or phone are ignored."""
        condition = self.blueprint["condition"][0]["value_template"]
        self.assertTrue(self.render(condition, self.mobile_variables()))
        self.assertFalse(self.render(condition, self.mobile_variables(pad="c" * 32)))
        self.assertFalse(self.render(condition, self.mobile_variables(source="another-phone")))
        self.assertFalse(self.render(condition, self.mobile_variables(operation="unknown")))

    def test_ios_identifier_is_resolved_from_selected_mobile_device(self):
        """The iOS source identifier is checked against the device registry."""
        trigger = SimpleNamespace(
            id="mobile_action",
            event=SimpleNamespace(data={
                "action": f"KING_SMITH|{self.inputs['walkingpad_device']}|{self.session_id}|discard",
                "sourceDeviceID": "phone-app-identifier",
            }),
        )
        condition = self.blueprint["condition"][0]["value_template"]
        self.assertTrue(self.render(condition, self.variables(trigger)))
        trigger.event.data["sourceDeviceID"] = "another-phone"
        self.assertFalse(self.render(condition, self.variables(trigger)))

    def test_session_identity_is_stable_across_restarts(self):
        """UUID-based tags/actions remain usable outside the original automation run."""
        variables = self.mobile_variables()
        self.assertEqual(variables["requested_session_id"], self.session_id)
        variables["session"] = self.session()
        sequence = self.blueprint["action"][-1]["repeat"]["sequence"]
        tag = sequence[0]["variables"]["notification_tag"]
        self.assertEqual(
            self.render(tag, variables),
            f"king_smith_{self.inputs['walkingpad_device']}_{self.session_id}",
        )
        self.assertNotIn("context.id", BLUEPRINT_PATH.read_text())

    def test_notification_threshold_uses_moving_time(self):
        """Exactly 60 moving seconds qualifies; paused elapsed time cannot qualify."""
        condition = self.notification_branches()[1]["conditions"]
        variables = self.variables()
        variables["session"] = self.session(moving_seconds=59)
        variables["session"]["elapsed_seconds"] = 300
        self.assertFalse(self.render(condition, variables))
        variables["session"]["moving_seconds"] = 60
        self.assertTrue(self.render(condition, variables))
        variables["minimum_seconds"] = 30
        variables["session"]["moving_seconds"] = 30
        self.assertTrue(self.render(condition, variables))

    def test_notification_explains_the_saved_quality_reason(self):
        """A counter-reset note replaces the old unspecified missing-data warning."""
        message = self.notification_branches()[1]["sequence"][-1]["message"]
        variables = self.variables()
        variables["session"] = self.session()
        variables["session"].update(partial=True, quality_notes=["Device counters restarted; review the recorded totals."])
        rendered = self.render(message, variables)
        self.assertIn("Device counters restarted", rendered)
        self.assertNotIn("some walking data was not captured", rendered)

    def test_ambiguous_upload_requires_explicit_retry_action(self):
        """A stale Upload tap cannot retry an uncertain network request."""
        action = self.blueprint["action"][2]["choose"][0]["sequence"][0]["choose"][0]
        condition = action["conditions"]
        variables = self.mobile_variables()
        variables["selected_session"] = self.session("needs_review")
        self.assertFalse(self.render(condition, variables))
        variables = self.mobile_variables(operation="retry")
        variables["selected_session"] = self.session("needs_review")
        self.assertTrue(self.render(condition, variables))
        variables["selected_session"] = self.session("sent")
        self.assertFalse(self.render(condition, variables))

    def test_notification_buttons_follow_durable_status(self):
        """Uncertain uploads request a Garmin check; active uploads cannot be discarded."""
        template = self.notification_branches()[1]["sequence"][0]["variables"]["notification_actions"]
        variables = self.variables()
        variables["session_action_prefix"] = f"KING_SMITH|pad|{self.session_id}|"
        variables["session"] = self.session("needs_review")
        buttons = self.render(template, variables)
        self.assertEqual(buttons[0]["action"], variables["session_action_prefix"] + "retry")
        self.assertIn("Checked Garmin", buttons[0]["title"])
        variables["session"] = self.session("uploading")
        self.assertEqual(self.render(template, variables), [])
        variables["session"] = self.session()
        self.assertEqual([button["title"] for button in self.render(template, variables)], ["Upload", "Discard"])

    def test_resolved_notifications_clear_and_do_not_replay(self):
        """Completed review notifications clear their tag and stay out of startup replay."""
        branch = self.notification_branches()[0]
        variables = self.variables()
        variables["session"] = self.session("discarded")
        self.assertTrue(self.render(branch["conditions"], variables))
        self.assertEqual(branch["sequence"][0]["message"], "clear_notification")
        template = self.blueprint["action"][-2]["variables"]["notification_sessions"]
        variables["saved_sessions"] = [self.session(), self.session("sent"), self.session("discarded")]
        self.assertEqual(len(self.render(template, variables)), 1)

    def test_stale_event_payload_uses_the_current_saved_status(self):
        """A delayed ready/update event cannot recreate a prompt after upload."""
        trigger = SimpleNamespace(
            id="session_ready",
            event=SimpleNamespace(data=self.session("awaiting_review")),
        )
        variables = self.variables(trigger)
        variables["saved_sessions"] = [self.session("sent")]
        template = self.blueprint["action"][-2]["variables"]["notification_sessions"]
        candidates = self.render(template, variables)
        self.assertEqual(len(candidates), 1)
        variables["session"] = candidates[0]
        self.assertTrue(self.render(self.notification_branches()[0]["conditions"], variables))

    def test_service_documentation_matches_public_contract(self):
        """Expose identifiers rather than arbitrary paths for review actions."""
        services = yaml.safe_load(
            (ROOT / "custom_components/king_smith/services.yaml").read_text()
        )
        self.assertEqual(set(services), {"list_sessions", "upload_session", "discard_session"})
        upload = services["upload_session"]["fields"]
        self.assertEqual(set(upload), {"device_id", "session_id", "garmin_entity_id", "retry"})
        self.assertFalse(upload["retry"]["default"])
        self.assertIn("keep the recording locally", services["discard_session"]["description"])

    @unittest.skipUnless(
        importlib.util.find_spec("homeassistant") and importlib.util.find_spec("probatio"),
        "Real schema validation runs against current Home Assistant in the Linux CI job",
    )
    def test_home_assistant_schema(self):
        """Use actual HA blueprint/script schemas when running Linux HA CI."""
        async def validate_blueprint():
            """Validate templates in the event-loop context required by HA."""
            from homeassistant.core import HomeAssistant
            from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
            from homeassistant.helpers import config_validation as cv
            from homeassistant.util import yaml as yaml_util

            with tempfile.TemporaryDirectory() as config_directory:
                hass = HomeAssistant(config_directory)
                try:
                    data = yaml_util.load_yaml(str(BLUEPRINT_PATH))
                    BLUEPRINT_SCHEMA(data)
                    substituted = yaml_util.substitute(deepcopy(data), self.inputs)
                    cv.TRIGGER_SCHEMA(substituted["trigger"])
                    cv.SCRIPT_SCHEMA(substituted["action"])
                finally:
                    await hass.async_stop(force=True)

        asyncio.run(validate_blueprint())


if __name__ == "__main__":
    unittest.main()
