"""Durable session storage and explicitly approved Garmin uploads."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from uuid import UUID

from .fit_export import encode_session
from .session_recorder import SessionRecorder, StatusSample
from .session_inspection import inspect_fit, quality_details

_LOGGER = logging.getLogger(__name__)
RESOLVED = {"sent", "discarded"}
RETENTION_SECONDS = 90 * 86400


class SessionError(ValueError):
    """A session action cannot be performed in its current state."""


class SessionManager:
    """Own storage, lifecycle deadlines, and one account-aware upload worker."""

    def __init__(self, hass, store, account_ready: Callable[[str], bool]) -> None:
        """Create a manager with injectable HA storage and account validation."""
        self.hass = hass
        self.store = store
        self.account_ready = account_ready
        self.recorders: dict[str, SessionRecorder] = {}
        self.sessions: dict[str, dict] = {}
        self.pending_archives: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._closing = False
        self._listeners: dict[str, list[Callable]] = {}
        self._last_cleanup = 0.0
        self._events: list[tuple[str, dict]] = []
        self.last_error: str | None = None

    async def async_initialize(self) -> None:
        """Restore sessions before receiving packets or dispatching uploads."""
        data = await self.store.async_load() or {}
        self.sessions = data.get("sessions", {})
        self.pending_archives = data.get("pending_archives", {})
        for entry_id, saved in data.get("recorders", {}).items():
            self.recorders[entry_id] = SessionRecorder(
                entry_id, saved["device_id"], saved["name"], saved["data"]
            )
        for session in self.sessions.values():
            if session["status"] == "uploading":
                session["status"] = "needs_review"
                session["error"] = "Upload was interrupted. Check Garmin before retrying."
        await self.async_tick()
        self.hass.bus.async_fire("king_smith_recorder_ready")

    def attach(self, entry_id: str, device_id: str, name: str) -> None:
        """Attach a device to an existing or newly restored recorder."""
        if entry_id not in self.recorders:
            self.recorders[entry_id] = SessionRecorder(entry_id, device_id, name)
        recorder = self.recorders[entry_id]
        recorder.device_id = device_id
        recorder.name = name
        if recorder.active is not None:
            recorder.active["device_id"] = device_id

    def subscribe(self, entry_id: str, callback: Callable) -> Callable:
        """Subscribe a recorder sensor to local state changes."""
        listeners = self._listeners.setdefault(entry_id, [])
        listeners.append(callback)
        return lambda: listeners.remove(callback)

    def recorder_state(self, entry_id: str) -> str:
        """Return the current recording state independent of Bluetooth health."""
        recorder = self.recorders.get(entry_id)
        return recorder.state if recorder else "idle"

    def pending_count(self, entry_id: str) -> int:
        """Count unresolved recordings for one pad."""
        return sum(
            s["entry_id"] == entry_id and s["status"] not in RESOLVED
            for s in self.sessions.values()
        )

    def list_sessions(self, device_id: str, status: str | None = None) -> list[dict]:
        """Return summaries without exposing mutable internal records."""
        return sorted(
            [self._public_summary(s) for s in self.sessions.values()
             if s["device_id"] == device_id and (status is None or s["status"] == status)],
            key=lambda s: s["start_time"], reverse=True,
        )

    def list_all_sessions(self) -> list[dict]:
        """Return account-wide summaries for the administrator session browser."""
        return sorted([self._public_summary(s) for s in self.sessions.values()], key=lambda s: s["start_time"], reverse=True)

    @staticmethod
    def _public_summary(session: dict) -> dict:
        result = deepcopy(session)
        result["quality_notes"] = [detail["description"] for detail in quality_details(session)]
        return result

    def list_devices(self) -> list[dict]:
        """List live and restored recorders without requiring a connected pad."""
        return [{
            "entry_id": entry_id, "device_id": recorder.device_id,
            "name": recorder.name, "recorder_state": recorder.state,
            "pending_sessions": self.pending_count(entry_id),
        } for entry_id, recorder in self.recorders.items()]

    async def async_session_detail(self, session_id: str) -> dict:
        """Inspect the archived source and actual FIT bytes without rewriting them."""
        async with self._lock:
            session = deepcopy(self._session_by_id(session_id))
            source = deepcopy(self.pending_archives.get(session_id))
        recording_error = None
        if source is None:
            try:
                source = await self.hass.async_add_executor_job(self._read_source, session)
            except (OSError, ValueError):
                source = {"samples": [], "timer_events": []}
                recording_error = "The saved source recording is unavailable or invalid."
        fit_summary, fit_error = None, None
        try:
            fit_summary = await self.hass.async_add_executor_job(self._inspect_archive, session)
        except (OSError, ValueError):
            fit_error = "The saved FIT file is unavailable or failed validation."
        return {
            "session": session, "recording": source,
            "quality_details": quality_details(session),
            "fit_summary": fit_summary, "fit_error": fit_error,
            "recording_error": recording_error,
        }

    async def async_session_download(self, session_id: str, file_format: str) -> dict:
        """Return an existing session file only through an authenticated request."""
        if file_format not in {"fit", "json"}:
            raise SessionError("Choose FIT or JSON.")
        async with self._lock:
            session = deepcopy(self._session_by_id(session_id))
        try:
            return await self.hass.async_add_executor_job(self._download_archive, session, file_format)
        except (OSError, ValueError) as err:
            raise SessionError("The saved activity file is unavailable or invalid.") from err

    def _session_by_id(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError("Saved WalkingPad session not found.")
        return session

    def _inspect_archive(self, session: dict) -> dict:
        encoded = self._paths(session)[1].read_bytes()
        if hashlib.sha256(encoded).hexdigest() != session.get("fit_sha256"):
            raise ValueError("Saved activity no longer matches its recorded checksum")
        return inspect_fit(encoded)

    def _download_archive(self, session: dict, file_format: str) -> dict:
        json_path, fit_path = self._paths(session)
        encoded = (fit_path if file_format == "fit" else json_path).read_bytes()
        if file_format == "fit":
            if hashlib.sha256(encoded).hexdigest() != session.get("fit_sha256"):
                raise ValueError("Saved activity no longer matches its recorded checksum")
        else:
            self._read_source(session)
        return {
            "filename": f"walkingpad-{session['session_id']}.{file_format}",
            "mime_type": "application/octet-stream" if file_format == "fit" else "application/json",
            "content": base64.b64encode(encoded).decode("ascii"),
        }

    async def async_receive(self, entry_id: str, sample: StatusSample) -> None:
        """Persist one immutable snapshot in arrival order."""
        if self._closing:
            return
        async with self._lock:
            recorder = self.recorders[entry_id]
            previous_id = recorder.active["session_id"] if recorder.active else None
            completed = recorder.receive(sample)
            if recorder.active and recorder.active["session_id"] != previous_id:
                self._events.append(("king_smith_session_start", {
                    "device_id": recorder.device_id, "entry_id": entry_id,
                    "domain": "king_smith", "type": "session_start",
                    "session_id": recorder.active["session_id"],
                    "start_time": recorder.active["start_time"],
                }))
            for session in completed:
                await self._async_complete(session)
            await self._async_save()
            self._notify(entry_id)

    async def async_tick(self, _now=None) -> None:
        """Rehydrate deadlines, flush archives, and retry only preflight blocks."""
        if self._closing:
            return
        try:
            async with self._lock:
                now = time.time()
                for recorder in self.recorders.values():
                    for session in recorder.expire(now):
                        await self._async_complete(session)
                for session_id in list(self.pending_archives):
                    await self._async_archive(session_id)
                if now - self._last_cleanup >= 86400:
                    await self._async_cleanup(now)
                    self._last_cleanup = now
                await self._async_save()
                for entry_id in self.recorders:
                    self._notify(entry_id)
            self._kick_worker()
        except Exception:
            self.last_error = "Session storage could not be saved; uploads are held."
            _LOGGER.exception("Could not checkpoint WalkingPad sessions")

    async def async_upload(
        self, device_id: str, session_id: str, garmin_entity_id: str, retry: bool = False
    ) -> dict:
        """Durably queue a user-approved upload; do not dispatch before saving."""
        async with self._lock:
            session = self._get_session(device_id, session_id)
            if session["status"] in RESOLVED | {"queued", "uploading"}:
                return deepcopy(session)
            if session["status"] == "blocked" and session.get("garmin_entity_id") == garmin_entity_id:
                return deepcopy(session)
            if session["status"] == "needs_review" and not retry:
                raise SessionError("Check Garmin first, then explicitly request a retry.")
            if not garmin_entity_id.startswith("sensor."):
                raise SessionError("Select a Garmin Connect sensor for the account.")
            if session["status"] == "failed_local" and session_id not in self.pending_archives:
                try:
                    self.pending_archives[session_id] = await self.hass.async_add_executor_job(
                        self._read_source, session
                    )
                except (OSError, ValueError) as err:
                    raise SessionError("Local source recording could not be recovered.") from err
            if session_id in self.pending_archives:
                await self._async_archive(session_id)
            if session["status"] == "failed_local":
                await self._async_save()
                raise SessionError(session["error"])
            session.update(status="queued", garmin_entity_id=garmin_entity_id, error=None)
            self._events.append(("king_smith_session_updated", deepcopy(session)))
            await self._async_save()
            result = deepcopy(session)
            self._notify(session["entry_id"])
        self._kick_worker()
        return result

    async def async_discard(self, device_id: str, session_id: str) -> dict:
        """Keep a recording locally while declining its Garmin upload."""
        async with self._lock:
            session = self._get_session(device_id, session_id)
            if session["status"] == "uploading":
                raise SessionError("This activity is already being sent; it cannot be cancelled.")
            if session["status"] not in RESOLVED:
                session.update(status="discarded", resolved_at=time.time(), error=None)
                self._events.append(("king_smith_session_updated", deepcopy(session)))
                await self._async_save()
                self._notify(session["entry_id"])
            return deepcopy(session)

    async def async_shutdown(self) -> None:
        """Flush active sessions and conservatively hold interrupted uploads."""
        self._closing = True
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
        async with self._lock:
            await self._async_save()

    async def _async_complete(self, session: dict) -> None:
        session_id = session["session_id"]
        self.pending_archives[session_id] = deepcopy(session)
        self.sessions[session_id] = {
            key: deepcopy(value) for key, value in session.items()
            if key not in {"samples", "timer_events"}
        }
        self.sessions[session_id].update(status="awaiting_review", attempts=0, error=None)
        # Preserve the entire source before the engine forgets the completed walk.
        await self._async_save()
        await self._async_archive(session_id)
        summary = self.sessions[session_id]
        self._events.append(("king_smith_session_ready", deepcopy(summary)))
        self._events.append(("king_smith_session_done", {
            **deepcopy(summary), "domain": "king_smith", "type": "session_done",
            "duration": summary["moving_seconds"], "dist": summary["distance_m"] / 1000,
            "avg_step_cadence": summary["steps"] * 60 / max(summary["moving_seconds"], 1),
            "avg_speed": summary["distance_m"] * 3.6 / max(summary["moving_seconds"], 1),
        }))

    async def _async_archive(self, session_id: str) -> None:
        session = self.sessions[session_id]
        try:
            digest = await self.hass.async_add_executor_job(
                self._write_archive, self.pending_archives[session_id], self.hass.config.time_zone
            )
        except Exception:
            if session["status"] not in RESOLVED:
                session.update(status="failed_local", error="Could not save the activity files. Recording retained in Home Assistant storage.")
            return
        self.pending_archives.pop(session_id)
        session["fit_sha256"] = digest
        if session["status"] == "failed_local":
            session.update(status="awaiting_review", error=None)
            self._events.append(("king_smith_session_updated", deepcopy(session)))

    def _paths(self, session: dict) -> tuple[Path, Path]:
        entry_id = session["entry_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", entry_id):
            raise SessionError("Invalid recorder identifier.")
        session_id = str(UUID(session["session_id"]))
        directory = Path(self.hass.config.path("walkingpad_sessions", entry_id))
        return directory / f"{session_id}.json", directory / f"{session_id}.fit"

    def _write_archive(self, session: dict, time_zone: str) -> str:
        json_path, fit_path = self._paths(session)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        source = json.dumps({"version": 1, **session}, allow_nan=False).encode()
        encoded = encode_session(session, time_zone)
        for path, contents in ((json_path, source), (fit_path, encoded)):
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(contents)
            temporary.replace(path)
        return hashlib.sha256(encoded).hexdigest()

    def _read_source(self, session: dict) -> dict:
        source = json.loads(self._paths(session)[0].read_text(encoding="utf-8"))
        if source.get("version") != 1 or any(
            source.get(key) != session[key] for key in ("session_id", "entry_id", "device_id")
        ):
            raise ValueError("Recording identifiers do not match the session index")
        source.pop("version")
        return source

    async def _async_save(self) -> None:
        try:
            await self.store.async_save({
                "recorders": {entry_id: {
                    "device_id": r.device_id, "name": r.name, "data": r.to_dict()
                } for entry_id, r in self.recorders.items()},
                "sessions": self.sessions, "pending_archives": self.pending_archives,
            })
        except Exception:
            self.last_error = "Session storage could not be saved; uploads are held."
            for entry_id in self.recorders:
                self._notify(entry_id)
            raise
        self.last_error = None
        events, self._events = self._events, []
        for name, data in events:
            self.hass.bus.async_fire(name, data)

    def _get_session(self, device_id: str, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if session is None or session["device_id"] != device_id:
            raise SessionError("Session not found for this WalkingPad.")
        return session

    def _notify(self, entry_id: str) -> None:
        for listener in list(self._listeners.get(entry_id, [])):
            listener()

    def _kick_worker(self) -> None:
        if not self._closing and (self._worker_task is None or self._worker_task.done()):
            self._worker_task = asyncio.create_task(self._async_upload_worker())
            self._worker_task.add_done_callback(self._worker_done)

    @staticmethod
    def _worker_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("WalkingPad upload worker stopped; persisted uploads are held", exc_info=task.exception())

    async def _async_upload_worker(self) -> None:
        examined: set[str] = set()
        while not self._closing:
            async with self._lock:
                session = next((s for s in self.sessions.values()
                                if s["status"] in {"queued", "blocked"}
                                and s["session_id"] not in examined), None)
                if session is None:
                    return
                examined.add(session["session_id"])
                if not self.hass.services.has_service("garmin_connect", "upload_activity") or not self.account_ready(session["garmin_entity_id"]):
                    if session["status"] != "blocked":
                        session.update(status="blocked", error="The selected Garmin account or upload action is unavailable. Upload approval is saved.")
                        self._events.append(("king_smith_session_updated", deepcopy(session)))
                        await self._async_save()
                    continue
                _, fit_path = self._paths(session)
                try:
                    valid = await self.hass.async_add_executor_job(
                        self._valid_fit, fit_path, session.get("fit_sha256")
                    )
                except OSError:
                    valid = False
                if not valid:
                    session.update(status="failed_local", error="Activity file is missing or changed. Recording remains local.")
                    self._events.append(("king_smith_session_updated", deepcopy(session)))
                    await self._async_save()
                    continue
                session.update(status="uploading", attempts=session["attempts"] + 1, last_attempt=time.time(), error=None)
                self._events.append(("king_smith_session_updated", deepcopy(session)))
                try:
                    await self._async_save()
                except Exception:
                    session.update(status="queued", error="Upload held until session storage recovers.")
                    self._events.clear()
                    raise
                arguments = {"file_path": str(fit_path), "entity_id": session["garmin_entity_id"]}
            try:
                # Do not cancel and retry an HTTP upload that may still be running.
                await self.hass.services.async_call("garmin_connect", "upload_activity", arguments, blocking=True)
            except asyncio.CancelledError:
                async with self._lock:
                    session.update(status="needs_review", error="Upload was interrupted. Check Garmin before retrying.")
                    self._events.append(("king_smith_session_updated", deepcopy(session)))
                    await self._async_save()
                raise
            except Exception:
                async with self._lock:
                    session.update(status="needs_review", error="Upload result is uncertain. Check Garmin before retrying.")
                    self._events.append(("king_smith_session_updated", deepcopy(session)))
                    await self._async_save()
            else:
                async with self._lock:
                    session.update(status="sent", resolved_at=time.time(), error=None)
                    self._events.append(("king_smith_session_updated", deepcopy(session)))
                    await self._async_save()
            self._notify(session["entry_id"])

    @staticmethod
    def _valid_fit(path: Path, digest: str | None) -> bool:
        return digest is not None and hashlib.sha256(path.read_bytes()).hexdigest() == digest

    async def _async_cleanup(self, now: float) -> None:
        for session_id, session in list(self.sessions.items()):
            if session["status"] in RESOLVED and now - session["resolved_at"] >= RETENTION_SECONDS:
                await self.hass.async_add_executor_job(self._delete_archive, session)
                self.sessions.pop(session_id)
                self.pending_archives.pop(session_id, None)

    def _delete_archive(self, session: dict) -> None:
        for path in self._paths(session):
            path.unlink(missing_ok=True)
