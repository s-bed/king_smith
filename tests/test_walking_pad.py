"""Exercise Walking Pad behavior without Home Assistant or Bluetooth hardware."""

import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch


# Load the API directly so these tests do not start the integration's HA setup.
_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/king_smith/walking_pad.py"
)
_PACKAGE = ModuleType("walking_pad_tests")
_PACKAGE.__path__ = [str(_SOURCE.parent)]
sys.modules[_PACKAGE.__name__] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location("walking_pad_tests.walking_pad", _SOURCE)
walking_pad = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(walking_pad)


class WalkingPadApiTests(unittest.IsolatedAsyncioTestCase):
    """Cover cadence, command concurrency, and offline recovery."""

    def setUp(self):
        """Create an API backed by an asynchronous fake controller."""
        self.controller = SimpleNamespace(
            run=AsyncMock(),
            disconnect=AsyncMock(),
            ask_stats=AsyncMock(),
            change_speed=AsyncMock(),
        )
        self.device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
        with patch.object(walking_pad, "Controller", return_value=self.controller):
            self.api = walking_pad.WalkingPadApi("Desk treadmill", self.device)
        self.api._last_cmd_time = 0
        self.api._end_cmd = AsyncMock()

    def send_status(self, elapsed, steps, speed=10):
        """Deliver a device status through the registered controller callback."""
        status = SimpleNamespace(time=elapsed, steps=steps, speed=speed, dist=0)
        self.controller.handler_cur_status(None, status)
        return status

    def test_cadence_averages_only_the_last_ten_readings(self):
        """Discard older cadence readings once the ten-sample window fills."""
        callback = Mock()
        self.api.register_status_callback(callback)
        steps = 0
        self.send_status(1, steps)
        for elapsed in range(2, 14):
            steps += elapsed
            last_status = self.send_status(elapsed, steps)
        self.assertEqual(self.api.step_cadence, 13 * 60)
        self.assertAlmostEqual(self.api.step_cadence_mean, 8.5 * 60)
        callback.assert_called_with(last_status)

    def test_stopping_clears_cadence_history(self):
        """A new walking session does not inherit cadence from the last one."""
        self.send_status(1, 0)
        self.send_status(2, 2)
        self.assertGreater(self.api.step_cadence_mean, 0)
        self.send_status(3, 2, speed=0)
        self.assertEqual(self.api.step_cadence_mean, 0)
        self.send_status(4, 2)
        self.assertEqual(self.api.step_cadence_mean, 0)
        self.send_status(5, 3)
        self.assertEqual(self.api.step_cadence_mean, 30)

    def test_repeated_timestamp_and_counter_reset_are_safe(self):
        """Repeated timestamps avoid division by zero and resets stay nonnegative."""
        self.send_status(1, 5)
        self.send_status(1, 5)
        self.assertEqual(self.api.step_cadence, 0)
        self.send_status(2, 0)
        self.assertEqual(self.api.step_cadence, 0)
        self.assertEqual(self.api.step_cadence_mean, 0)

    async def test_polling_waits_for_an_inflight_command(self):
        """Polling and another command cannot overlap a blocked speed change."""
        self.api._connected = True
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def change_speed(speed):
            calls.append(speed)
            if speed == 10:
                started.set()
                await release.wait()

        self.controller.change_speed.side_effect = change_speed
        first = asyncio.create_task(self.api.change_speed(10))
        await asyncio.wait_for(started.wait(), timeout=1)
        poll = asyncio.create_task(self.api.update_state())
        second = asyncio.create_task(self.api.change_speed(20))
        try:
            await asyncio.sleep(0)
            self.controller.ask_stats.assert_not_awaited()
            self.assertEqual(calls, [10])
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(first, poll, second), timeout=1)
        self.controller.ask_stats.assert_awaited_once()
        self.assertEqual(calls, [10, 20])
        self.assertEqual(self.api.speed, 20)

    async def test_offline_poll_reconnects(self):
        """An offline poll connects without issuing a concurrent stats request."""
        await self.api.update_state()
        self.controller.run.assert_awaited_once_with(self.device)
        self.controller.ask_stats.assert_not_awaited()
        self.assertTrue(self.api.connected)

    def test_recording_observes_immutable_packets_during_command_suppression(self):
        """A stop packet reaches recording even while display updates are held."""
        received = []
        unsubscribe = self.api.register_snapshot_callback(received.append)
        self.api._status_lock = True
        status = SimpleNamespace(time=65, steps=101, speed=0, dist=10)
        self.controller.handler_cur_status(None, status)
        status.steps = 0
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].steps, 101)
        self.assertEqual(received[0].distance_m, 100)
        self.assertEqual(received[0].speed_mps, 0)
        unsubscribe()
        self.controller.handler_cur_status(None, status)
        self.assertEqual(len(received), 1)

    async def test_offline_device_can_be_resolved_after_startup(self):
        """Saved devices keep identity and can reconnect when Bluetooth returns."""
        resolver = Mock(return_value=None)
        with patch.object(walking_pad, "Controller", return_value=self.controller):
            api = walking_pad.WalkingPadApi(
                "Desk treadmill", None, address=self.device.address, device_resolver=resolver
            )
        api._end_cmd = AsyncMock()
        self.assertEqual(api.mac, self.device.address)
        with self.assertRaises(ConnectionError):
            await api.connect()
        self.controller.run.assert_not_awaited()
        resolver.return_value = self.device
        await api.connect()
        self.controller.run.assert_awaited_once_with(self.device)
        self.assertTrue(api.connected)

    async def test_connection_failure_is_logged_at_debug(self):
        """An unavailable treadmill does not produce warning-level log spam."""
        self.controller.run.side_effect = OSError("offline")
        with self.assertLogs(walking_pad._LOGGER, level="DEBUG") as logs:
            await self.api.update_state()
        self.assertTrue(all(record.levelno == logging.DEBUG for record in logs.records))
        self.assertFalse(self.api.connected)

    async def test_failed_poll_handles_failed_disconnect(self):
        """A failed cleanup still marks the device disconnected for the next poll."""
        self.api._connected = True
        self.controller.ask_stats.side_effect = OSError("not responding")
        self.controller.disconnect.side_effect = OSError("already disconnected")
        with self.assertLogs(walking_pad._LOGGER, level="DEBUG") as logs:
            await self.api.update_state()
        self.controller.disconnect.assert_awaited_once()
        self.assertFalse(self.api.connected)
        self.assertTrue(all(record.levelno == logging.DEBUG for record in logs.records))
        await self.api.update_state()
        self.assertTrue(self.api.connected)

    async def test_disconnect_failure_still_clears_connected_state(self):
        """Unloading an already-disconnected device finishes cleanly."""
        self.api._connected = True
        self.controller.disconnect.side_effect = OSError("already disconnected")
        await self.api.disconnect()
        self.assertFalse(self.api.connected)
        self.api._end_cmd.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
