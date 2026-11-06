"""Exercise durable recording and upload approval through injected HA adapters."""

import asyncio
import base64
from copy import deepcopy
import importlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "king_smith_manager_under_test"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "custom_components/king_smith")]
sys.modules[_PACKAGE_NAME] = _PACKAGE
manager_module = importlib.import_module(f"{_PACKAGE_NAME}.session_manager")
recorder_module = importlib.import_module(f"{_PACKAGE_NAME}.session_recorder")
SessionManager = manager_module.SessionManager
SessionError = manager_module.SessionError
StatusSample = recorder_module.StatusSample
_NOW = 1793977200.0


class FakeStore:
    """Keep independent snapshots and model failures at durable boundaries."""

    def __init__(self, initial=None):
        """Configure a store without sharing mutable snapshots with the manager."""
        self.data = deepcopy(initial)
        self.saves = []
        self.failures = 0
        self.fail_uploading = 0
        self.fail_sent = 0

    async def async_load(self):
        """Return an isolated view of the last durable snapshot."""
        return deepcopy(self.data)

    async def async_save(self, data):
        """Fail before changing durable state when instructed."""
        uploading = any(s["status"] == "uploading" for s in data["sessions"].values())
        sent = any(s["status"] == "sent" for s in data["sessions"].values())
        if self.failures or (self.fail_uploading and uploading) or (self.fail_sent and sent):
            if self.failures:
                self.failures -= 1
            elif self.fail_uploading and uploading:
                self.fail_uploading -= 1
            else:
                self.fail_sent -= 1
            raise OSError("Simulated storage failure")
        self.data = deepcopy(data)
        self.saves.append(deepcopy(data))


class FakeBus:
    """Collect isolated event payloads for lifecycle assertions."""

    def __init__(self):
        """Initialize the event log."""
        self.events = []
        self.on_fire = None

    def async_fire(self, name, data=None):
        """Remember a fired event without retaining mutable session references."""
        if self.on_fire:
            self.on_fire(name, data)
        self.events.append((name, deepcopy(data)))


class FakeServices:
    """Model the public Garmin upload action without making network requests."""

    def __init__(self):
        """Initialize availability, failure, and upload inspection controls."""
        self.available = True
        self.calls = []
        self.error = None
        self.on_call = None

    def has_service(self, domain, name):
        """Advertise only the expected optional Garmin action."""
        return self.available and (domain, name) == ("garmin_connect", "upload_activity")

    async def async_call(self, domain, name, data, blocking=False):
        """Record the exact approved account and optionally suspend dispatch."""
        self.calls.append((domain, name, deepcopy(data), blocking))
        if self.on_call:
            await self.on_call(data)
        if self.error:
            raise self.error


class FakeHass:
    """Supply only the HA adapter surface used by SessionManager."""

    def __init__(self, directory):
        """Give each test a private configuration directory."""
        self.config = SimpleNamespace(
            path=lambda *parts: str(directory.joinpath(*parts)), time_zone="UTC"
        )
        self.bus = FakeBus()
        self.services = FakeServices()

    async def async_add_executor_job(self, function, *args):
        """Perform actual encoding and file operations outside the event loop."""
        return await asyncio.to_thread(function, *args)


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    """Test persistence, approval races, recovery, and retention using real FIT files."""

    async def asyncSetUp(self):
        """Create a deterministic clock and a private HA storage directory."""
        temporary_root = _ROOT / "venv/tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.directory.cleanup)
        self.hass = FakeHass(Path(self.directory.name))
        self.store = FakeStore()
        self.ready = True
        clock_patch = patch.object(manager_module.time, "time", return_value=_NOW)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.managers = []
        self.manager = await self.new_manager(self.store)

    async def asyncTearDown(self):
        """Flush and close background upload workers before removing test files."""
        for manager in reversed(self.managers):
            await manager.async_shutdown()

    async def new_manager(self, store):
        """Initialize a manager and settle its initial empty upload worker."""
        manager = SessionManager(self.hass, store, lambda _entity: self.ready)
        self.managers.append(manager)
        await manager.async_initialize()
        await self.drain(manager)
        return manager

    async def drain(self, manager=None):
        """Wait for the current worker without introducing arbitrary sleeps."""
        manager = manager or self.manager
        if manager._worker_task:
            await asyncio.wait_for(asyncio.shield(manager._worker_task), timeout=5)

    async def finish_walk(self, entry_id="entry", device_id="pad", offset=0):
        """Record and archive a known ten-second physical walk."""
        base = _NOW + offset
        self.manager.attach(entry_id, device_id, "Desk walking")
        await self.manager.async_receive(entry_id, StatusSample(base, 0, 0, 0, 0))
        await self.manager.async_receive(entry_id, StatusSample(base + 5, 5, 10, 8, 1))
        session_id = self.manager.recorders[entry_id].active["session_id"]
        await self.manager.async_receive(entry_id, StatusSample(base + 10, 10, 20, 16, 0))
        self.clock.return_value = base + 130
        await self.manager.async_tick()
        await self.drain()
        return session_id

    async def test_completed_session_is_durable_before_ready_event(self):
        """A ready activity has persisted source, a valid FIT, and a durable index."""
        def require_checkpoint(name, data):
            if name == "king_smith_session_ready":
                durable = self.store.data["sessions"][data["session_id"]]
                self.assertEqual(durable["fit_sha256"], data["fit_sha256"])
                self.assertNotIn(data["session_id"], self.store.data["pending_archives"])

        self.hass.bus.on_fire = require_checkpoint
        session_id = await self.finish_walk()
        session = self.manager.sessions[session_id]
        json_path, fit_path = self.manager._paths(session)
        self.assertTrue(json_path.is_file())
        self.assertTrue(fit_path.is_file())
        self.assertTrue(self.manager._valid_fit(fit_path, session["fit_sha256"]))
        self.assertEqual(self.store.data["sessions"][session_id]["status"], "awaiting_review")
        self.assertNotIn(session_id, self.store.data["pending_archives"])
        ready = [data for name, data in self.hass.bus.events if name == "king_smith_session_ready"]
        self.assertEqual([event["session_id"] for event in ready], [session_id])
        self.assertEqual(self.manager.pending_count("entry"), 1)
        self.assertEqual(self.hass.services.calls, [])

    async def test_upload_is_checkpointed_and_routes_the_explicit_account(self):
        """Garmin receives only the selected account after the uploading checkpoint."""
        session_id = await self.finish_walk()

        async def verify_checkpoint(arguments):
            self.assertEqual(self.store.data["sessions"][session_id]["status"], "uploading")
            self.assertEqual(arguments["entity_id"], "sensor.garmin_second_account")
            self.assertTrue(Path(arguments["file_path"]).is_file())

        self.hass.services.on_call = verify_checkpoint
        await self.manager.async_upload("pad", session_id, "sensor.garmin_second_account")
        await self.drain()
        self.assertEqual(len(self.hass.services.calls), 1)
        domain, service, _, blocking = self.hass.services.calls[0]
        self.assertEqual((domain, service, blocking), ("garmin_connect", "upload_activity", True))
        self.assertEqual(self.store.data["sessions"][session_id]["status"], "sent")
        self.assertEqual(self.manager.pending_count("entry"), 0)

    async def test_duplicate_taps_and_discard_cannot_race_an_upload(self):
        """Repeated approvals dispatch once, and an in-flight upload cannot be cancelled."""
        session_id = await self.finish_walk()
        started, release = asyncio.Event(), asyncio.Event()

        async def suspend_upload(_arguments):
            started.set()
            await release.wait()

        self.hass.services.on_call = suspend_upload
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            results = await asyncio.gather(*(
                self.manager.async_upload("pad", session_id, "sensor.garmin")
                for _ in range(3)
            ))
            self.assertTrue(all(result["status"] == "uploading" for result in results))
            with self.assertRaises(SessionError):
                await self.manager.async_discard("pad", session_id)
            self.assertEqual(len(self.hass.services.calls), 1)
        finally:
            release.set()
            await self.drain()
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_missing_service_blocks_and_recovers_the_approved_queue(self):
        """Approval survives a missing optional Garmin service without an early request."""
        session_id = await self.finish_walk()
        self.hass.services.available = False
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "blocked")
        self.assertEqual(self.hass.services.calls, [])
        self.hass.services.available = True
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "sent")

    async def test_unloaded_selected_account_blocks_without_using_another(self):
        """Another available Garmin account is never substituted for the selected one."""
        session_id = await self.finish_walk()
        self.ready = False
        await self.manager.async_upload("pad", session_id, "sensor.selected_account")
        await self.drain()
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(self.manager.sessions[session_id]["status"], "blocked")
        self.ready = True
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(self.hass.services.calls[0][2]["entity_id"], "sensor.selected_account")

    async def test_failed_approval_checkpoint_prevents_network_dispatch(self):
        """A storage failure cannot cause an uncheckpointed approved upload."""
        session_id = await self.finish_walk()
        self.store.failures = 1
        with self.assertRaises(OSError):
            await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await asyncio.sleep(0)
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(self.store.data["sessions"][session_id]["status"], "awaiting_review")
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "sent")

    async def test_failed_uploading_checkpoint_holds_dispatch_until_recovery(self):
        """The final before-network checkpoint must succeed before Garmin is called."""
        session_id = await self.finish_walk()
        self.store.fail_uploading = 1
        with self.assertLogs(manager_module._LOGGER, level="ERROR"):
            await self.manager.async_upload("pad", session_id, "sensor.garmin")
            with self.assertRaises(OSError):
                await self.drain()
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(self.manager.sessions[session_id]["status"], "queued")
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "sent")

    async def test_ambiguous_failure_requires_an_explicit_retry(self):
        """No automatic tick or old Upload button can resend an uncertain activity."""
        session_id = await self.finish_walk()
        self.hass.services.error = OSError("Upload timed out after possible acceptance")
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "needs_review")
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(len(self.hass.services.calls), 1)
        with self.assertRaises(SessionError):
            await self.manager.async_upload("pad", session_id, "sensor.garmin")
        self.hass.services.error = None
        await self.manager.async_upload("pad", session_id, "sensor.garmin", retry=True)
        await self.drain()
        self.assertEqual(len(self.hass.services.calls), 2)
        self.assertEqual(self.manager.sessions[session_id]["status"], "sent")

    async def test_failed_success_checkpoint_never_resends_accepted_request(self):
        """A post-upload disk failure preserves the in-flight marker for restart review."""
        session_id = await self.finish_walk()
        self.store.fail_sent = 1
        with self.assertLogs(manager_module._LOGGER, level="ERROR"):
            await self.manager.async_upload("pad", session_id, "sensor.garmin")
            with self.assertRaises(OSError):
                await self.drain()
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(self.store.data["sessions"][session_id]["status"], "uploading")
        recovered = await self.new_manager(FakeStore(self.store.data))
        self.assertEqual(recovered.sessions[session_id]["status"], "needs_review")
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_retry_rejects_an_unseen_attempt_from_another_client(self):
        """An old web confirmation cannot approve retrying a newer uncertain upload."""
        session_id = await self.finish_walk()
        self.hass.services.error = OSError("Uncertain upload")
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await self.drain()
        await self.manager.async_upload("pad", session_id, "sensor.garmin", retry=True, expected_attempts=1)
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["attempts"], 2)
        with self.assertRaisesRegex(SessionError, "attempt changed"):
            await self.manager.async_upload("pad", session_id, "sensor.garmin", retry=True, expected_attempts=1)
        self.assertEqual(len(self.hass.services.calls), 2)
        self.assertEqual(self.manager.sessions[session_id]["status"], "needs_review")

    async def test_restart_marks_uploading_as_uncertain_without_resending(self):
        """A persisted in-flight request becomes a review item on restart."""
        session_id = await self.finish_walk()
        saved = deepcopy(self.store.data)
        saved["sessions"][session_id].update(status="uploading", garmin_entity_id="sensor.garmin")
        recovered_store = FakeStore(saved)
        recovered = await self.new_manager(recovered_store)
        self.assertEqual(recovered.sessions[session_id]["status"], "needs_review")
        self.assertEqual(recovered_store.data["sessions"][session_id]["status"], "needs_review")
        self.assertEqual(self.hass.services.calls, [])

    async def test_shutdown_of_inflight_upload_persists_uncertainty(self):
        """Unloading cannot turn a cancelled upload into an automatic retry."""
        session_id = await self.finish_walk()
        started = asyncio.Event()

        async def wait_until_cancelled(_arguments):
            started.set()
            await asyncio.Event().wait()

        self.hass.services.on_call = wait_until_cancelled
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await asyncio.wait_for(started.wait(), timeout=5)
        await self.manager.async_shutdown()
        self.assertEqual(self.store.data["sessions"][session_id]["status"], "needs_review")
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_discard_preserves_files_and_rejects_stale_upload_buttons(self):
        """Discard is a retained local decision rather than deletion or upload."""
        session_id = await self.finish_walk()
        paths = self.manager._paths(self.manager.sessions[session_id])
        contents = [path.read_bytes() for path in paths]
        await self.manager.async_discard("pad", session_id)
        await self.manager.async_discard("pad", session_id)
        result = await self.manager.async_upload("pad", session_id, "sensor.garmin")
        self.assertEqual(result["status"], "discarded")
        self.assertEqual([path.read_bytes() for path in paths], contents)
        self.assertEqual(self.hass.services.calls, [])

    async def test_retention_expires_resolved_files_but_keeps_unresolved(self):
        """Only decisions older than ninety days allow source and FIT deletion."""
        sent = await self.finish_walk(offset=0)
        discarded = await self.finish_walk(offset=300)
        pending = await self.finish_walk(offset=600)
        failed = await self.finish_walk(offset=900)
        await self.manager.async_upload("pad", sent, "sensor.garmin")
        await self.drain()
        await self.manager.async_discard("pad", discarded)
        self.manager.sessions[failed]["status"] = "needs_review"
        paths = {identifier: self.manager._paths(self.manager.sessions[identifier])
                 for identifier in (sent, discarded, pending, failed)}
        self.clock.return_value = _NOW + 900 + manager_module.RETENTION_SECONDS + 131
        await self.manager.async_tick()
        await self.drain()
        for identifier in (sent, discarded):
            self.assertNotIn(identifier, self.manager.sessions)
            self.assertTrue(all(not path.exists() for path in paths[identifier]))
        for identifier in (pending, failed):
            self.assertIn(identifier, self.manager.sessions)
            self.assertTrue(all(path.is_file() for path in paths[identifier]))
        with self.assertRaises(SessionError):
            await self.manager.async_upload("pad", sent, "sensor.garmin")

    async def test_restart_restores_active_pause_deadline_and_totals(self):
        """Recovery retains the original stop and does not extend its grace period."""
        self.manager.attach("entry", "pad", "Desk walking")
        for sample in (
            StatusSample(_NOW, 0, 0, 0, 0),
            StatusSample(_NOW + 5, 5, 10, 8, 1),
            StatusSample(_NOW + 10, 10, 20, 16, 0),
        ):
            await self.manager.async_receive("entry", sample)
        self.clock.return_value = _NOW + 60
        recovered = await self.new_manager(FakeStore(self.store.data))
        self.assertEqual(recovered.recorder_state("entry"), "paused")
        self.assertEqual(recovered.recorders["entry"].next_deadline, _NOW + 130)
        self.clock.return_value = _NOW + 130
        await recovered.async_tick()
        await self.drain(recovered)
        session = recovered.list_sessions("pad")[0]
        self.assertEqual(session["end_time"], _NOW + 10)
        self.assertEqual(session["moving_seconds"], 10)
        self.assertEqual(session["steps"], 16)
        self.assertFalse(session["partial"])

    async def test_failed_fit_archive_retains_source_until_recovery(self):
        """Encoder or filesystem failure leaves a recoverable source checkpoint."""
        with patch.object(manager_module, "encode_session", side_effect=OSError("Disk failure")):
            session_id = await self.finish_walk()
        self.assertEqual(self.manager.sessions[session_id]["status"], "failed_local")
        self.assertIn(session_id, self.store.data["pending_archives"])
        self.assertEqual(self.store.data["pending_archives"][session_id]["steps"], 16)
        await self.manager.async_tick()
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "awaiting_review")
        self.assertNotIn(session_id, self.store.data["pending_archives"])

    async def test_missing_or_changed_fit_cannot_be_uploaded(self):
        """Local validation rejects a file changed after the reviewed recording."""
        session_id = await self.finish_walk()
        _, fit_path = self.manager._paths(self.manager.sessions[session_id])
        fit_path.write_bytes(b"not the recorded activity")
        await self.manager.async_upload("pad", session_id, "sensor.garmin")
        await self.drain()
        self.assertEqual(self.manager.sessions[session_id]["status"], "failed_local")
        self.assertEqual(self.hass.services.calls, [])

    async def test_multiple_pad_uploads_share_one_worker(self):
        """Two approved pads never overlap requests through the Garmin action."""
        first = await self.finish_walk("entry", "pad", 0)
        second = await self.finish_walk("entry2", "pad2", 300)
        started, release = asyncio.Event(), asyncio.Event()

        async def suspend_first(_arguments):
            if len(self.hass.services.calls) == 1:
                started.set()
                await release.wait()

        self.hass.services.on_call = suspend_first
        await self.manager.async_upload("pad", first, "sensor.garmin_first")
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            await self.manager.async_upload("pad2", second, "sensor.garmin_second")
            await asyncio.sleep(0)
            self.assertEqual(len(self.hass.services.calls), 1)
        finally:
            release.set()
            await self.drain()
        self.assertEqual([call[2]["entity_id"] for call in self.hass.services.calls],
                         ["sensor.garmin_first", "sensor.garmin_second"])
        self.assertEqual(self.manager.sessions[second]["status"], "sent")

    async def test_device_filter_and_returned_summaries_are_isolated(self):
        """Public reads and actions cannot cross pad boundaries or mutate the store."""
        session_id = await self.finish_walk()
        with self.assertRaises(SessionError):
            await self.manager.async_discard("another_pad", session_id)
        self.assertEqual(self.manager.list_sessions("another_pad"), [])
        listed = self.manager.list_sessions("pad", "awaiting_review")
        listed[0]["quality_flags"].append("changed_by_caller")
        listed[0]["status"] = "sent"
        self.assertEqual(self.manager.sessions[session_id]["quality_flags"], [])
        self.assertEqual(self.manager.sessions[session_id]["status"], "awaiting_review")

    async def test_inspection_lists_restored_pads_and_sessions_without_bluetooth(self):
        """The session browser can inspect all saved pads while no device is connected."""
        first = await self.finish_walk("entry", "pad", 0)
        second = await self.finish_walk("entry2", "pad2", 300)
        self.manager.attach("idle_entry", "idle_pad", "Idle treadmill")
        await self.manager.async_tick()
        recovered = await self.new_manager(FakeStore(self.store.data))
        listed = recovered.list_all_sessions()
        self.assertEqual([session["session_id"] for session in listed], [second, first])
        devices = {device["device_id"]: device for device in recovered.list_devices()}
        self.assertEqual(set(devices), {"pad", "pad2", "idle_pad"})
        self.assertEqual(devices["pad"]["pending_sessions"], 1)
        self.assertEqual(devices["idle_pad"]["pending_sessions"], 0)
        self.assertTrue(all(device["recorder_state"] == "idle" for device in devices.values()))
        listed[0]["quality_notes"].append("changed by browser")
        listed[0]["name"] = "Browser-only label"
        self.assertEqual(recovered.list_all_sessions()[0]["name"], "Desk walking")
        self.assertEqual(recovered.list_all_sessions()[0]["quality_notes"], [])
        self.assertEqual(self.hass.services.calls, [])

    async def test_session_detail_reads_actual_fit_odd_steps_and_quality_reasons(self):
        """Details retain odd steps encoded as a fractional stride and exact saved reasons."""
        self.manager.attach("entry", "pad", "Desk walking")
        await self.manager.async_receive("entry", StatusSample(_NOW, 0, 0, 0, 0))
        for offset, steps, distance in ((5, 8, 10), (10, 16, 10), (15, 24, 20), (20, 32, 20), (25, 39, 30)):
            await self.manager.async_receive(
                "entry", StatusSample(_NOW + offset, offset, distance, steps, 0 if offset == 25 else 1)
            )
        active = self.manager.recorders["entry"].active
        session_id = active["session_id"]
        active.update(partial=True, quality_flags=["counter_reset"])
        self.clock.return_value = _NOW + 145
        await self.manager.async_tick()
        detail = await self.manager.async_session_detail(session_id)
        self.assertEqual(detail["session"]["steps"], 39)
        self.assertEqual(detail["recording"]["steps"], 39)
        self.assertEqual(detail["fit_summary"]["steps"], 39)
        self.assertEqual(detail["fit_summary"]["distance_m"], 30)
        self.assertEqual(detail["fit_summary"]["timer_seconds"], 25)
        self.assertEqual(detail["fit_summary"]["elapsed_seconds"], 25)
        self.assertEqual(detail["fit_summary"]["record_count"], 5)
        self.assertEqual(detail["quality_details"][0]["code"], "counter_reset")
        self.assertIn("counters", detail["quality_details"][0]["title"])
        self.assertEqual(detail["quality_details"][0]["severity"], "info")
        self.assertFalse(detail["session"]["partial"])
        self.assertTrue(detail["session"]["recorded_partial"])
        self.assertTrue(detail["recording"]["partial"])
        self.assertTrue(self.manager.sessions[session_id]["partial"])
        self.assertFalse(self.manager.list_sessions("pad")[0]["partial"])
        self.assertFalse(self.manager.list_all_sessions()[0]["partial"])
        downloaded = await self.manager.async_session_download(session_id, "json")
        self.assertIn(b'"partial": true', base64.b64decode(downloaded["content"]))
        self.assertIsNone(detail["fit_error"])
        self.assertIsNone(detail["recording_error"])
        # A detail read never regenerates or mutates the source recording.
        detail["recording"]["steps"] = 0
        self.assertEqual((await self.manager.async_session_detail(session_id))["recording"]["steps"], 39)

    async def test_real_quality_losses_still_show_as_partial_in_public_summaries(self):
        """Historical reset normalization never hides evidence of an incomplete walk."""
        session_id = await self.finish_walk()
        for flags in (["counter_reset", "data_gap"], ["counter_reset_while_moving"], []):
            with self.subTest(flags=flags):
                self.manager.sessions[session_id].update(partial=True, quality_flags=flags)
                listed = self.manager.list_sessions("pad")[0]
                self.assertTrue(listed["partial"])
                self.assertTrue(listed["recorded_partial"])
                self.assertTrue(listed["quality_notes"])

    async def test_session_downloads_are_identical_to_the_existing_archives(self):
        """FIT and JSON downloads return the original bytes, not a reconstructed activity."""
        session_id = await self.finish_walk()
        json_path, fit_path = self.manager._paths(self.manager.sessions[session_id])
        for file_format, path, mime in (
            ("fit", fit_path, "application/octet-stream"),
            ("json", json_path, "application/json"),
        ):
            with self.subTest(file_format=file_format):
                result = await self.manager.async_session_download(session_id, file_format)
                self.assertEqual(base64.b64decode(result["content"], validate=True), path.read_bytes())
                self.assertEqual(result["filename"], f"walkingpad-{session_id}.{file_format}")
                self.assertEqual(result["mime_type"], mime)
        self.assertEqual(self.manager.sessions[session_id]["status"], "awaiting_review")
        self.assertEqual(self.hass.services.calls, [])

    async def test_unknown_or_pathlike_download_identifiers_never_reach_filesystem(self):
        """Browser requests resolve saved UUIDs and fixed formats rather than user paths."""
        session_id = await self.finish_walk()
        with patch.object(self.hass, "async_add_executor_job", side_effect=AssertionError("Unexpected file access")):
            for identifier in ("unknown", "../../configuration.yaml", "/config/secrets.yaml"):
                with self.subTest(identifier=identifier):
                    with self.assertRaises(SessionError):
                        await self.manager.async_session_detail(identifier)
                    with self.assertRaises(SessionError):
                        await self.manager.async_session_download(identifier, "fit")
            for file_format in ("../fit", "yaml", "/config/secrets.yaml"):
                with self.subTest(file_format=file_format), self.assertRaises(SessionError):
                    await self.manager.async_session_download(session_id, file_format)

    async def test_tampered_fit_is_visible_as_an_error_and_cannot_be_downloaded(self):
        """A failed FIT checksum keeps the source accessible without serving changed bytes."""
        session_id = await self.finish_walk()
        _, fit_path = self.manager._paths(self.manager.sessions[session_id])
        fit_path.write_bytes(b"changed FIT contents")
        detail = await self.manager.async_session_detail(session_id)
        self.assertIsNone(detail["fit_summary"])
        self.assertIn("validation", detail["fit_error"])
        self.assertEqual(detail["recording"]["steps"], 16)
        self.assertIsNone(detail["recording_error"])
        with self.assertRaises(SessionError):
            await self.manager.async_session_download(session_id, "fit")
        self.assertEqual(self.hass.services.calls, [])

    async def test_missing_source_retains_fit_and_summary_inspection(self):
        """A lost JSON file does not hide a valid FIT or the saved session summary."""
        session_id = await self.finish_walk()
        json_path, fit_path = self.manager._paths(self.manager.sessions[session_id])
        original_fit = fit_path.read_bytes()
        json_path.unlink()
        detail = await self.manager.async_session_detail(session_id)
        self.assertEqual(detail["session"]["session_id"], session_id)
        self.assertEqual(detail["session"]["steps"], 16)
        self.assertEqual(detail["fit_summary"]["steps"], 16)
        self.assertIsNone(detail["fit_error"])
        self.assertIn("source recording", detail["recording_error"])
        self.assertEqual(detail["recording"]["samples"], [])
        downloaded = await self.manager.async_session_download(session_id, "fit")
        self.assertEqual(base64.b64decode(downloaded["content"]), original_fit)
        with self.assertRaises(SessionError):
            await self.manager.async_session_download(session_id, "json")


if __name__ == "__main__":
    unittest.main()
