"""Validate read-only Pengfei telemetry before exposing it to ground clients."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional


_FIELDS = {
    "current_target": {
        "strings": ("target_id", "target_type"),
        "floats": (
            "latitude_deg", "longitude_deg", "altitude_gps_m",
            "velocity_north_mps", "velocity_east_mps",
        ),
    },
    "last_recognition": {
        "strings": ("target_id", "target_type"),
        "floats": ("latitude_deg", "longitude_deg"),
    },
    "node_status": {
        "strings": ("scheduler_state", "maneuver_state", "gimbal_state", "follower_state"),
        "floats": (),
    },
}


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def sanitize_pengfei(value: Any) -> Dict[str, Any]:
    """Keep only the documented fields; invalid ROS NaNs become JSON nulls."""
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {"sent_at_unix": _finite(value.get("sent_at_unix"))}
    for name, spec in _FIELDS.items():
        source = value.get(name)
        if not isinstance(source, dict):
            continue
        block: Dict[str, Any] = {
            field: str(source.get(field) or "")[:128] for field in spec["strings"]
        }
        for field in spec["floats"]:
            number = _finite(source.get(field))
            if field == "latitude_deg" and number is not None and abs(number) > 90:
                number = None
            if field == "longitude_deg" and number is not None and abs(number) > 180:
                number = None
            block[field] = number
        block["received_at_unix"] = _finite(source.get("received_at_unix"))
        if name == "node_status":
            point = source.get("scan_point_number")
            block["scan_point_number"] = (
                point if type(point) is int and 0 <= point <= 1000000 else 0
            )
        result[name] = block
    return result if len(result) > 1 else {}
