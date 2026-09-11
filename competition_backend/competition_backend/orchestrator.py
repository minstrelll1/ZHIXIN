from __future__ import annotations

import secrets
import threading
import time
import uuid
import math
from typing import Any, Callable, Dict, List, Optional

from .adapter import FleetAdapter
from .assignment_protocol import assignment_checksum
from .journal import EventJournal
from .models import (
    BackendConfig,
    MissionPhase,
    MissionRuntime,
    ReturnReason,
    Telemetry,
    UavPhase,
    UavRuntime,
)
from .search_planner import (
    plan_landing_points,
    plan_quadrilateral_landing_points,
    plan_quadrilateral_search,
    plan_rectangular_search,
)


class MissionError(RuntimeError):
    pass


class CompetitionOrchestrator:
    def __init__(
        self,
        config: BackendConfig,
        adapter: FleetAdapter,
        journal: Optional[EventJournal] = None,
        live_mode: bool = False,
        clock: Callable[[], float] = time.time,
        active_uav_ids: Optional[List[int]] = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.journal = journal or EventJournal(None)
        self.live_mode = live_mode
        self.clock = clock
        self.active_uav_ids = list(active_uav_ids or config.uav_ids)
        if not self.active_uav_ids or not set(self.active_uav_ids).issubset(
            set(config.uav_ids)
        ):
            raise ValueError("active_uav_ids must be a non-empty subset of configured UAVs")
        if len(set(self.active_uav_ids)) != len(self.active_uav_ids):
            raise ValueError("active_uav_ids must not contain duplicates")
        self._lock = threading.RLock()
        self._mission: Optional[MissionRuntime] = None
        self._telemetry: Dict[int, Telemetry] = {}
        self._confirmation_token: Optional[str] = None
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        adapter.set_telemetry_sink(self.update_telemetry)

    def start(self) -> None:
        self.adapter.start()
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="competition-monitor", daemon=True
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
        self.adapter.stop()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(0.25):
            self.tick()

    def _event(self, kind: str, **details: Any) -> None:
        event = {"time": self.clock(), "kind": kind, **details}
        if self._mission is not None:
            self._mission.events.append(event)
            self._mission.events = self._mission.events[-200:]
        self.journal.append(event)

    def _save(self) -> None:
        self.journal.save_snapshot(
            {
                "live_mode": self.live_mode,
                "mission": self._mission.to_dict() if self._mission else None,
            }
        )

    def plan(
        self,
        subject: str,
        tasks_by_uav: Optional[Dict[int, Dict[str, Any]]] = None,
        duration_seconds: Optional[float] = None,
        search_area: Optional[Dict[str, Any]] = None,
        flight_profile: str = "lab",
        controller_mode: str = "internal",
        gps_origin: Optional[Dict[str, Any]] = None,
        landing_area: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self._mission and self._mission.phase not in (
                MissionPhase.COMPLETED,
                MissionPhase.FAILED,
                MissionPhase.IDLE,
                MissionPhase.PLANNED,
                MissionPhase.PREFLIGHT_READY,
            ):
                raise MissionError("an active mission already exists")
            if subject not in self.config.subjects:
                raise MissionError("unknown subject: %s" % subject)
            template = self.config.subjects[subject]
            area = None
            landing_plan = None
            coordinate_mode = "xyz"
            input_gps_origin = None
            selected_altitudes = self.config.takeoff_altitudes_m
            if subject in ("subject1", "subject2") and tasks_by_uav is None:
                area = dict(template.get("search_area", {}))
                area.update(search_area or {})
                raw_sector_assignments = area.get("sector_by_uav", {})
                sector_assignments = {
                    int(key): int(value) for key, value in raw_sector_assignments.items()
                }
                profiles = template.get("takeoff_altitudes_m_by_profile", {})
                if profiles:
                    if flight_profile not in profiles:
                        raise MissionError("unknown flight profile: %s" % flight_profile)
                    selected_altitudes = {
                        int(key): float(value)
                        for key, value in profiles[flight_profile].items()
                    }
                if set(selected_altitudes) != set(self.config.uav_ids):
                    raise MissionError("flight profile must define all six UAV altitudes")
                coordinate_mode = str(area.get("coordinate_mode", "xyz")).lower()
                raw_points = area.get("points")
                landing_cfg = dict(landing_area or {})
                raw_landing_points = landing_cfg.get("points")
                if raw_points is not None or raw_landing_points is not None:
                    if raw_points is None or raw_landing_points is None:
                        raise MissionError("search area and landing area must both provide four points")
                    if coordinate_mode not in ("xyz", "gps"):
                        raise MissionError("coordinate_mode must be xyz or gps")
                    landing_coordinate_mode = str(
                        landing_cfg.get("coordinate_mode", coordinate_mode)
                    ).lower()
                    if landing_coordinate_mode != coordinate_mode:
                        raise MissionError(
                            "search area and landing area must use the same coordinate_mode"
                        )
                    if coordinate_mode == "gps":
                        try:
                            latitude = float(raw_points[0][0])
                            longitude = float(raw_points[0][1])
                            altitude = float((gps_origin or {}).get("altitude_m", area.get("altitude_m", 0.0)))
                        except (IndexError, TypeError, ValueError) as error:
                            raise MissionError("GPS area points must contain latitude and longitude") from error
                        if abs(latitude) > 90.0 or abs(longitude) > 180.0:
                            raise MissionError("GPS area point is out of range")
                        cos_lat = max(1e-6, abs(math.cos(math.radians(latitude))))
                        def gps_to_local(points):
                            local_points = []
                            for point in points:
                                try:
                                    point_latitude, point_longitude = float(point[0]), float(point[1])
                                except (IndexError, TypeError, ValueError) as error:
                                    raise MissionError(
                                        "GPS points must contain latitude and longitude"
                                    ) from error
                                if (not math.isfinite(point_latitude)
                                        or not math.isfinite(point_longitude)
                                        or abs(point_latitude) > 90.0
                                        or abs(point_longitude) > 180.0):
                                    raise MissionError("GPS point is out of range")
                                local_points.append([
                                    (point_latitude - latitude) * 111111.0,
                                    -(point_longitude - longitude) * 111111.0 * cos_lat,
                                ])
                            return local_points
                        area_points_m = gps_to_local(raw_points)
                        landing_points_m = gps_to_local(raw_landing_points)
                        input_gps_origin = {"latitude": latitude, "longitude": longitude, "altitude_m": altitude}
                    else:
                        try:
                            area_points_m = [[float(point[0]), float(point[1])] for point in raw_points]
                            landing_points_m = [[float(point[0]), float(point[1])] for point in raw_landing_points]
                        except (IndexError, TypeError, ValueError) as error:
                            raise MissionError("XYZ points must contain X and Y") from error
                    landing_plan = plan_quadrilateral_landing_points(
                        self.config.uav_ids, selected_altitudes, landing_points_m
                    )
                    task_source, coverage = plan_quadrilateral_search(
                        self.config.uav_ids,
                        area_points_m,
                        {uav_id: landing_plan[uav_id]["point_m"] for uav_id in self.config.uav_ids},
                        lane_spacing_m=float(area.get("lane_spacing_m", 150.0)),
                        turn_radius_m=float(area.get("turn_radius_m", 5.0)),
                    )
                    low_x, high_x = min(point[0] for point in area_points_m), max(point[0] for point in area_points_m)
                    low_y, high_y = min(point[1] for point in area_points_m), max(point[1] for point in area_points_m)
                    area = {
                        "coordinate_frame": "WGS84" if coordinate_mode == "gps" else "ENU",
                        "coordinate_mode": coordinate_mode,
                        "points": raw_points,
                        "points_m": coverage["polygon_m"],
                        "origin_x_m": low_x, "origin_y_m": low_y,
                        "width_m": high_y - low_y, "height_m": high_x - low_x,
                        "lane_spacing_m": float(area.get("lane_spacing_m", 150.0)),
                        "turn_radius_m": float(area.get("turn_radius_m", 5.0)),
                        "coverage": coverage,
                        "landing_area": {"coordinate_frame": "WGS84" if coordinate_mode == "gps" else "ENU",
                                         "coordinate_mode": coordinate_mode, "points": raw_landing_points,
                                         "points_m": landing_points_m},
                        "flight_profile": flight_profile,
                    }
                else:
                    task_source = plan_rectangular_search(
                        self.config.uav_ids,
                        width_m=float(area.get("width_m", 3.0)),
                        height_m=float(area.get("height_m", 3.0)),
                        lane_spacing_m=float(area.get("lane_spacing_m", 0.5)),
                        origin_x_m=float(area.get("origin_x_m", 0.0)),
                        origin_y_m=float(area.get("origin_y_m", 0.0)),
                        sector_by_uav=sector_assignments,
                    )
                    area = {
                        "coordinate_frame": "ENU",
                        "coordinate_mode": "xyz",
                        "coordinate_layout": {
                            "origin_corner": "bottom_right",
                            "vertical_axis": "positive_x_up",
                            "horizontal_axis": "positive_y_left",
                        },
                        "origin_x_m": float(area.get("origin_x_m", 0.0)),
                        "origin_y_m": float(area.get("origin_y_m", 0.0)),
                        "width_m": float(area.get("width_m", 3.0)),
                        "height_m": float(area.get("height_m", 3.0)),
                        "lane_spacing_m": float(area.get("lane_spacing_m", 0.5)),
                        "columns": 3, "rows": 2,
                        "sector_by_uav": {str(key): value for key, value in sector_assignments.items()},
                        "flight_profile": flight_profile,
                    }
            else:
                task_source = tasks_by_uav or {
                    int(key): value for key, value in template["tasks_by_uav"].items()
                }
            if set(task_source) != set(self.config.uav_ids):
                raise MissionError("tasks_by_uav must contain exactly UAV IDs 1 through 6")
            duration = float(
                duration_seconds
                if duration_seconds is not None
                else template["duration_seconds"]
            )
            if duration <= 0.0:
                raise MissionError("duration_seconds must be positive")
            controller_mode = str(controller_mode or "internal").strip().lower()
            if controller_mode not in ("internal", "external"):
                raise MissionError("controller_mode must be internal or external")
            normalized_gps_origin = input_gps_origin
            if landing_plan is None:
                landing_cfg = dict(landing_area or {})
                landing_plan = plan_landing_points(
                    self.config.uav_ids,
                    selected_altitudes,
                    float(landing_cfg.get("origin_x_m", 0.0)),
                    float(landing_cfg.get("origin_y_m", 0.0)),
                    float(landing_cfg.get("width_m", 3.0)),
                    float(landing_cfg.get("height_m", 3.0)),
                )
                landing_area_result = {
                    "origin_x_m": float(landing_cfg.get("origin_x_m", 0.0)),
                    "origin_y_m": float(landing_cfg.get("origin_y_m", 0.0)),
                    "width_m": float(landing_cfg.get("width_m", 3.0)),
                    "height_m": float(landing_cfg.get("height_m", 3.0)),
                    "coordinate_frame": "ENU",
                    "coordinate_layout": area.get("coordinate_layout") if area else None,
                }
                if area is not None:
                    area["landing_area"] = landing_area_result
            if controller_mode == "external":
                try:
                    source = normalized_gps_origin or gps_origin or {}
                    latitude = float(source["latitude"])
                    longitude = float(source["longitude"])
                    altitude = float(source["altitude_m"])
                except (KeyError, TypeError, ValueError) as error:
                    raise MissionError(
                        "external controller requires GPS origin latitude, longitude and altitude_m"
                    ) from error
                if not all(math.isfinite(value) for value in (latitude, longitude, altitude)):
                    raise MissionError("GPS origin values must be finite")
                if abs(latitude) > 90.0 or abs(longitude) > 180.0:
                    raise MissionError("GPS origin latitude or longitude is out of range")
                normalized_gps_origin = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude_m": altitude,
                }
                cos_lat = max(1e-6, abs(math.cos(math.radians(latitude))))
                for uav_id, task in list(task_source.items()):
                    converted = dict(task)
                    converted["waypoints_wgs84"] = [
                        [
                            latitude + float(point[0]) / 111111.0,
                            longitude - float(point[1]) / (111111.0 * cos_lat),
                            altitude + float(selected_altitudes[uav_id]),
                        ]
                        for point in task["waypoints_m"]
                    ]
                    task_source[uav_id] = converted
                if area is not None:
                    area["gps_origin"] = normalized_gps_origin

            now = self.clock()
            mission_id = "%s-%s" % (subject, uuid.uuid4().hex[:10])
            planned_uavs = {
                uav_id: UavRuntime(
                    uav_id=uav_id,
                    target_altitude_m=selected_altitudes[uav_id],
                    task=dict(task_source[uav_id]),
                    phase=UavPhase.READY,
                    landing_point_m=list(landing_plan[uav_id]["point_m"]),
                    landing_sequence=int(landing_plan[uav_id]["landing_sequence"]),
                )
                for uav_id in self.config.uav_ids
            }
            uavs = {
                uav_id: planned_uavs[uav_id] for uav_id in self.active_uav_ids
            }
            self._mission = MissionRuntime(
                mission_id=mission_id,
                subject=subject,
                duration_seconds=duration,
                phase=MissionPhase.PLANNED,
                planned_at=now,
                flight_profile=flight_profile,
                controller_mode=controller_mode,
                uavs=uavs,
                planned_uavs=planned_uavs,
                search_area=area,
            )
            for telemetry in self._telemetry.values():
                telemetry.task_complete = False
                telemetry.task_assignment_acked = False
                telemetry.task_assignment_mission_id = ""
                telemetry.task_assignment_checksum = ""
            self._confirmation_token = None
            for uav_id, runtime in self._mission.uavs.items():
                assignment_payload = {
                    "mission_id": mission_id,
                    "subject": subject,
                    "task": runtime.task,
                    "target_altitude_m": runtime.target_altitude_m,
                    "controller_mode": controller_mode,
                    "coordinate_frame": "WGS84" if controller_mode == "external" else "ENU",
                    "landing_point_m": runtime.landing_point_m,
                    "landing_sequence": runtime.landing_sequence,
                }
                if controller_mode == "external" and normalized_gps_origin is not None:
                    lat0 = normalized_gps_origin["latitude"]
                    lon0 = normalized_gps_origin["longitude"]
                    cos_lat = max(1e-6, abs(math.cos(math.radians(lat0))))
                    px, py = runtime.landing_point_m or [0.0, 0.0]
                    assignment_payload["landing_point_wgs84"] = [
                        lat0 + float(px) / 111111.0,
                        lon0 - float(py) / (111111.0 * cos_lat),
                        normalized_gps_origin["altitude_m"] + runtime.target_altitude_m,
                    ]
                runtime.assignment_checksum = assignment_checksum(
                    {"type": "assign_task", "uav_id": uav_id, **assignment_payload}
                )
                assignment_payload["assignment_checksum"] = runtime.assignment_checksum
                self.adapter.command_assign_task(
                    uav_id,
                    assignment_payload,
                )
                self._event("task_assignment_sent", uav_id=uav_id)
            self._event("mission_planned", mission_id=mission_id, subject=subject)
            if not self.active_uav_ids:
                self._event(
                    "mission_planning_only",
                    mission_id=mission_id,
                    reason="no connected UAV telemetry",
                )
            self._save()
            return self.snapshot()

    def update_telemetry(self, telemetry: Telemetry) -> None:
        if telemetry.uav_id not in self.active_uav_ids:
            return
        with self._lock:
            self._telemetry[telemetry.uav_id] = telemetry

    def set_active_uav_ids(self, uav_ids: List[int]) -> None:
        """Select connected UAVs for this mission while retaining six-UAV planning."""
        with self._lock:
            selected = [int(value) for value in uav_ids]
            if not set(selected).issubset(set(self.config.uav_ids)):
                raise MissionError("active UAV IDs must be a subset of configured UAVs")
            if len(set(selected)) != len(selected):
                raise MissionError("active UAV IDs must not contain duplicates")
            if self._mission and self._mission.phase not in (
                MissionPhase.IDLE,
                MissionPhase.PLANNED,
                MissionPhase.PREFLIGHT_READY,
                MissionPhase.COMPLETED,
                MissionPhase.FAILED,
            ):
                raise MissionError("cannot change active UAVs during an active mission")
            self.active_uav_ids = selected

    def preflight_report(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            battery_threshold_enforced = not (
                self._mission is not None
                and self._mission.flight_profile == "lab"
            )
            per_uav: Dict[str, List[str]] = {}
            if self.live_mode and not self.config.safety.production_config_confirmed:
                per_uav["system"] = [
                    "production_config_confirmed is false; live takeoff is locked"
                ]
            if self._mission is not None and not self.active_uav_ids:
                # A disconnected fleet may still be planned so the operator can
                # review the complete six-UAV solution.  It must never pass
                # preflight or accidentally transition into flight execution.
                per_uav.setdefault("system", []).append(
                    "no UAV is connected; this mission is planning-only"
                )
            for uav_id in self.active_uav_ids:
                failures: List[str] = []
                telemetry = self._telemetry.get(uav_id)
                if telemetry is None:
                    failures.append("no telemetry")
                else:
                    if now - telemetry.received_at > self.config.safety.telemetry_max_age_seconds:
                        failures.append("telemetry is stale")
                    if not telemetry.connected:
                        failures.append("flight controller is disconnected")
                    if not telemetry.odom_valid:
                        failures.append("odometry is invalid")
                    if telemetry.failsafe:
                        failures.append("failsafe is active")
                    if (
                        battery_threshold_enforced
                        and telemetry.battery_percentage
                        < self.config.safety.preflight_battery_min
                    ):
                        failures.append("battery is below preflight threshold")
                    if (
                        self.config.safety.require_armed_for_takeoff
                        and not telemetry.armed
                    ):
                        failures.append("UAV is not armed")
                    if telemetry.control_state != self.config.safety.required_control_state:
                        failures.append("control_state is not COMMAND_CONTROL")
                    if (
                        not telemetry.task_assignment_acked
                        or self._mission is None
                        or telemetry.task_assignment_mission_id
                        != self._mission.mission_id
                        or telemetry.task_assignment_checksum
                        != self._mission.uavs[uav_id].assignment_checksum
                    ):
                        failures.append("task assignment is not acknowledged")
                per_uav[str(uav_id)] = failures
            return {
                "ok": all(not failures for failures in per_uav.values()),
                "checked_at": now,
                "battery_threshold_enforced": battery_threshold_enforced,
                "failures": per_uav,
            }

    def prepare_takeoff(self) -> Dict[str, Any]:
        with self._lock:
            if not self._mission or self._mission.phase != MissionPhase.PLANNED:
                raise MissionError("mission must be planned before takeoff preparation")
            report = self.preflight_report()
            if not report["ok"]:
                return {"ready": False, "preflight": report}
            token = secrets.token_urlsafe(18)
            expires_at = self.clock() + self.config.safety.confirmation_ttl_seconds
            self._confirmation_token = token
            self._mission.confirmation_expires_at = expires_at
            self._mission.phase = MissionPhase.PREFLIGHT_READY
            self._event("preflight_passed", expires_at=expires_at)
            self._save()
            return {
                "ready": True,
                "confirmation_token": token,
                "expires_at": expires_at,
                "preflight": report,
            }

    def confirm_takeoff(self, token: str) -> Dict[str, Any]:
        with self._lock:
            if not self._mission or self._mission.phase != MissionPhase.PREFLIGHT_READY:
                raise MissionError("takeoff is not awaiting confirmation")
            now = self.clock()
            if (
                not token
                or token != self._confirmation_token
                or self._mission.confirmation_expires_at is None
                or now > self._mission.confirmation_expires_at
            ):
                self._mission.phase = MissionPhase.PLANNED
                self._confirmation_token = None
                raise MissionError("takeoff confirmation token is invalid or expired")
            report = self.preflight_report()
            if not report["ok"]:
                self._mission.phase = MissionPhase.PLANNED
                self._confirmation_token = None
                raise MissionError("preflight changed before confirmation")

            self._mission.phase = MissionPhase.TAKING_OFF
            self._mission.started_at = now
            self._mission.deadline_at = now + self._mission.duration_seconds
            self._mission.takeoff_commanded_at = now
            for uav_id, runtime in self._mission.uavs.items():
                runtime.phase = UavPhase.TAKEOFF_COMMANDED
                self.adapter.command_takeoff(
                    uav_id,
                    {
                        "mission_id": self._mission.mission_id,
                        "target_altitude_m": runtime.target_altitude_m,
                        "deadline_at": self._mission.deadline_at,
                        "return_battery_threshold": self.config.safety.return_battery_threshold,
                    },
                )
            self._confirmation_token = None
            self._event("takeoff_commanded", uav_ids=self.active_uav_ids)
            self._save()
            return self.snapshot()

    def request_return_all(self, reason: ReturnReason = ReturnReason.MANUAL) -> Dict[str, Any]:
        with self._lock:
            if not self._mission:
                raise MissionError("no mission exists")
            self._mission.phase = MissionPhase.RETURNING
            for uav_id in self.active_uav_ids:
                self._request_return_one(uav_id, reason)
            self._save()
            return self.snapshot()

    def _request_return_one(self, uav_id: int, reason: ReturnReason) -> None:
        if not self._mission:
            return
        runtime = self._mission.uavs[uav_id]
        if runtime.phase in (UavPhase.RETURN_COMMANDED, UavPhase.LANDED):
            return
        runtime.phase = UavPhase.RETURN_COMMANDED
        runtime.return_reason = reason.value
        self.adapter.command_return(
            uav_id,
            {
                "mission_id": self._mission.mission_id,
                "reason": reason.value,
                "land_after_return": True,
            },
        )
        self._event("return_commanded", uav_id=uav_id, reason=reason.value)

    def _mark_autonomous_return(self, uav_id: int, reason: ReturnReason) -> None:
        """Record a return that the onboard executor performs autonomously."""
        if not self._mission:
            return
        runtime = self._mission.uavs[uav_id]
        if runtime.phase in (UavPhase.RETURN_COMMANDED, UavPhase.LANDED):
            return
        runtime.phase = UavPhase.RETURN_COMMANDED
        runtime.return_reason = reason.value
        self._event("autonomous_return_expected", uav_id=uav_id, reason=reason.value)

    def tick(self) -> None:
        with self._lock:
            mission = self._mission
            if not mission or mission.phase in (
                MissionPhase.IDLE,
                MissionPhase.PLANNED,
                MissionPhase.PREFLIGHT_READY,
                MissionPhase.COMPLETED,
                MissionPhase.FAILED,
            ):
                return
            now = self.clock()

            for uav_id, runtime in mission.uavs.items():
                telemetry = self._telemetry.get(uav_id)
                if telemetry is None:
                    continue
                if (not telemetry.connected or now - telemetry.received_at > self.config.safety.telemetry_max_age_seconds
                        or runtime.phase == UavPhase.LANDED):
                    continue
                if (telemetry.task_assignment_mission_id == mission.mission_id
                        and telemetry.task_phase in ("returning", "landing", "landed")):
                    runtime.phase = UavPhase.RETURN_COMMANDED
                if runtime.phase == UavPhase.RETURN_COMMANDED and not telemetry.armed:
                    runtime.phase = UavPhase.LANDED
                    self._event("uav_landed", uav_id=uav_id)
                    continue
                if (
                    telemetry.armed
                    and telemetry.battery_percentage
                    <= self.config.safety.return_battery_threshold
                ):
                    self._mark_autonomous_return(uav_id, ReturnReason.LOW_BATTERY)

            if mission.deadline_at is not None and now >= mission.deadline_at:
                if mission.phase != MissionPhase.RETURNING:
                    mission.phase = MissionPhase.RETURNING
                    for uav_id in self.active_uav_ids:
                        self._mark_autonomous_return(uav_id, ReturnReason.TIME_LIMIT)

            if mission.phase in (MissionPhase.TAKING_OFF, MissionPhase.RUNNING):
                started_now = []
                for uav_id, runtime in mission.uavs.items():
                    telemetry = self._telemetry.get(uav_id)
                    if telemetry is None or runtime.phase != UavPhase.TAKEOFF_COMMANDED:
                        continue
                    altitude_error = abs(
                        telemetry.position[2] - runtime.target_altitude_m
                    )
                    if (
                        altitude_error <= self.config.safety.altitude_tolerance_m
                        and abs(telemetry.velocity[2])
                        <= self.config.safety.vertical_speed_tolerance_mps
                    ):
                        runtime.phase = UavPhase.AT_ALTITUDE
                        self._event("uav_at_altitude", uav_id=uav_id)
                        runtime.phase = UavPhase.EXECUTING
                        started_now.append(uav_id)
                        self._event("uav_task_started", uav_id=uav_id)
                if started_now:
                    mission.phase = MissionPhase.RUNNING
                if (
                    mission.takeoff_commanded_at is not None
                    and now - mission.takeoff_commanded_at
                    >= self.config.safety.takeoff_timeout_seconds
                ):
                    for uav_id, runtime in mission.uavs.items():
                        if runtime.phase == UavPhase.TAKEOFF_COMMANDED:
                            self._mark_autonomous_return(uav_id, ReturnReason.TAKEOFF_TIMEOUT)
                    if all(
                        runtime.phase in (UavPhase.RETURN_COMMANDED, UavPhase.LANDED)
                        for runtime in mission.uavs.values()
                    ):
                        mission.phase = MissionPhase.RETURNING

            if mission.phase == MissionPhase.RUNNING:
                for uav_id, runtime in mission.uavs.items():
                    telemetry = self._telemetry.get(uav_id)
                    if (
                        telemetry is not None
                        and telemetry.task_complete
                        and runtime.phase == UavPhase.EXECUTING
                    ):
                        runtime.phase = UavPhase.RETURN_COMMANDED
                        runtime.return_reason = ReturnReason.TASK_COMPLETE.value
                        self._event("return_commanded", uav_id=uav_id, reason="task_complete")
                if all(
                    runtime.phase in (UavPhase.RETURN_COMMANDED, UavPhase.LANDED)
                    for runtime in mission.uavs.values()
                ):
                    mission.phase = MissionPhase.RETURNING

            if mission.phase == MissionPhase.RETURNING and all(
                runtime.phase == UavPhase.LANDED for runtime in mission.uavs.values()
            ):
                mission.phase = MissionPhase.COMPLETED
                self._event("mission_completed")
                self.adapter.release_coordination()
            self._save()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            telemetry = {
                str(uav_id): {
                    "uav_id": item.uav_id,
                    "received_at": item.received_at,
                    "connected": item.connected,
                    "armed": item.armed,
                    "odom_valid": item.odom_valid,
                    "failsafe": item.failsafe,
                    "control_state": item.control_state,
                    "battery_percentage": item.battery_percentage,
                    "position": list(item.position),
                    "velocity": list(item.velocity),
                    "task_complete": item.task_complete,
                    "task_phase": item.task_phase,
                    "task_assignment_acked": item.task_assignment_acked,
                    "task_assignment_mission_id": item.task_assignment_mission_id,
                    "task_assignment_checksum": item.task_assignment_checksum,
                }
                for uav_id, item in self._telemetry.items()
            }
            mission = self._mission.to_dict() if self._mission else None
            dispatch_status = None
            if self._mission is not None:
                acknowledged = []
                for uav_id, runtime in self._mission.uavs.items():
                    item = self._telemetry.get(uav_id)
                    if (
                        item is not None
                        and item.task_assignment_acked
                        and item.task_assignment_mission_id == self._mission.mission_id
                        and item.task_assignment_checksum == runtime.assignment_checksum
                    ):
                        acknowledged.append(uav_id)
                dispatch_status = {
                    "mode": "planning_only" if not self.active_uav_ids else "assigned",
                    "planned_uav_ids": sorted(self._mission.planned_uavs),
                    "assigned_uav_ids": sorted(self.active_uav_ids),
                    "acknowledged_uav_ids": sorted(acknowledged),
                    "ack_required_before_takeoff": True,
                }
            return {
                "live_mode": self.live_mode,
                "active_uav_ids": list(self.active_uav_ids),
                "mission": mission,
                "dispatch_status": dispatch_status,
                "telemetry": telemetry,
                "server_time": self.clock(),
            }
