"""Pure, restartable session recording from physical WalkingPad status."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from typing import Any
from uuid import uuid4


PAUSE_SECONDS = 120.0
NO_DATA_SECONDS = 120.0
FRESH_BASELINE_SECONDS = 15.0


@dataclass(frozen=True)
class StatusSample:
    """One immutable device observation in UTC and physical units."""

    timestamp: float
    duration_s: int
    distance_m: float
    steps: int
    speed_mps: float

    def __post_init__(self) -> None:
        """Reject invalid counters before they can damage a saved session."""
        values = (
            self.timestamp,
            self.duration_s,
            self.distance_m,
            self.steps,
            self.speed_mps,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("Status values must be finite and nonnegative")


class SessionRecorder:
    """Accumulate walking segments, retaining enough state for recovery."""

    def __init__(
        self,
        entry_id: str,
        device_id: str,
        name: str,
        restored: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a recorder, optionally recovering an unfinished session."""
        self.entry_id = entry_id
        self.device_id = device_id
        self.name = name
        restored = restored or {}
        self.active: dict[str, Any] | None = deepcopy(restored.get("active"))
        previous = restored.get("last_sample")
        self._last_sample = StatusSample(**previous) if previous is not None else None
        self._paused_at: float | None = restored.get("paused_at")
        if self.active is None:
            self._paused_at = None
        elif self._last_sample is None:
            raise ValueError("An active session requires its last device observation")

    @property
    def state(self) -> str:
        """Return the current live recording state."""
        if self.active is None:
            return "idle"
        return "paused" if self._paused_at is not None else "recording"

    @property
    def last_sample(self) -> StatusSample | None:
        """Return the last immutable observation, including while stopped."""
        return self._last_sample

    @property
    def next_deadline(self) -> float | None:
        """Return the next timestamp at which an active session must finish."""
        if self.active is None or self._last_sample is None:
            return None
        if self._paused_at is not None:
            return self._paused_at + PAUSE_SECONDS
        return self._last_sample.timestamp + NO_DATA_SECONDS

    def to_dict(self) -> dict[str, Any]:
        """Return an independent JSON-compatible checkpoint."""
        return {
            "active": deepcopy(self.active),
            "last_sample": asdict(self._last_sample) if self._last_sample else None,
            "paused_at": self._paused_at,
        }

    def receive(self, sample: StatusSample) -> list[dict[str, Any]]:
        """Consume a fresh observation and return any newly completed session."""
        previous = self._last_sample
        if previous is not None and sample.timestamp <= previous.timestamp:
            return []
        completed = self.expire(sample.timestamp)
        previous = self._last_sample
        if self.active is None:
            if sample.speed_mps > 0:
                self._start(sample, previous)
            self._last_sample = sample
            return completed

        self.active["device_id"] = self.device_id
        self.active["name"] = self.name
        if previous is not None:
            if (
                sample.timestamp - previous.timestamp > FRESH_BASELINE_SECONDS
                and (sample.speed_mps > 0 or previous.speed_mps > 0)
            ):
                self._flag("data_gap", partial=True)
            if sample.speed_mps > 0 or self._paused_at is None:
                self._advance(sample, previous)

        if sample.speed_mps == 0:
            if self._paused_at is None:
                self._paused_at = sample.timestamp
                self._timer("stop", sample.timestamp)
                self._append_sample(sample, 0.0)
        else:
            if self._paused_at is not None:
                # _advance has calculated only the new moving counter delta.
                # Use that delta to position the resume within the last poll.
                elapsed = sample.timestamp - previous.timestamp if previous else 0
                duration = self._counter_delta(sample.duration_s, previous.duration_s) if previous else 0
                resumed_at = sample.timestamp - min(float(duration), elapsed)
                self._timer("start", max(self._paused_at, resumed_at))
                self._paused_at = None
            self._append_sample(sample, self.active.pop("_sample_cadence", 0.0))

        self._last_sample = sample
        return completed

    def expire(self, now: float) -> list[dict[str, Any]]:
        """Finish a confirmed stop or an interrupted session when due."""
        if self.active is None or self._last_sample is None:
            return []
        if self._paused_at is not None:
            if now >= self._paused_at + PAUSE_SECONDS:
                return [self._finish(self._paused_at)]
        elif now >= self._last_sample.timestamp + NO_DATA_SECONDS:
            self._flag("connection_lost", partial=True)
            result = self._finish(self._last_sample.timestamp)
            # The next observation must establish a new counter baseline.
            self._last_sample = None
            return [result]
        return []

    def _start(self, sample: StatusSample, previous: StatusSample | None) -> None:
        """Start with a recent stopped baseline or mark the observed portion partial."""
        known_start = (
            previous is not None
            and previous.speed_mps == 0
            and sample.timestamp - previous.timestamp <= FRESH_BASELINE_SECONDS
        )
        duration = 0.0
        if known_start:
            duration = min(
                float(self._counter_delta(sample.duration_s, previous.duration_s)),
                sample.timestamp - previous.timestamp,
            )
        start_time = sample.timestamp - duration
        self.active = {
            "session_id": str(uuid4()),
            "entry_id": self.entry_id,
            "device_id": self.device_id,
            "name": self.name,
            "start_time": start_time,
            "end_time": sample.timestamp,
            "elapsed_seconds": duration,
            "moving_seconds": 0.0,
            "distance_m": 0.0,
            "steps": 0,
            "partial": not known_start,
            "quality_flags": [] if known_start else ["started_mid_walk"],
            "samples": [],
            "timer_events": [{"timestamp": start_time, "event": "start"}],
        }
        self._paused_at = None
        if known_start:
            self._advance(sample, previous, starting=True)
        self._append_sample(sample, self.active.pop("_sample_cadence", 0.0))

    @staticmethod
    def _counter_delta(current: float, previous: float) -> float:
        """Count a reset counter as the beginning of a fresh device epoch."""
        return current - previous if current >= previous else current

    def _advance(
        self, sample: StatusSample, previous: StatusSample, starting: bool = False
    ) -> None:
        """Accumulate source counter differences without including a stopped pause."""
        if self.active is None:
            return
        interval = sample.timestamp - previous.timestamp
        duration = float(self._counter_delta(sample.duration_s, previous.duration_s))
        distance = self._counter_delta(sample.distance_m, previous.distance_m)
        steps = int(self._counter_delta(sample.steps, previous.steps))
        reset = (
            sample.duration_s < previous.duration_s
            or sample.distance_m < previous.distance_m
            or sample.steps < previous.steps
        )
        if reset:
            self._flag("counter_reset", partial=self._paused_at is None and not starting)
        if duration > interval + 1:
            self._flag("counter_jump", partial=True)
        duration = min(duration, interval)
        self.active["moving_seconds"] += duration
        self.active["distance_m"] += distance
        self.active["steps"] += steps
        self.active["_sample_cadence"] = steps * 60 / duration if duration else 0.0
        self.active["end_time"] = sample.timestamp
        self.active["elapsed_seconds"] = sample.timestamp - self.active["start_time"]

    def _append_sample(self, sample: StatusSample, cadence: float) -> None:
        """Store the observed speed and session-relative cumulative counters."""
        if self.active is None:
            return
        self.active["samples"].append(
            {
                "timestamp": sample.timestamp,
                "distance_m": self.active["distance_m"],
                "speed_mps": sample.speed_mps,
                "cadence_spm": cadence,
                "steps": self.active["steps"],
            }
        )

    def _timer(self, event: str, timestamp: float) -> None:
        """Add a timer boundary without duplicating an identical event."""
        if self.active is None:
            return
        timer = {"timestamp": timestamp, "event": event}
        if not self.active["timer_events"] or self.active["timer_events"][-1] != timer:
            self.active["timer_events"].append(timer)

    def _flag(self, flag: str, partial: bool = False) -> None:
        """Record a quality issue once."""
        if self.active is None:
            return
        if flag not in self.active["quality_flags"]:
            self.active["quality_flags"].append(flag)
        self.active["partial"] = self.active["partial"] or partial

    def _finish(self, end_time: float) -> dict[str, Any]:
        """Detach a completed immutable snapshot, preserving its real endpoint."""
        self._timer("stop", end_time)
        session = deepcopy(self.active)
        session.pop("_sample_cadence", None)
        session["end_time"] = end_time
        session["elapsed_seconds"] = max(0.0, end_time - session["start_time"])
        session["moving_seconds"] = min(session["moving_seconds"], session["elapsed_seconds"])
        session["device_id"] = self.device_id
        session["name"] = self.name
        session["samples"] = [sample for sample in session["samples"] if sample["timestamp"] <= end_time]
        session["timer_events"] = [event for event in session["timer_events"] if event["timestamp"] <= end_time]
        self.active = None
        self._paused_at = None
        return session
