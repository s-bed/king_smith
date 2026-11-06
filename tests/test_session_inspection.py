"""Check quality explanations and measurements decoded from archived FIT bytes."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

from garmin_fit_sdk import Encoder, Profile


_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/king_smith"


def load_module(name):
    """Load a pure integration module without starting Home Assistant."""
    spec = importlib.util.spec_from_file_location(name, _COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inspection = load_module("session_inspection")
encode_session = load_module("fit_export").encode_session


class SessionInspectionTests(unittest.TestCase):
    """Keep decoded file information separate from local recording assertions."""

    def setUp(self):
        """Create synthetic walking data with a pause and an odd step total."""
        self.start = datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp()
        self.session = {
            "session_id": "4c35b010-8ed8-47c5-ad1b-331e69129208",
            "start_time": self.start, "end_time": self.start + 180,
            "elapsed_seconds": 180, "moving_seconds": 120,
            "distance_m": 100, "steps": 301, "partial": False,
            "quality_flags": [],
            "samples": [
                {"timestamp": self.start, "distance_m": 0, "speed_mps": 0.75, "cadence_spm": 101},
                {"timestamp": self.start + 60, "distance_m": 50, "speed_mps": 0, "cadence_spm": 0},
                {"timestamp": self.start + 120, "distance_m": 50, "speed_mps": 0.75, "cadence_spm": 101},
                {"timestamp": self.start + 180, "distance_m": 100, "speed_mps": 0, "cadence_spm": 0},
            ],
            "timer_events": [
                {"timestamp": self.start, "event": "start"},
                {"timestamp": self.start + 60, "event": "stop"},
                {"timestamp": self.start + 120, "event": "start"},
                {"timestamp": self.start + 180, "event": "stop"},
            ],
        }

    def test_decodes_original_fit_measurements_and_fractional_steps(self):
        """Report actual file values, retaining FIT precision and pause duration."""
        contents = encode_session(self.session, "America/Toronto")
        result = inspection.inspect_fit(contents)
        self.assertTrue(result["integrity_ok"])
        self.assertEqual(result["distance_m"], 100)
        self.assertEqual(result["timer_seconds"], 120)
        self.assertEqual(result["elapsed_seconds"], 180)
        self.assertEqual(result["steps"], 301)
        self.assertEqual(result["max_cadence_spm"], 101)
        self.assertAlmostEqual(result["avg_cadence_spm"], 150.5)
        self.assertEqual(result["max_speed_mps"], 0.75)
        self.assertAlmostEqual(result["avg_speed_mps"], 100 / 120, places=3)
        self.assertEqual(result["start_time"], self.start)
        self.assertEqual(result["end_time"], self.start + 180)
        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["lap_count"], 1)
        self.assertEqual(result["sport"], "walking")
        self.assertEqual(result["sub_sport"], "indoor_walking")

    def test_corrupted_or_truncated_file_fails_integrity_validation(self):
        """A regenerated or partial substitute cannot hide damaged original bytes."""
        contents = encode_session(self.session, "America/Toronto")
        corrupted = bytearray(contents)
        corrupted[len(corrupted) // 2] ^= 0x10
        for candidate in (b"", b"not a FIT", contents[:-1], bytes(corrupted)):
            with self.subTest(size=len(candidate)), self.assertRaises(ValueError):
                inspection.inspect_fit(candidate)

    def test_small_odd_step_total_remains_exact_in_the_fit_file(self):
        """An odd total keeps its half stride even if a consumer displays whole strides."""
        session = deepcopy(self.session)
        session["steps"] = 39
        encoded = encode_session(session, "America/Toronto")
        self.assertEqual(inspection.inspect_fit(encoded)["steps"], 39)

    def test_missing_measurements_are_null_not_invented_zeroes(self):
        """FIT files with absent optional metrics do not claim recorded steps or speed."""
        encoder = Encoder()
        encoder.write_mesg({
            "mesg_num": Profile["mesg_num"]["FILE_ID"],
            "type": "activity", "manufacturer": "development",
        })
        encoder.write_mesg({
            "mesg_num": Profile["mesg_num"]["SESSION"], "sport": "walking",
            "total_elapsed_time": 10, "total_timer_time": 10,
        })
        result = inspection.inspect_fit(bytes(encoder.close()))
        for name in ("steps", "distance_m", "avg_cadence_spm", "max_cadence_spm", "avg_speed_mps", "max_speed_mps", "start_time", "end_time"):
            self.assertIsNone(result[name], name)
        self.assertEqual(result["record_count"], 0)

    def test_quality_reasons_distinguish_sample_gaps_from_missing_totals(self):
        """A coverage warning must not claim every metric or cumulative total was lost."""
        session = deepcopy(self.session)
        session.update(partial=True, quality_flags=["data_gap", "started_mid_walk"])
        result = inspection.quality_details(session)
        self.assertEqual([item["code"] for item in result], ["data_gap", "started_mid_walk"])
        self.assertIn("totals may still be complete", result[0]["description"])
        self.assertIn("before the first recorded sample", result[1]["description"])
        self.assertTrue(all(item["severity"] == "warning" for item in result))
        self.assertEqual(self.session["quality_flags"], [])

    def test_reset_is_informational_for_a_complete_recording(self):
        """A correctly accumulated normal reset is not itself missing data."""
        result = inspection.quality_details({"quality_flags": ["counter_reset"], "partial": False})
        self.assertEqual(result[0]["severity"], "info")
        result = inspection.quality_details({"quality_flags": ["counter_reset"], "partial": True})
        self.assertEqual(result[0]["severity"], "info")
        self.assertIn("earlier recorder version", result[0]["description"])

    def test_legacy_reset_only_partial_is_reinterpreted_without_mutation(self):
        """Only the old broad reset classification is relaxed, retaining raw provenance."""
        session = {"quality_flags": ["counter_reset"], "partial": True}
        self.assertFalse(inspection.recording_is_partial(session))
        inspection.quality_details(session)
        self.assertEqual(session, {"quality_flags": ["counter_reset"], "partial": True})
        for flags in (["counter_reset", "counter_reset_while_moving"], ["counter_reset", "data_gap"], ["started_mid_walk"], ["counter_jump"], ["connection_lost"], ["future_reason"], []):
            with self.subTest(flags=flags):
                self.assertTrue(inspection.recording_is_partial({"quality_flags": flags, "partial": True}))

    def test_moving_reset_retains_warning_and_incomplete_classification(self):
        """A specific reset during motion never disappears behind the broad reset note."""
        session = {"quality_flags": ["counter_reset", "counter_reset_while_moving"], "partial": True}
        self.assertTrue(inspection.recording_is_partial(session))
        result = inspection.quality_details(session)
        self.assertEqual([item["severity"] for item in result], ["info", "warning"])
        self.assertIn("cannot be recovered", result[1]["description"])

    def test_unknown_and_unspecified_reasons_remain_visible(self):
        """Unknown future flags and old partial records retain honest explanations."""
        result = inspection.quality_details({"quality_flags": ["future_reason", "future_reason"]})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "future_reason")
        self.assertIn("future_reason", result[0]["description"])
        result = inspection.quality_details({"partial": True, "quality_flags": []})
        self.assertEqual(result[0]["code"], "partial_unspecified")
        self.assertIn("cannot be identified", result[0]["description"])
        self.assertEqual(inspection.quality_details(self.session), [])


if __name__ == "__main__":
    unittest.main()
