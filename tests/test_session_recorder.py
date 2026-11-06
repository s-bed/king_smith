"""Verify session recording without Home Assistant or Bluetooth hardware."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest


_SOURCE = Path(__file__).resolve().parents[1] / "custom_components/king_smith/session_recorder.py"
_SPEC = importlib.util.spec_from_file_location("session_recorder_under_test", _SOURCE)
recorder_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = recorder_module
_SPEC.loader.exec_module(recorder_module)
StatusSample = recorder_module.StatusSample
SessionRecorder = recorder_module.SessionRecorder


def sample(timestamp, duration=0, distance=0, steps=0, speed=0):
    """Construct a physical-unit observation."""
    return StatusSample(timestamp, duration, distance, steps, speed)


class SessionRecorderTests(unittest.TestCase):
    """Cover physical session boundaries, counter baselines, and recovery."""

    def setUp(self):
        """Create an independent recorder for each scenario."""
        self.recorder = SessionRecorder("entry", "device", "Desk treadmill")

    def start_walk(self):
        """Establish a known stopped baseline and five seconds of walking."""
        self.recorder.receive(sample(100))
        self.recorder.receive(sample(105, 5, 10, 8, 1))

    def test_stop_waits_two_minutes_but_preserves_endpoint(self):
        """The grace period never adds time or stationary samples to the activity."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        self.assertEqual(self.recorder.state, "paused")
        self.assertEqual(self.recorder.expire(229.999), [])
        result = self.recorder.expire(230)[0]
        self.assertEqual(result["start_time"], 100)
        self.assertEqual(result["end_time"], 110)
        self.assertEqual(result["elapsed_seconds"], 10)
        self.assertEqual(result["moving_seconds"], 10)
        self.assertEqual(result["distance_m"], 20)
        self.assertEqual(result["steps"], 16)
        self.assertFalse(result["partial"])
        self.assertEqual(result["samples"][-1]["timestamp"], 110)
        self.assertEqual(self.recorder.state, "idle")
        self.assertEqual(self.recorder.expire(500), [])

    def test_repeated_stopped_updates_do_not_extend_stop_deadline(self):
        """Periodic idle packets keep baselines fresh without extending a session."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        self.recorder.receive(sample(115, 10, 20, 16))
        self.recorder.receive(sample(120, 10, 20, 16))
        result = self.recorder.expire(230)[0]
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(result["timer_events"][-1], {"timestamp": 110, "event": "stop"})
        self.assertFalse(result["partial"])

    def test_short_pause_merges_segments_and_excludes_paused_duration(self):
        """Elapsed time includes a merged pause while timer duration does not."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        self.recorder.receive(sample(160, 10, 20, 16))
        self.recorder.receive(sample(165, 15, 30, 24, 1))
        self.recorder.receive(sample(170, 20, 40, 32))
        result = self.recorder.expire(290)[0]
        self.assertEqual(result["moving_seconds"], 20)
        self.assertEqual(result["elapsed_seconds"], 70)
        self.assertEqual(result["steps"], 32)
        self.assertEqual([event["event"] for event in result["timer_events"]], ["start", "stop", "start", "stop"])
        self.assertEqual(result["timer_events"][2]["timestamp"], 160)

    def test_pause_boundary_starts_a_new_session(self):
        """A resume at exactly the two-minute limit belongs to another activity."""
        self.start_walk()
        first_id = self.recorder.active["session_id"]
        self.recorder.receive(sample(110, 10, 20, 16))
        self.recorder.receive(sample(225, 10, 20, 16))
        completed = self.recorder.receive(sample(230, 15, 30, 24, 1))
        self.assertEqual(completed[0]["session_id"], first_id)
        self.assertNotEqual(self.recorder.active["session_id"], first_id)
        self.assertEqual(self.recorder.active["steps"], 8)

    def test_counter_reset_during_pause_preserves_both_segments(self):
        """Device resets never erase or double-count the preceding segment."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        self.recorder.receive(sample(115))
        self.recorder.receive(sample(120, 5, 10, 8, 1))
        self.recorder.receive(sample(125, 10, 20, 16))
        result = self.recorder.expire(245)[0]
        self.assertEqual(result["moving_seconds"], 20)
        self.assertEqual(result["distance_m"], 40)
        self.assertEqual(result["steps"], 32)
        self.assertFalse(result["partial"])

    def test_first_moving_packet_records_only_observed_portion(self):
        """Unknown activity before attachment is excluded and labelled partial."""
        self.recorder.receive(sample(100, 500, 800, 1000, 1))
        self.assertTrue(self.recorder.active["partial"])
        self.assertEqual(self.recorder.active["moving_seconds"], 0)
        self.recorder.receive(sample(105, 505, 810, 1008, 1))
        result = self.recorder.expire(225)[0]
        self.assertEqual(result["start_time"], 100)
        self.assertEqual(result["moving_seconds"], 5)
        self.assertEqual(result["distance_m"], 10)
        self.assertEqual(result["steps"], 8)
        self.assertIn("started_mid_walk", result["quality_flags"])

    def test_new_walk_can_reset_counters_after_a_known_idle_sample(self):
        """A reset at a witnessed start does not make an otherwise complete walk partial."""
        self.recorder.receive(sample(100, 300, 600, 500))
        self.recorder.receive(sample(105, 5, 10, 8, 1))
        self.recorder.receive(sample(110, 10, 20, 16))
        result = self.recorder.expire(230)[0]
        self.assertEqual(result["moving_seconds"], 10)
        self.assertEqual(result["distance_m"], 20)
        self.assertFalse(result["partial"])
        self.assertIn("counter_reset", result["quality_flags"])

    def test_normal_counter_clear_on_stop_is_not_partial(self):
        """A stop that clears all counters keeps captured totals without an alarm."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16, 1))
        self.recorder.receive(sample(115))
        result = self.recorder.expire(235)[0]
        self.assertEqual(result["moving_seconds"], 10)
        self.assertEqual(result["distance_m"], 20)
        self.assertEqual(result["steps"], 16)
        self.assertFalse(result["partial"])
        self.assertEqual(result["quality_flags"], ["counter_reset"])

    def test_counters_cleared_on_stop_and_resume_preserve_both_segments(self):
        """Repeated normal stop resets neither erase totals nor imply data loss."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16, 1))
        self.recorder.receive(sample(115))
        self.recorder.receive(sample(120, 5, 10, 8, 1))
        self.recorder.receive(sample(125))
        result = self.recorder.expire(245)[0]
        self.assertEqual(result["moving_seconds"], 15)
        self.assertEqual(result["distance_m"], 30)
        self.assertEqual(result["steps"], 24)
        self.assertEqual([item["event"] for item in result["timer_events"]], ["start", "stop", "start", "stop"])
        self.assertFalse(result["partial"])

    def test_counter_reset_while_moving_retains_specific_partial_reason(self):
        """A reset without a witnessed stop can lose an unobserved moving interval."""
        self.start_walk()
        self.recorder.receive(sample(110, 2, 4, 3, 1))
        self.recorder.receive(sample(115, 7, 14, 11))
        result = self.recorder.expire(235)[0]
        self.assertEqual(result["moving_seconds"], 12)
        self.assertEqual(result["distance_m"], 24)
        self.assertEqual(result["steps"], 19)
        self.assertTrue(result["partial"])
        self.assertIn("counter_reset_while_moving", result["quality_flags"])

    def test_missing_stationary_packets_do_not_make_a_walk_partial(self):
        """An unchanged stopped counter needs no invented stationary records."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        self.recorder.receive(sample(200, 10, 20, 16))
        result = self.recorder.expire(230)[0]
        self.assertFalse(result["partial"])
        self.assertEqual(result["end_time"], 110)

    def test_connection_loss_ends_at_last_sample_and_drops_old_baseline(self):
        """No-data timeout marks a partial activity without adding the timeout."""
        self.start_walk()
        self.assertEqual(self.recorder.expire(224.999), [])
        result = self.recorder.expire(225)[0]
        self.assertEqual(result["end_time"], 105)
        self.assertEqual(result["moving_seconds"], 5)
        self.assertTrue(result["partial"])
        self.assertIn("connection_lost", result["quality_flags"])
        self.recorder.receive(sample(300, 200, 600, 500, 1))
        self.assertEqual(self.recorder.active["distance_m"], 0)

    def test_reconnect_before_deadline_recovers_the_same_session(self):
        """Short missing-data gaps preserve the UUID and flag reduced coverage."""
        self.start_walk()
        session_id = self.recorder.active["session_id"]
        self.recorder.receive(sample(165, 65, 130, 104, 1))
        self.assertEqual(self.recorder.active["session_id"], session_id)
        self.assertEqual(self.recorder.active["moving_seconds"], 65)
        self.assertIn("data_gap", self.recorder.active["quality_flags"])

    def test_checkpoint_recovers_a_paused_deadline(self):
        """A Home Assistant restart does not restart the pause grace period."""
        self.start_walk()
        self.recorder.receive(sample(110, 10, 20, 16))
        state = json.loads(json.dumps(self.recorder.to_dict()))
        restored = SessionRecorder("entry", "device", "Desk treadmill", state)
        self.assertEqual(restored.state, "paused")
        self.assertEqual(restored.next_deadline, 230)
        self.assertEqual(restored.expire(229), [])
        self.assertEqual(restored.expire(230)[0]["end_time"], 110)

    def test_checkpoint_recovers_recording_or_finalizes_an_expired_gap(self):
        """Reloaded recorders retain observations, UUIDs, and inactivity deadlines."""
        self.start_walk()
        checkpoint = self.recorder.to_dict()
        restored = SessionRecorder("entry", "device", "Desk treadmill", checkpoint)
        restored.receive(sample(110, 10, 20, 16, 1))
        self.assertEqual(restored.active["session_id"], self.recorder.active["session_id"])
        self.assertEqual(restored.active["steps"], 16)
        self.assertEqual(self.recorder.active["steps"], 8)
        expired = SessionRecorder("entry", "device", "Desk treadmill", checkpoint)
        self.assertTrue(expired.expire(225)[0]["partial"])

    def test_duplicate_and_out_of_order_samples_do_not_change_counters(self):
        """Old packets do not corrupt totals or session deadlines."""
        self.start_walk()
        before = self.recorder.to_dict()
        self.recorder.receive(sample(105, 50, 500, 500, 1))
        self.recorder.receive(sample(104, 4, 5, 6))
        self.assertEqual(self.recorder.to_dict(), before)

    def test_counter_jump_cannot_create_more_time_than_observed(self):
        """An implausible device-time jump is bounded and made visible."""
        self.start_walk()
        self.recorder.receive(sample(110, 500, 20, 16, 1))
        self.assertEqual(self.recorder.active["moving_seconds"], 10)
        self.assertTrue(self.recorder.active["partial"])
        self.assertIn("counter_jump", self.recorder.active["quality_flags"])

    def test_clustered_packets_use_device_duration_for_cadence(self):
        """A one-second device tick received 0.1 seconds later stays sixty steps/min."""
        self.recorder.receive(sample(100, 500, 800, 1000, 1))
        self.recorder.receive(sample(100.1, 501, 801, 1001, 1))
        self.assertEqual(self.recorder.active["samples"][-1]["cadence_spm"], 60)
        self.assertLessEqual(self.recorder.active["moving_seconds"], 0.1)
        # A subsequent observation supplies enough wall-time budget to preserve
        # the previously clipped source second even though its counter repeats.
        self.recorder.receive(sample(101.1, 501, 801, 1001, 1))
        self.assertEqual(self.recorder.active["moving_seconds"], 1)
        self.recorder.receive(sample(101.2, 501, 801, 1001))
        result = self.recorder.expire(221.2)[0]
        self.assertEqual(result["moving_seconds"], 1)
        self.assertNotIn("_moving_seconds_raw", result)

    def test_receipt_jitter_does_not_systematically_lose_device_seconds(self):
        """Early packets can recover clipped fractions when later packets arrive."""
        self.recorder.receive(sample(0))
        self.recorder.receive(sample(5.1, 5, 5, 5, 1))
        self.recorder.receive(sample(9.9, 10, 10, 10, 1))
        self.assertLessEqual(
            self.recorder.active["moving_seconds"],
            self.recorder.active["elapsed_seconds"],
        )
        self.recorder.receive(sample(15.1, 15, 15, 15, 1))
        self.recorder.receive(sample(16.1, 16, 16, 16))
        result = self.recorder.expire(136.1)[0]
        self.assertAlmostEqual(result["moving_seconds"], 16)
        self.assertAlmostEqual(result["elapsed_seconds"], 16)

    def test_quantized_duration_credit_survives_a_checkpoint(self):
        """Restart recovery retains raw counter seconds awaiting wall-time budget."""
        self.recorder.receive(sample(100, 500, 800, 1000, 1))
        self.recorder.receive(sample(100.1, 501, 801, 1001, 1))
        checkpoint = json.loads(json.dumps(self.recorder.to_dict()))
        restored = SessionRecorder("entry", "device", "Desk treadmill", checkpoint)
        restored.receive(sample(101.1, 501, 801, 1001, 1))
        self.assertEqual(restored.active["moving_seconds"], 1)

    def test_multiple_pads_and_registry_updates_are_independent(self):
        """Only the intended recorder changes, and final metadata stays current."""
        other = SessionRecorder("second", "other_device", "Other treadmill")
        self.start_walk()
        self.assertEqual(other.state, "idle")
        self.recorder.device_id = "new_device"
        self.recorder.name = "Renamed treadmill"
        result = self.recorder.expire(225)[0]
        self.assertEqual(result["device_id"], "new_device")
        self.assertEqual(result["name"], "Renamed treadmill")

    def test_invalid_physical_values_are_rejected(self):
        """Bad data cannot introduce negative totals or non-JSON numbers."""
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sample(100, distance=value)


if __name__ == "__main__":
    unittest.main()
