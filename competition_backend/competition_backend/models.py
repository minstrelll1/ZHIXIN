from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MissionPhase(str, Enum):
    IDLE = "idle"
    PLANNED = "planned"
    PREFLIGHT_READY = "preflight_ready"
    TAKING_OFF = "taking_off"
    RUNNING = "running"
    RETURNING = "returning"
    COMPLETED = "completed"
    FAILED = "failed"


class UavPhase(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    TAKEOFF_COMMANDED = "takeoff_commanded"
    AT_ALTITUDE = "at_altitude"
    EXECUTING = "executing"
    RETURN_COMMANDED = "return_commanded"
    LANDED = "landed"
    ERROR = "error"


class ReturnReason(str, Enum):
    TASK_COMPLETE = "task_complete"
    TIME_LIMIT = "time_limit"
    LOW_BATTERY = "low_battery"
    MANUAL = "manual"
    TAKEOFF_TIMEOUT = "takeoff_timeout"


@dataclass
class Telemetry:
    uav_id: int
    received_at: float
    connected: bool = False
    armed: bool = False
    odom_valid: bool = False
    failsafe: bool = False
    control_state: int = 0
    battery_percentage: float = 0.0
    position: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    velocity: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    task_complete: bool = False
    task_phase: str = ""
    gps_status: int = 0
    location_source: int = -1
    gps_num: int = 0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    rel_alt: Optional[float] = None
    identity: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    mission_altitude_m: Optional[float] = None
    task_assignment_acked: bool = False
    task_assignment_mission_id: str = ""
    task_assignment_checksum: str = ""


@dataclass
class UavRuntime:
    uav_id: int
    target_altitude_m: float
    task: Dict[str, Any]
    phase: UavPhase = UavPhase.UNKNOWN
    return_reason: Optional[str] = None
    last_error: Optional[str] = None
    assignment_checksum: str = ""
    landing_point_m: Optional[List[float]] = None
    landing_sequence: int = 0


@dataclass
class MissionRuntime:
    mission_id: str
    subject: str
    duration_seconds: float
    phase: MissionPhase
    planned_at: float
    flight_profile: str = "lab"
    flight_altitude_plan: str = "default"
    controller_mode: str = "internal"
    started_at: Optional[float] = None
    deadline_at: Optional[float] = None
    takeoff_commanded_at: Optional[float] = None
    confirmation_expires_at: Optional[float] = None
    uavs: Dict[int, UavRuntime] = field(default_factory=dict)
    planned_uavs: Dict[int, UavRuntime] = field(default_factory=dict)
    search_area: Optional[Dict[str, Any]] = None
    prepared_plan: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["phase"] = self.phase.value
        def serialize_uavs(items: Dict[int, UavRuntime]) -> Dict[str, Any]:
            return {
                str(uav_id): {
                    **asdict(runtime),
                    "phase": runtime.phase.value,
                }
                for uav_id, runtime in items.items()
            }

        result["uavs"] = serialize_uavs(self.uavs)
        result["planned_uavs"] = serialize_uavs(self.planned_uavs)
        return result


@dataclass
class SafetyConfig:
    production_config_confirmed: bool
    require_armed_for_takeoff: bool
    required_control_state: int
    preflight_battery_min: float
    return_battery_threshold: float
    telemetry_max_age_seconds: float
    altitude_tolerance_m: float
    vertical_speed_tolerance_mps: float
    takeoff_timeout_seconds: float
    confirmation_ttl_seconds: float


@dataclass
class BackendConfig:
    uav_ids: List[int]
    takeoff_altitudes_m: Dict[int, float]
    safety: SafetyConfig
    subjects: Dict[str, Dict[str, Any]]
