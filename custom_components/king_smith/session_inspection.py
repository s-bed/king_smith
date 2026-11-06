"""Explain recording quality and inspect the actual archived FIT activity."""

from datetime import datetime
import math

from garmin_fit_sdk import Decoder, FIT_EPOCH_S, Stream


_QUALITY = {
    "started_mid_walk": (
        "Recording began while the belt was moving",
        "No recent stopped observation established the beginning of this walk. "
        "Walking before the first recorded sample is not included.",
    ),
    "data_gap": (
        "Gaps between Bluetooth samples",
        "Some Bluetooth samples were not received. Cumulative duration, distance "
        "and step totals may still be complete, but graph coverage is reduced.",
    ),
    "counter_jump": (
        "Device time advanced unexpectedly",
        "The device duration increased beyond the observed time interval. "
        "Recorded duration was limited to avoid adding unobserved time.",
    ),
    "connection_lost": (
        "Recording ended after device data stopped",
        "No WalkingPad status arrived for two minutes. This session ends at "
        "the last received sample; any later walking is not included.",
    ),
    "counter_reset": (
        "The treadmill counters restarted",
        "Device duration, distance or step counters reset during the recording. "
        "The recorder accumulated measurements across the reset.",
    ),
}


def quality_details(session: dict) -> list[dict]:
    """Translate saved reasons without claiming which unmeasured metrics are absent."""
    partial = bool(session.get("partial"))
    flags = session.get("quality_flags") or []
    if isinstance(flags, str):
        flags = [flags]
    result = []
    seen = set()
    for flag in flags:
        code = str(flag)
        if code in seen:
            continue
        seen.add(code)
        title, description = _QUALITY.get(code, (
            "Recording quality note", f"The recorder reported quality flag: {code}.",
        ))
        severity = "warning" if code != "counter_reset" or partial else "info"
        if code not in _QUALITY:
            severity = "warning" if partial else "info"
        if code == "counter_reset" and partial:
            description += " This recording is marked partial; some totals may be incomplete."
        result.append({
            "code": code, "title": title, "description": description,
            "severity": severity,
        })
    if partial and not result:
        result.append({
            "code": "partial_unspecified", "title": "Recording marked partial",
            "description": "This recording was marked partial without a saved reason. "
            "The missing information cannot be identified from its summary.",
            "severity": "warning",
        })
    return result


def _number(message: dict, *names: str) -> float | None:
    """Read the first present finite decoded field, retaining absent measurements."""
    for name in names:
        value = message.get(name)
        if value is not None:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"Invalid decoded FIT field: {name}")
            return number
    return None


def _epoch(value) -> float | None:
    """Convert decoded FIT dates or raw FIT epoch seconds into UTC Unix time."""
    if isinstance(value, datetime):
        return value.timestamp()
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Invalid decoded FIT timestamp")
    return number + FIT_EPOCH_S


def _cadence(message: dict, prefix: str) -> float | None:
    """Convert walking FIT stride cadence, including fractional cycles, to steps/min."""
    whole = _number(message, f"{prefix}_cadence", f"{prefix}_running_cadence")
    if whole is None:
        return None
    fraction = _number(message, f"{prefix}_fractional_cadence") or 0.0
    return 2 * (whole + fraction)


def inspect_fit(contents: bytes) -> dict:
    """Decode an archived FIT file and report its encoded walking measurements.

    This reads the original bytes, including their CRC, rather than regenerating
    a file from the JSON recording. Missing measurements remain null.
    """
    try:
        decoder = Decoder(Stream.from_byte_array(contents))
        if not decoder.is_fit() or not decoder.check_integrity():
            raise ValueError("FIT integrity check failed")
        messages, errors = Decoder(Stream.from_byte_array(contents)).read()
        if errors:
            raise ValueError("FIT decoding failed")
        sessions = messages.get("session_mesgs", [])
        if len(sessions) != 1:
            raise ValueError("Expected one recorded WalkingPad session in the FIT file")
        session = sessions[0]
        cycles = _number(session, "total_cycles", "total_strides")
        fractional_cycles = _number(session, "total_fractional_cycles") or 0.0
        steps = 2 * (cycles + fractional_cycles) if cycles is not None else None
        if steps is not None and steps.is_integer():
            steps = int(steps)
        return {
            "integrity_ok": True,
            "distance_m": _number(session, "total_distance"),
            "timer_seconds": _number(session, "total_timer_time"),
            "elapsed_seconds": _number(session, "total_elapsed_time"),
            "steps": steps,
            "avg_cadence_spm": _cadence(session, "avg"),
            "max_cadence_spm": _cadence(session, "max"),
            "avg_speed_mps": _number(session, "enhanced_avg_speed", "avg_speed"),
            "max_speed_mps": _number(session, "enhanced_max_speed", "max_speed"),
            "start_time": _epoch(session.get("start_time")),
            "end_time": _epoch(session.get("timestamp")),
            "record_count": len(messages.get("record_mesgs", [])),
            "lap_count": len(messages.get("lap_mesgs", [])),
            "sport": session.get("sport"),
            "sub_sport": session.get("sub_sport"),
        }
    except ValueError:
        raise
    except Exception as err:
        raise ValueError("Could not decode the archived FIT activity") from err
