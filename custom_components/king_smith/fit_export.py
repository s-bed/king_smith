"""Encode recorded indoor walks using Garmin's official FIT SDK."""

from datetime import datetime, timezone
import math
from uuid import UUID
from zoneinfo import ZoneInfo

from garmin_fit_sdk import Encoder, FIT_EPOCH_S, Profile


def _number(value, name: str) -> float:
    """Require a finite, nonnegative physical measurement."""
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {name}: {value}")
    return result


def _timestamp(value) -> int:
    """Convert a UTC Unix timestamp to whole FIT epoch seconds."""
    result = int(_number(value, "timestamp")) - FIT_EPOCH_S
    if not 0 <= result < 0xFFFFFFFF:
        raise ValueError("Timestamp is outside the FIT date range")
    return result


def _cadence(steps_per_minute) -> tuple[int, float]:
    """Convert both-foot step cadence into whole and fractional stride cycles."""
    units = round(_number(steps_per_minute, "cadence") * 64)
    whole, fraction = divmod(units, 128)
    if whole >= 255:
        raise ValueError("Cadence is outside the FIT field range")
    return whole, fraction / 128


def encode_session(session: dict, time_zone: str) -> bytes:
    """Return a deterministic FIT activity containing the observed walking data.

    Session and sample distances are metres and speeds are metres per second.
    WalkingPad counts both feet, while FIT walking cadence and cycles count
    strides; fractional fields retain odd step totals and fractional cadence.
    """
    start = _timestamp(session["start_time"])
    end = _timestamp(session["end_time"])
    if end < start:
        raise ValueError("Session ends before it starts")
    elapsed = _number(session["elapsed_seconds"], "elapsed time")
    moving = _number(session["moving_seconds"], "moving time")
    if moving > elapsed + 0.001:
        raise ValueError("Moving time exceeds elapsed time")
    distance = _number(session["distance_m"], "distance")
    step_count = _number(session["steps"], "steps")
    if not step_count.is_integer():
        raise ValueError("Step total must be an integer")
    steps = int(step_count)
    serial = UUID(session["session_id"]).int % 0xFFFFFFFE + 1
    encoder = Encoder()

    def write(kind, **fields):
        encoder.write_mesg({"mesg_num": Profile["mesg_num"][kind], **fields})

    write(
        "FILE_ID", type="activity", manufacturer="development", product=0,
        time_created=start, serial_number=serial,
    )
    write(
        "DEVICE_INFO", device_index="creator", manufacturer="development",
        product=0, product_name="King Smith WalkingPad", serial_number=serial,
        timestamp=start,
    )
    timeline = []
    samples = session.get("samples", [])
    maximum_speed = 0.0
    maximum_cadence = 0.0
    last_distance = 0.0
    for sample in sorted(samples, key=lambda item: item["timestamp"]):
        timestamp = _timestamp(sample["timestamp"])
        if not start <= timestamp <= end:
            continue
        sample_distance = _number(sample["distance_m"], "sample distance")
        if sample_distance < last_distance or sample_distance > distance + 0.01:
            raise ValueError("Sample distance is not a valid cumulative session distance")
        last_distance = sample_distance
        speed = _number(sample["speed_mps"], "speed")
        cadence_spm = _number(sample.get("cadence_spm", 0), "cadence")
        cadence, fractional_cadence = _cadence(cadence_spm)
        maximum_speed = max(maximum_speed, speed)
        maximum_cadence = max(maximum_cadence, cadence_spm)
        timeline.append((timestamp, 1, "RECORD", {
            "timestamp": timestamp, "distance": sample_distance,
            "enhanced_speed": speed, "cadence": cadence,
            "fractional_cadence": fractional_cadence,
        }))
    if not timeline:
        raise ValueError("Session has no observed samples within its timestamps")
    events = [
        event for event in session.get("timer_events", [])
        if start <= _timestamp(event["timestamp"]) <= end
    ]
    if not events or events[0]["event"] != "start":
        events.insert(0, {"timestamp": session["start_time"], "event": "start"})
    if events[-1]["event"] != "stop":
        events.append({"timestamp": session["end_time"], "event": "stop"})
    for event in events:
        kind = event["event"]
        if kind not in {"start", "stop"}:
            raise ValueError("Unsupported timer event")
        timestamp = _timestamp(event["timestamp"])
        timeline.append((timestamp, 0 if kind == "start" else 2, "EVENT", {
            "timestamp": timestamp, "event": "timer", "event_type": kind,
        }))
    for _, _, kind, fields in sorted(timeline, key=lambda row: (row[0], row[1])):
        write(kind, **fields)

    average_spm = steps * 60 / moving if moving else 0.0
    avg_cadence, avg_fraction = _cadence(average_spm)
    max_cadence, max_fraction = _cadence(maximum_cadence)
    summary = {
        "message_index": 0, "timestamp": end, "start_time": start,
        "total_elapsed_time": elapsed, "total_timer_time": moving,
        "total_distance": distance, "total_cycles": steps // 2,
        "total_fractional_cycles": (steps % 2) / 2,
        "sport": "walking", "sub_sport": "indoor_walking",
        "enhanced_avg_speed": distance / moving if moving else 0.0,
        "enhanced_max_speed": maximum_speed,
        "avg_cadence": avg_cadence, "avg_fractional_cadence": avg_fraction,
        "max_cadence": max_cadence, "max_fractional_cadence": max_fraction,
    }
    write("LAP", **summary)
    write("SESSION", **summary, first_lap_index=0, num_laps=1)
    offset = datetime.fromtimestamp(
        session["end_time"], timezone.utc
    ).astimezone(ZoneInfo(time_zone)).utcoffset().total_seconds()
    write(
        "ACTIVITY", timestamp=end, num_sessions=1, type="manual",
        total_timer_time=moving, local_timestamp=end + int(offset),
    )
    return bytes(encoder.close())
