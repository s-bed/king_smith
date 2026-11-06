"""Round-trip recorded walks through the official FIT encoder and decoder."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

from garmin_fit_sdk import Decoder, FIT_EPOCH_S, Stream


_SOURCE = Path(__file__).resolve().parents[1] / "custom_components/king_smith/fit_export.py"
_SPEC = importlib.util.spec_from_file_location("fit_export_under_test", _SOURCE)
fit_export = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fit_export)


class FitExportTests(unittest.TestCase):
    """Validate units, message structure, pause boundaries and time zones."""

    def setUp(self):
        """Build a three-minute walk containing a one-minute pause."""
        self.start = datetime(2026, 11, 6, 15, tzinfo=timezone.utc).timestamp()
        self.session = {
            "session_id": "55aeb7d0-e4fd-4cd9-9581-559463ee4acf",
            "entry_id": "entry", "device_id": "pad", "name": "Desk walking",
            "start_time": self.start, "end_time": self.start + 180,
            "elapsed_seconds": 180, "moving_seconds": 120,
            "distance_m": 100, "steps": 301, "partial": False,
            "quality_flags": [],
            "samples": [
                {"timestamp": self.start, "distance_m": 0, "speed_mps": 0.75, "cadence_spm": 101, "steps": 0},
                {"timestamp": self.start + 60, "distance_m": 50, "speed_mps": 0, "cadence_spm": 0, "steps": 150},
                {"timestamp": self.start + 120, "distance_m": 50, "speed_mps": 0.75, "cadence_spm": 101, "steps": 150},
                {"timestamp": self.start + 180, "distance_m": 100, "speed_mps": 0, "cadence_spm": 0, "steps": 301},
            ],
            "timer_events": [
                {"timestamp": self.start, "event": "start"},
                {"timestamp": self.start + 60, "event": "stop"},
                {"timestamp": self.start + 120, "event": "start"},
                {"timestamp": self.start + 180, "event": "stop"},
            ],
        }

    def decode(self, session=None, zone="America/Toronto"):
        """Encode and decode an activity, requiring a valid header and CRC."""
        data = fit_export.encode_session(session or self.session, zone)
        decoder = Decoder(Stream.from_byte_array(data))
        self.assertTrue(decoder.is_fit())
        self.assertTrue(decoder.check_integrity())
        messages, errors = Decoder(Stream.from_byte_array(data)).read()
        self.assertFalse(errors)
        return data, messages

    def test_message_structure_units_and_step_convention(self):
        """Keep physical units and both-foot step totals through FIT cycles."""
        _, messages = self.decode()
        self.assertEqual(len(messages["file_id_mesgs"]), 1)
        self.assertEqual(messages["file_id_mesgs"][0]["manufacturer"], "development")
        self.assertEqual(len(messages["lap_mesgs"]), 1)
        self.assertEqual(len(messages["session_mesgs"]), 1)
        self.assertEqual(len(messages["activity_mesgs"]), 1)
        session = messages["session_mesgs"][0]
        self.assertEqual(session["sport"], "walking")
        self.assertEqual(session["sub_sport"], "indoor_walking")
        self.assertEqual(session["total_timer_time"], 120)
        self.assertEqual(session["total_elapsed_time"], 180)
        self.assertEqual(session["total_distance"], 100)
        self.assertEqual(2 * (session["total_cycles"] + session["total_fractional_cycles"]), 301)
        record = messages["record_mesgs"][0]
        self.assertEqual(record["enhanced_speed"], 0.75)
        self.assertEqual(2 * (record["cadence"] + record["fractional_cadence"]), 101)
        for record in messages["record_mesgs"]:
            for unmeasured in ("position_lat", "position_long", "heart_rate", "calories", "altitude", "power"):
                self.assertNotIn(unmeasured, record)

    def test_preserves_irregular_records_and_pause_events(self):
        """Use observed samples and explicit timer events without interpolation."""
        _, messages = self.decode()
        records = messages["record_mesgs"]
        self.assertEqual(len(records), 4)
        self.assertEqual([record["distance"] for record in records], [0, 50, 50, 100])
        events = messages["event_mesgs"]
        self.assertEqual([event["event_type"] for event in events], ["start", "stop", "start", "stop"])
        self.assertEqual((events[2]["timestamp"] - events[1]["timestamp"]).total_seconds(), 60)

    def test_uses_session_end_timezone_offset_including_dst(self):
        """Use the configured HA zone at the activity date, not the host zone."""
        _, messages = self.decode()
        activity = messages["activity_mesgs"][0]
        self.assertEqual(activity["local_timestamp"] - (activity["timestamp"].timestamp() - FIT_EPOCH_S), -5 * 3600)
        summer = deepcopy(self.session)
        shift = datetime(2026, 7, 6, 15, tzinfo=timezone.utc).timestamp() - self.start
        for field in ("start_time", "end_time"):
            summer[field] += shift
        for row in summer["samples"] + summer["timer_events"]:
            row["timestamp"] += shift
        _, messages = self.decode(summer)
        activity = messages["activity_mesgs"][0]
        self.assertEqual(activity["local_timestamp"] - (activity["timestamp"].timestamp() - FIT_EPOCH_S), -4 * 3600)

    def test_encoding_is_deterministic_and_does_not_mutate_source(self):
        """Retries use the same bytes and source JSON remains untouched."""
        original = deepcopy(self.session)
        first, _ = self.decode()
        second, _ = self.decode()
        self.assertEqual(first, second)
        self.assertEqual(self.session, original)

    def test_rejects_corrupt_measurements(self):
        """Invalid physical values fail locally before a file can be uploaded."""
        for field, value in (("distance_m", -1), ("moving_seconds", 200), ("steps", 1.5), ("start_time", float("nan"))):
            with self.subTest(field=field):
                session = deepcopy(self.session)
                session[field] = value
                with self.assertRaises(ValueError):
                    fit_export.encode_session(session, "America/Toronto")
        session = deepcopy(self.session)
        session["samples"][2]["distance_m"] = 20
        with self.assertRaises(ValueError):
            fit_export.encode_session(session, "America/Toronto")


if __name__ == "__main__":
    unittest.main()
