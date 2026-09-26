from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .models import BackendConfig, SafetyConfig


def load_config(path: str) -> BackendConfig:
    source = Path(path).resolve()
    raw: Dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    uav_ids = [int(value) for value in raw["uav_ids"]]
    if sorted(uav_ids) != [1, 2, 3, 4, 5, 6]:
        raise ValueError("uav_ids must contain each ID from 1 through 6 exactly once")

    altitude_values = raw["takeoff_altitudes_m"]
    altitudes = {int(key): float(value) for key, value in altitude_values.items()}
    if set(altitudes) != set(uav_ids):
        raise ValueError("takeoff_altitudes_m must define all six UAV IDs")
    if any(value <= 0.0 for value in altitudes.values()):
        raise ValueError("all takeoff altitudes must be positive")
    if len(set(altitudes.values())) != len(altitudes):
        raise ValueError("takeoff altitudes must be different for all six UAVs")

    safety_raw = raw["safety"]
    safety = SafetyConfig(
        production_config_confirmed=bool(safety_raw["production_config_confirmed"]),
        require_armed_for_takeoff=bool(safety_raw["require_armed_for_takeoff"]),
        required_control_state=int(safety_raw["required_control_state"]),
        preflight_battery_min=float(safety_raw["preflight_battery_min"]),
        return_battery_threshold=float(safety_raw["return_battery_threshold"]),
        telemetry_max_age_seconds=float(safety_raw["telemetry_max_age_seconds"]),
        altitude_tolerance_m=float(safety_raw["altitude_tolerance_m"]),
        vertical_speed_tolerance_mps=float(safety_raw["vertical_speed_tolerance_mps"]),
        takeoff_timeout_seconds=float(safety_raw["takeoff_timeout_seconds"]),
        confirmation_ttl_seconds=float(safety_raw["confirmation_ttl_seconds"]),
    )
    if not 0.0 < safety.return_battery_threshold < safety.preflight_battery_min <= 1.0:
        raise ValueError("battery thresholds must satisfy 0 < return < preflight <= 1")

    return BackendConfig(
        uav_ids=uav_ids,
        takeoff_altitudes_m=altitudes,
        safety=safety,
        subjects=dict(raw["subjects"]),
    )

