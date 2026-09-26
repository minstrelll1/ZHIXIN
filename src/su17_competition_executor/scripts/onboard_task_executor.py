#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import rospy
from prometheus_msgs.msg import UAVCommand, UAVControlState, UAVState
from std_msgs.msg import String, Float64MultiArray, Int32, Bool

# 源码部署时也可找到统一配置/坐标工具；无需改动厂商包。
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from competition_shared.navigation import resolve_waypoints
from competition_shared.scan import ScanSession
import uuid
from su17_competition_executor.tcp_link import OnboardTcpLink
from su17_competition_executor.task_protocol import (
    TaskValidationError,
    load_assignment,
    save_assignment_atomic,
    validate_assignment,
)


class OnboardTaskExecutor:
    def __init__(self) -> None:
        # 竞赛编号与飞控编号一致；两种机型共用该执行器。
        self.uav_id = int(rospy.get_param("~uav_id", 1))
        self.local_ros_uav_id = int(rospy.get_param("~local_ros_uav_id", self.uav_id))
        self.identity = json.loads(os.environ.get("COMPETITION_ONBOARD_IDENTITY", "{}"))
        self.vehicle_config = json.loads(os.environ.get("COMPETITION_ONBOARD_VEHICLE", "{}"))
        self._identity_error = ""
        self._scan_session = None
        self._image_health = {}
        self._image_health_received = 0.0
        if self.identity and (self.identity.get("uav_id") != self.uav_id or self.local_ros_uav_id != self.uav_id):
            raise ValueError("启动参数与已经核验的飞控编号不一致")
        self.enable_motion = bool(rospy.get_param("~enable_motion", False))
        self.transport = str(rospy.get_param("~transport", "tcp")).strip().lower()
        self.ground_host = str(rospy.get_param("~ground_host", ""))
        self.ground_port = int(rospy.get_param("~ground_port", 56100))
        self.auth_token = str(rospy.get_param("~auth_token", ""))
        self.telemetry_rate = float(rospy.get_param("~telemetry_rate_hz", 5.0))
        self.cache_path = str(rospy.get_param("~cache_path"))
        self.search_speed = float(rospy.get_param("~search_speed_mps", 0.30))
        self.return_speed = float(rospy.get_param("~return_speed_mps", 0.30))
        self.max_distance_from_home = float(
            rospy.get_param("~max_distance_from_home_m", 10.0)
        )
        self.log_full_task = bool(rospy.get_param("~log_full_task", True))
        self.command_rate = float(rospy.get_param("~command_rate_hz", 20.0))
        self.position_tolerance = float(rospy.get_param("~position_tolerance_m", 0.15))
        self.altitude_tolerance = float(rospy.get_param("~altitude_tolerance_m", 0.12))
        self.velocity_tolerance = float(rospy.get_param("~velocity_tolerance_mps", 0.15))
        # 扫描阶段允许少量位置抖动，避免实验室小范围飞行时因瞬时漂移
        # 提前中止整条航线。硬限制和持续时间仍保留，用于防止真正失控。
        self.scan_position_tolerance = max(
            self.position_tolerance,
            float(rospy.get_param("~scan_position_tolerance_m", 0.25)),
        )
        self.scan_altitude_tolerance = max(
            self.altitude_tolerance,
            float(rospy.get_param("~scan_altitude_tolerance_m", 0.20)),
        )
        self.scan_hard_position_tolerance = max(
            self.scan_position_tolerance,
            float(rospy.get_param("~scan_hard_position_tolerance_m", 0.50)),
        )
        self.scan_hard_altitude_tolerance = max(
            self.scan_altitude_tolerance,
            float(rospy.get_param("~scan_hard_altitude_tolerance_m", 0.30)),
        )
        self.scan_drift_grace_seconds = max(
            0.0, float(rospy.get_param("~scan_drift_grace_seconds", 3.0))
        )
        self.stable_seconds = float(rospy.get_param("~stable_seconds", 1.0))
        self.waypoint_timeout = float(rospy.get_param("~waypoint_timeout_sec", 45.0))
        self.deceleration_distance = float(rospy.get_param("~deceleration_distance_m", 0.50))
        self.xy_gain = float(rospy.get_param("~xy_gain", 0.8))
        self.external_mission_topic = str(
            rospy.get_param("~external_mission_topic", "")
        ).strip() or "/uav{}/competition/external_mission".format(self.local_ros_uav_id)
        self.external_status_topic = str(
            rospy.get_param("~external_status_topic", "")
        ).strip() or "/uav{}/competition/external_status".format(self.local_ros_uav_id)
        self.external_path_topic = str(
            rospy.get_param("~external_path_topic", "")
        ).strip() or "/ground_mission_planner/vehicle_{}/path_stage_1".format(self.local_ros_uav_id)
        self.external_landing_topic = str(
            rospy.get_param("~external_landing_topic", "")
        ).strip() or "/ground_mission_planner/vehicle_{}/jiangluodian".format(self.local_ros_uav_id)
        self.external_return_topic = str(
            rospy.get_param("~external_return_topic", "")
        ).strip() or "/ground_mission_planner/vehicle_{}/return_home".format(self.local_ros_uav_id)
        self.restart_all_command = str(rospy.get_param("~restart_all_command", "")).strip()
        self.max_speed = float(self.vehicle_config.get("max_speed_mps", 2.0))
        self.search_speed = min(self.search_speed, self.max_speed)
        self.return_speed = min(self.return_speed, self.max_speed)
        self.flight_speed_limit = None
        if self.identity:
            self._refresh_flight_speed_limit()
        self._validate_parameters()

        self._lock = threading.RLock()
        self._state: Optional[UAVState] = None
        self._control: Optional[UAVControlState] = None
        self._assignment: Optional[Dict[str, Any]] = None
        self._assignment_acked = False
        self._home: Optional[Tuple[float, float, float, float]] = None
        self._gps_home: Optional[Tuple[float, float, float]] = None
        self._motion_thread: Optional[threading.Thread] = None
        self._pending_execute: Optional[Dict[str, Any]] = None
        self._abort_motion = threading.Event()
        self._command_id = int(rospy.Time.now().to_sec()) & 0xFFFFFFFF
        self.tcp_link: Optional[OnboardTcpLink] = None
        self._progress = {"phase": "idle", "next_waypoint": 0}
        self._resume_pending = False
        self._state_received = 0.0
        self._control_received = 0.0
        self._boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self._restore_progress()

        local_prefix = "/uav{}".format(self.local_ros_uav_id)
        fleet_prefix = "/uav{}".format(self.uav_id)
        self.command_pub = rospy.Publisher(
            local_prefix + "/prometheus/command", UAVCommand, queue_size=10
        )
        self.status_pub = rospy.Publisher(
            fleet_prefix + "/competition/task_status", String, queue_size=10, latch=True
        )
        self.external_mission_pub = rospy.Publisher(
            self.external_mission_topic, String, queue_size=1, latch=True
        )
        self.external_path_pub = rospy.Publisher(
            self.external_path_topic, Float64MultiArray, queue_size=1, latch=True
        )
        self.external_landing_pub = rospy.Publisher(
            self.external_landing_topic, Float64MultiArray, queue_size=1, latch=True
        )
        self.external_return_pub = rospy.Publisher(
            self.external_return_topic, Bool, queue_size=1, latch=True
        )
        self.image_mission_control_pub = rospy.Publisher(
            "/uav{}/image_transfer/mission_control".format(self.uav_id),
            String,
            queue_size=1,
        )
        self.recon_start_mode_pub = rospy.Publisher(
            "/ground_mission_planner/recon_start_mode", Int32, queue_size=1, latch=True
        )
        rospy.Subscriber(
            self.external_status_topic,
            String,
            self._external_status_callback,
            queue_size=10,
        )
        if self.transport in ("ros", "both"):
            rospy.Subscriber(
                fleet_prefix + "/competition/high_level_command",
                String,
                self._high_level_callback,
                queue_size=20,
            )
        rospy.Subscriber(
            local_prefix + "/prometheus/state",
            UAVState,
            self._state_callback,
            queue_size=1,
        )
        rospy.Subscriber(
            local_prefix + "/prometheus/control_state",
            UAVControlState,
            self._control_callback,
            queue_size=1,
        )

        self.scan_pub = rospy.Publisher(local_prefix + "/competition/scan/request", String, queue_size=1)
        rospy.Subscriber(local_prefix + "/competition/scan/status", String, self._scan_callback, queue_size=5)
        rospy.Subscriber(local_prefix + "/competition/image_health", String, self._image_health_callback, queue_size=1)
        if self.transport in ("tcp", "both"):
            self.tcp_link = OnboardTcpLink(
                uav_id=self.uav_id,
                ground_host=self.ground_host,
                ground_port=self.ground_port,
                auth_token=self.auth_token,
                command_handler=self._handle_high_level_payload,
                identity=self.identity,
            )
            self.tcp_link.start()
            self._telemetry_timer = rospy.Timer(
                rospy.Duration(1.0 / self.telemetry_rate),
                self._tcp_telemetry_timer,
            )
            rospy.on_shutdown(self.tcp_link.stop)

        self._publish_status("executor_ready", enable_motion=self.enable_motion)
        self._recovery_timer = rospy.Timer(rospy.Duration(1.0), self._resume_tick)
        rospy.logwarn(
            "UAV%d 竞赛执行器就绪；ROS=/uav%d，通信=%s，"
            "地面=%s:%d，飞行控制=%s",
            self.uav_id,
            self.local_ros_uav_id,
            self.transport,
            self.ground_host,
            self.ground_port,
            self.enable_motion,
        )

    def _external_status_callback(self, message: String) -> None:
        """Accept progress notifications from the external flight program B."""
        try:
            payload = json.loads(message.data)
            if not isinstance(payload, dict):
                raise ValueError("external status must be a JSON object")
            if payload.get("uav_id") not in (None, self.uav_id):
                return
            phase = str(payload.get("phase", payload.get("state", ""))).strip().lower()
            if not phase:
                return
            with self._lock:
                assignment = self._assignment
                if assignment is None:
                    return
                if payload.get("mission_id") not in (None, assignment["mission_id"]):
                    return
                if "next_waypoint" in payload:
                    self._progress["next_waypoint"] = int(payload["next_waypoint"])
            if phase in ("completed", "task_complete", "finished"):
                count = len(self._assignment["task"]["waypoints_m"])
                self._checkpoint("completed", next_waypoint=count)
                self._publish_status("completed", controller_mode="external")
            elif phase in ("returning", "return"):
                self._checkpoint("returning")
                self._publish_status("returning", controller_mode="external")
            elif phase in ("landed", "landing"):
                self._checkpoint(phase)
                self._publish_status(phase, controller_mode="external")
            else:
                self._checkpoint("external_" + phase)
                self._publish_status("external_status", phase=phase)
        except (TypeError, ValueError, KeyError) as error:
            rospy.logwarn("已忽略无效的外部程序状态：%s", error)

    def _validate_parameters(self) -> None:
        if self.uav_id <= 0:
            raise ValueError("~uav_id must be positive")
        if self.local_ros_uav_id <= 0:
            raise ValueError("~local_ros_uav_id must be positive")
        for label, value in (
            ("search_speed_mps", self.search_speed),
            ("return_speed_mps", self.return_speed),
        ):
            if not 0.0 < value <= self.max_speed:
                raise ValueError("%s 超出机队配置的速度上限" % label)
        if self.max_distance_from_home <= 0.0:
            raise ValueError("~max_distance_from_home_m must be positive")
        if self.scan_position_tolerance <= 0.0 or self.scan_altitude_tolerance <= 0.0:
            raise ValueError("扫描阶段的位置和高度容差必须为正数")
        if self.scan_hard_position_tolerance < self.scan_position_tolerance:
            raise ValueError("扫描硬位置限制不能小于软位置容差")
        if self.scan_hard_altitude_tolerance < self.scan_altitude_tolerance:
            raise ValueError("扫描硬高度限制不能小于软高度容差")
        if not 5.0 <= self.command_rate <= 50.0:
            raise ValueError("~command_rate_hz must be in [5, 50]")
        if self.transport not in ("tcp", "ros", "both"):
            raise ValueError("~transport must be tcp, ros, or both")
        if not 1.0 <= self.telemetry_rate <= 20.0:
            raise ValueError("~telemetry_rate_hz must be in [1, 20]")
        if not 1 <= self.ground_port <= 65535:
            raise ValueError("~ground_port must be a valid TCP port")

    def _image_health_callback(self, message):
        try:
            self._image_health = json.loads(message.data)
            self._image_health_received = time.monotonic()
        except (ValueError, TypeError):
            pass

    def _state_callback(self, message: UAVState) -> None:
        if getattr(self, "identity", {}) and int(message.uav_id) != self.uav_id:
            self._identity_error = "运行中的飞控编号与启动时不一致"
            self._abort_motion.set()
            rospy.logerr_throttle(5, self._identity_error)
            return
        with self._lock:
            self._state = message
            self._state_received = time.monotonic()

    def _control_callback(self, message: UAVControlState) -> None:
        if getattr(self, "identity", {}) and int(message.uav_id) != self.uav_id:
            self._identity_error = "运行中的控制状态编号不一致"
            self._abort_motion.set()
            return
        with self._lock:
            self._control = message
            self._control_received = time.monotonic()

    def _checkpoint(self, phase: str, **values: Any) -> None:
        with self._lock:
            self._progress.update(values)
            self._progress.update(
                phase=phase,
                home=self._home,
                gps_home=self._gps_home,
                boot_id=self._boot_id,
                device_id=getattr(self, "identity", {}).get("device_id"),
                model=getattr(self, "identity", {}).get("model"),
                fleet_revision=getattr(self, "identity", {}).get("fleet_revision"),
            )
            save_assignment_atomic(self.cache_path + ".progress.json", {
                "assignment": self._assignment, "progress": self._progress,
            })

    def _restore_progress(self) -> None:
        path = Path(self.cache_path + ".progress.json")
        if not path.exists():
            return
        try:
            saved = load_assignment(str(path))
            assignment = validate_assignment(saved["assignment"], self.uav_id)
            progress = saved["progress"]
            identity = getattr(self, "identity", {})
            if identity and (progress.get("device_id") != identity.get("device_id") or progress.get("model") != identity.get("model")):
                raise ValueError("缓存任务属于其他设备或机型，拒绝恢复")
            if identity and progress.get("fleet_revision") != identity.get("fleet_revision"):
                raise ValueError("缓存任务的机队配置已变更，请重新分派任务")
            index = int(progress["next_waypoint"])
            if not 0 <= index <= len(assignment["task"]["waypoints_m"]):
                raise ValueError("保存的航点序号无效")
            self._assignment = assignment
            self._assignment_acked = True
            self._progress = progress
            self._home = tuple(progress["home"]) if progress.get("home") else None
            self._gps_home = tuple(progress["gps_home"]) if progress.get("gps_home") else None
            self._resume_pending = progress.get("phase") in (
                "taking_off", "executing", "returning", "landing",
                "external_waiting", "external_executing"
            ) and progress.get("boot_id") == self._boot_id
        except (OSError, ValueError, KeyError, TypeError) as error:
            rospy.logerr("任务恢复被拒绝：%s", error)

    def _resume_tick(self, _event: Any) -> None:
        state, _ = self._snapshot()
        if (self._progress.get("phase") == "landing" and state is not None
                and time.monotonic() - self._state_received < 2.0 and not state.armed):
            self._checkpoint("landed")
            self._resume_pending = False
            self._publish_status("landed")
        if not self._resume_pending or not self.enable_motion:
            return
        ready, _ = self._ready()
        if not ready:
            return
        with self._lock:
            if self._motion_thread is not None and self._motion_thread.is_alive():
                return
            self._resume_pending = False
            payload = dict(self._progress.get("execution", {}))
            target = self._resume_flight
            self._motion_thread = threading.Thread(
                target=self._motion_entry, args=("resume", target, payload), daemon=True
            )
            self._motion_thread.start()

    def _resume_flight(self, payload: Dict[str, Any]) -> bool:
        phase = self._progress["phase"]
        self._publish_status("task_resuming", next_waypoint=self._progress["next_waypoint"])
        if phase == "landing":
            self._land()
            return True
        if phase in ("external_waiting", "external_executing"):
            self._publish_external_mission(payload)
            # 程序 B 约定：0 表示正常启动，1 表示异常启动。
            # 任务数据已经按固定顺序发布完毕后，正常任务发送 0。
            self._publish_recon_start_mode(0)
            self._checkpoint("external_waiting", execution=dict(payload))
            self._publish_status("external_mission_published", controller_mode="external")
            return True
        if phase == "returning" or (payload.get("deadline_at") is not None and time.time() >= payload["deadline_at"]):
            self._run_return({"reason": "recovery", "land_after_return": True})
            return True
        if phase == "taking_off":
            if self._home is None:
                return False
            ok, _ = self._fly_to(self._home[0], self._home[1], self._target_z(self._assignment), self._home[3], self.search_speed)
            if not ok:
                return False
        return self._run_task(payload)

    def _snapshot(self) -> Tuple[Optional[UAVState], Optional[UAVControlState]]:
        with self._lock:
            return self._state, self._control

    def _publish_status(self, state: str, **details: Any) -> None:
        payload = {
            "state": state,
            "uav_id": self.uav_id,
            "timestamp_unix": time.time(),
            "task_assignment_acked": self._assignment_acked,
        }
        if self._assignment is not None:
            payload["mission_id"] = self._assignment["mission_id"]
            payload["assignment_checksum"] = self._assignment[
                "assignment_checksum"
            ]
        payload.update(details)
        self.status_pub.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )
        if self.tcp_link is not None:
            self.tcp_link.send_reliable(
                {"type": "task_status", "uav_id": self.uav_id, "status": payload}
            )

    def _tcp_telemetry_timer(self, _event: Any) -> None:
        if self.tcp_link is None:
            return
        state, control = self._snapshot()
        if state is None or control is None:
            return
        def gps_value(field_name):
            try:
                value = float(getattr(state, field_name))
            except (AttributeError, TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None
        self.tcp_link.update_telemetry(
            {
                "type": "telemetry",
                "uav_id": self.uav_id,
                "timestamp_unix": time.time(),
                "connected": bool(state.connected) and not self._identity_error and time.monotonic() - min(self._state_received, self._control_received) < 2.0,
                "capabilities": {"motion_enabled": self.enable_motion, "max_speed_mps": self.max_speed,
                    "flight_speed_limit_mps": self.flight_speed_limit,
                    "scan_available": self.scan_pub.get_num_connections() > 0 or self.vehicle_config.get("scan_mode") == "timed_hover",
                    "state_received": True, "control_received": True,
                    "image_received": time.monotonic() - self._image_health_received < 3.0,
                    "image_stamp_basis": self._image_health.get("stamp_basis", "unknown"),
                    "identity_error": self._identity_error},
                "armed": bool(state.armed),
                "odom_valid": bool(state.odom_valid),
                "failsafe": bool(control.failsafe),
                "control_state": int(control.control_state),
                "battery_percentage": float(state.battery_percetage),
                "position": [float(value) for value in state.position],
                "mission_altitude_m": float(state.position[2]) - self._home[2] if self._home and self._assignment and self._assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST" else None,
                "velocity": [float(value) for value in state.velocity],
                "task_phase": self._progress.get("phase", "idle"),
                "gps_status": int(getattr(state, "gps_status", 0)),
                "location_source": int(getattr(state, "location_source", -1)),
                "gps_num": int(getattr(state, "gps_num", 0)),
                "latitude": gps_value("latitude"),
                "longitude": gps_value("longitude"),
                "altitude": gps_value("altitude"),
                "rel_alt": gps_value("rel_alt"),
                "task_assignment_acked": self._assignment_acked,
                "task_assignment_mission_id": self._assignment["mission_id"] if self._assignment else "",
                "task_assignment_checksum": self._assignment["assignment_checksum"] if self._assignment else "",
                "task_complete": bool(self._assignment and self._progress.get("next_waypoint", 0) >= len(self._assignment["task"]["waypoints_m"])),
            }
        )

    def _high_level_callback(self, message: String) -> None:
        try:
            payload = json.loads(message.data)
            self._handle_high_level_payload(payload)
        except (TypeError, ValueError, KeyError) as error:
            rospy.logerr("高级控制指令被拒绝：%s", error)
            self._publish_status("command_rejected", error=str(error))

    def _handle_high_level_payload(self, payload: Dict[str, Any]) -> None:
        try:
            if not isinstance(payload, dict):
                raise ValueError("JSON root must be an object")
            if int(payload.get("uav_id", -1)) != self.uav_id:
                return
            command_type = payload.get("type")
            if command_type == "assign_task":
                self._accept_assignment(payload)
            elif command_type == "takeoff":
                self._accept_motion_command("takeoff", payload, self._run_takeoff)
            elif command_type == "execute_task":
                self._accept_motion_command("execute_task", payload, self._run_task)
            elif command_type == "return_home":
                self._start_return(payload)
            elif command_type == "restart_executor":
                state, _ = self._snapshot()
                if state is None or time.monotonic() - self._state_received > 2.0:
                    raise ValueError("restart requires fresh UAV state")
                self._publish_status("executor_restarting")
                threading.Timer(0.5, lambda: rospy.signal_shutdown("operator restart")).start()
            elif command_type == "restart_all_programs":
                self._publish_status("all_programs_restarting")
                if self.restart_all_command:
                    if not os.path.isfile(self.restart_all_command) or not os.access(self.restart_all_command, os.X_OK):
                        raise ValueError("全部重启命令不可执行")
                    subprocess.Popen([self.restart_all_command], close_fds=True)
                else:
                    rospy.logwarn("未配置全部重启命令，仅重启竞赛执行器")
                threading.Timer(0.5, lambda: rospy.signal_shutdown("operator restart all")).start()
            elif command_type == "restart_takeoff":
                if self._assignment is None:
                    raise ValueError("没有可重启的已保存任务")
                with self._lock:
                    if self._motion_thread is not None and self._motion_thread.is_alive():
                        raise ValueError("竞赛执行器正在执行其他任务")
                    self._home = None
                    self._gps_home = None
                    self._progress = {"phase": "assigned", "next_waypoint": 0}
                    self._checkpoint("assigned", next_waypoint=0, execution={})
                self._publish_status("takeoff_restarting")
                retry = dict(payload)
                retry["mission_id"] = self._assignment["mission_id"]
                retry["target_altitude_m"] = self._assignment["target_altitude_m"]
                self._accept_motion_command("takeoff", retry, self._run_takeoff)
            else:
                self._publish_status("command_rejected", error="unsupported command type")
        except (TypeError, ValueError, KeyError) as error:
            rospy.logerr("高级控制指令被拒绝：%s", error)
            self._publish_status("command_rejected", error=str(error))

    def _accept_assignment(self, payload: Dict[str, Any]) -> None:
        try:
            assignment = validate_assignment(payload, self.uav_id)
            if self._assignment and assignment["assignment_checksum"] == self._assignment["assignment_checksum"]:
                self._publish_status("task_received")
                return
            if self._motion_thread is not None and self._motion_thread.is_alive():
                raise TaskValidationError("当前任务正在执行，不能覆盖")
            waypoints = assignment["task"]["waypoints_m"]
            requested_speed = float(assignment["task"].get("speed_mps", self.search_speed))
            if requested_speed > getattr(self, "max_speed", 2.0):
                raise TaskValidationError("规划航速超过本机配置上限，请先核验飞控限制并调整配置或规划航速")
            if assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST":
                state, _ = self._snapshot()
                try:
                    gps = self._valid_gps(state)
                    resolve_waypoints(assignment["task"], state.position, gps, self.max_distance_from_home)
                except ValueError as error:
                    raise TaskValidationError(str(error))
            elif any(math.hypot(point[0], point[1]) > self.max_distance_from_home for point in waypoints):
                raise TaskValidationError(
                    "waypoint exceeds max_distance_from_home_m safety limit"
                )
            save_assignment_atomic(self.cache_path, assignment)
        except (OSError, TaskValidationError) as error:
            self._assignment_acked = False
            rospy.logerr("任务下发被拒绝：%s", error)
            self._publish_status("task_rejected", error=str(error))
            return

        with self._lock:
            self._assignment = assignment
            self._assignment_acked = True
            self._home = None
            self._gps_home = None
            self._resume_pending = False
            self._progress = {"phase": "assigned", "next_waypoint": 0}
            self._checkpoint("assigned", next_waypoint=0, execution={})
        self._publish_status(
            "task_received",
            waypoint_count=len(waypoints),
            cache_path=self.cache_path,
            assignment_checksum=assignment["assignment_checksum"],
            task_type=assignment["task"]["type"],
            sector=assignment["task"].get("sector"),
            bounds_m=assignment["task"].get("bounds_m"),
            lane_spacing_m=assignment["task"].get("lane_spacing_m"),
            target_altitude_m=assignment["target_altitude_m"],
            controller_mode=assignment.get("controller_mode", "internal"),
        )
        rospy.loginfo(
            "已接收任务：任务=%s，UAV%d，航点=%d，缓存=%s",
            assignment["mission_id"],
            self.uav_id,
            len(waypoints),
            self.cache_path,
        )
        rospy.loginfo(
            "完整任务 SHA-256：%s", assignment["assignment_checksum"]
        )
        if self.log_full_task:
            rospy.loginfo(
                "完整下发任务 JSON：\n%s",
                json.dumps(assignment, ensure_ascii=False, indent=2, sort_keys=True),
            )

    def _accept_motion_command(self, name: str, payload: Dict[str, Any], target: Any) -> None:
        if not self.enable_motion:
            self._publish_status("motion_disabled", command=name)
            rospy.logwarn("已忽略 %s：飞行控制未启用", name)
            return
        with self._lock:
            assignment = self._assignment
            active_thread = (
                self._motion_thread
                if self._motion_thread is not None and self._motion_thread.is_alive()
                else None
            )
        if assignment is None or payload.get("mission_id") != assignment["mission_id"]:
            self._publish_status("command_rejected", command=name, error="mission is not assigned")
            return
        if name == "takeoff" and self._progress.get("phase") not in ("assigned", "idle"):
            self._publish_status("command_rejected", command=name, error="mission already started; reassign before a new takeoff")
            return
        if active_thread is not None:
            if name == "execute_task" and active_thread.name == "takeoff":
                with self._lock:
                    if self._motion_thread is active_thread and active_thread.is_alive():
                        self._pending_execute = dict(payload)
                    else:
                        active_thread = None
            if active_thread is not None and name == "execute_task" and active_thread.name == "takeoff":
                self._publish_status("execute_queued", reason="waiting_for_takeoff")
                rospy.loginfo(
                "执行指令已排队；UAV%d 到达 %.2f 米目标高度后将立即前往首个航点",
                    self.uav_id,
                    float(assignment["target_altitude_m"]),
                )
                return
            if active_thread is not None:
                self._publish_status("command_rejected", command=name, error="executor is busy")
                return
        self._abort_motion.clear()
        thread = threading.Thread(
            target=self._motion_entry, args=(name, target, payload), name=name
        )
        thread.daemon = True
        with self._lock:
            self._motion_thread = thread
        if name == "takeoff":
            self._publish_image_mission_start(payload)
        thread.start()

    def _publish_image_mission_start(self, payload: Dict[str, Any]) -> None:
        mission_id = str(payload.get("mission_id") or "").strip()
        if not mission_id:
            return
        try:
            self.image_mission_control_pub.publish(
                String(data="start:{}".format(mission_id))
            )
            rospy.loginfo("已触发图片任务计时：任务=%s", mission_id)
        except Exception as error:
            # 图片回传计时不能阻塞起飞控制；发送失败由图片节点状态和地面端日志报告。
            rospy.logwarn("图片任务计时触发失败：%s", error)

    def _motion_entry(self, name: str, target: Any, payload: Dict[str, Any]) -> None:
        try:
            succeeded = bool(target(payload))
        except Exception as error:
            succeeded = False
            rospy.logerr("%s 执行异常终止：%s", name, error)
            self._publish_status("task_failed", command=name, error=str(error))
        if not succeeded and not self._abort_motion.is_set():
            self._checkpoint("paused")
        pending = None
        with self._lock:
            if name == "takeoff" and succeeded:
                # Continue locally even when the ground link has disappeared.
                pending = self._pending_execute or dict(payload)
                self._pending_execute = None
            elif name == "takeoff":
                self._pending_execute = None
            if pending is None and self._motion_thread is threading.current_thread():
                self._motion_thread = None

        if pending is not None and not rospy.is_shutdown():
            if self._assignment and self._assignment.get("controller_mode", "internal") == "external":
                try:
                    self._publish_external_mission(pending)
                    self._publish_recon_start_mode(0)
                    self._checkpoint("external_waiting", execution=dict(pending))
                    self._publish_status("external_mission_published", controller_mode="external")
                except Exception as error:
                    self._checkpoint("paused")
                    self._publish_status("task_failed", error=str(error))
                with self._lock:
                    if self._motion_thread is threading.current_thread():
                        self._motion_thread = None
                return
            self._publish_status("departing_to_first_waypoint")
            rospy.loginfo(
                "UAV%d 已到达任务高度，正在前往首个航点",
                self.uav_id,
            )
            try:
                if not self._run_task(pending) and not self._abort_motion.is_set():
                    self._checkpoint("paused")
            except Exception as error:
                self._checkpoint("paused")
                self._publish_status("task_failed", error=str(error))
            with self._lock:
                if self._motion_thread is threading.current_thread():
                    self._motion_thread = None

    def _start_return(self, payload: Dict[str, Any]) -> None:
        if not self.enable_motion:
            self._publish_status("motion_disabled", command="return_home")
            return
        with self._lock:
            assignment = self._assignment
        if assignment is None or payload.get("mission_id") != assignment["mission_id"]:
            self._publish_status(
                "command_rejected", command="return_home", error="mission is not assigned"
            )
            return
        if self._progress.get("phase") in ("returning", "landing", "landed"):
            return
        with self._lock:
            previous = self._motion_thread
            if previous is not None and previous.is_alive() and previous.name == "return_home":
                return
        self._abort_motion.set()

        def coordinator() -> None:
            if previous is not None and previous is not threading.current_thread():
                previous.join()
            self._abort_motion.clear()
            self._run_return(payload)

        thread = threading.Thread(target=coordinator, name="return_home")
        thread.daemon = True
        with self._lock:
            self._motion_thread = thread
        thread.start()

    def _ready(self) -> Tuple[bool, str]:
        if getattr(self, "_identity_error", ""):
            return False, self._identity_error
        state, control = self._snapshot()
        if state is None or control is None:
            return False, "waiting for UAV state"
        if time.monotonic() - min(self._state_received, self._control_received) > 2.0:
            return False, "UAV state is stale"
        if not state.connected:
            return False, "飞行控制器未连接"
        if not state.armed:
            return False, "UAV is not armed"
        if not state.odom_valid:
            return False, "里程计数据无效"
        if control.control_state != UAVControlState.COMMAND_CONTROL:
            return False, "控制状态不是 COMMAND_CONTROL"
        if control.pos_controller != UAVControlState.PX4_ORIGIN:
            return False, "位置控制器不是 PX4_ORIGIN"
        if control.failsafe:
            return False, "飞控保护模式已触发"
        return True, "ready"

    def _next_command_id(self) -> int:
        self._command_id = (self._command_id + 1) & 0xFFFFFFFF
        return self._command_id

    def _publish_velocity(self, vx: float, vy: float, target_z: float, yaw: float) -> None:
        command = UAVCommand()
        command.header.stamp = rospy.Time.now()
        command.header.frame_id = "ENU"
        command.Agent_CMD = UAVCommand.Move
        command.Control_Level = UAVCommand.DEFAULT_CONTROL
        command.Move_mode = UAVCommand.XY_VEL_Z_POS
        command.position_ref[2] = target_z
        command.velocity_ref[0] = vx
        command.velocity_ref[1] = vy
        command.yaw_ref = yaw
        command.Yaw_Rate_Mode = False
        command.Command_ID = self._next_command_id()
        self.command_pub.publish(command)

    def _hover(self) -> None:
        command = UAVCommand()
        command.header.stamp = rospy.Time.now()
        command.header.frame_id = "ENU"
        command.Agent_CMD = UAVCommand.Current_Pos_Hover
        command.Control_Level = UAVCommand.DEFAULT_CONTROL
        command.Command_ID = self._next_command_id()
        self.command_pub.publish(command)

    def _land(self) -> None:
        command = UAVCommand()
        command.header.stamp = rospy.Time.now()
        command.header.frame_id = "ENU"
        command.Agent_CMD = UAVCommand.Land
        command.Control_Level = UAVCommand.DEFAULT_CONTROL
        command.Command_ID = self._next_command_id()
        self.command_pub.publish(command)

    def _fly_to(
        self,
        target_x: float,
        target_y: float,
        target_z: float,
        yaw: float,
        speed_limit: float,
        deadline_at: Optional[float] = None,
        return_battery_threshold: Optional[float] = None,
        check_abort: bool = True,
    ) -> Tuple[bool, str]:
        speed_limit = min(speed_limit, getattr(self, "max_speed", speed_limit), getattr(self, "flight_speed_limit", None) or speed_limit)
        started = rospy.Time.now()
        initial_state, _ = self._snapshot()
        initial_distance = math.hypot(target_x-initial_state.position[0], target_y-initial_state.position[1]) if initial_state is not None else 0
        travel_timeout = max(self.waypoint_timeout, initial_distance / max(speed_limit, 0.01) * 2 + 15)
        stable_since: Optional[rospy.Time] = None
        rate = rospy.Rate(self.command_rate)
        while not rospy.is_shutdown():
            if check_abort and self._abort_motion.is_set():
                self._hover()
                return False, "preempted"
            ready, reason = self._ready()
            if not ready:
                self._hover()
                return False, reason
            state, _ = self._snapshot()
            if state is None:
                return False, "UAV state disappeared"
            if deadline_at is not None and time.time() >= deadline_at:
                self._hover()
                return False, "time_limit"
            if (
                return_battery_threshold is not None
                and state.battery_percetage <= return_battery_threshold
            ):
                self._hover()
                return False, "low_battery"

            error_x = target_x - state.position[0]
            error_y = target_y - state.position[1]
            error_z = target_z - state.position[2]
            distance = math.hypot(error_x, error_y)
            speed = min(speed_limit, self.xy_gain * distance)
            if distance > self.deceleration_distance:
                speed = speed_limit
            vx = speed * error_x / distance if distance > 1e-9 else 0.0
            vy = speed * error_y / distance if distance > 1e-9 else 0.0
            self._publish_velocity(vx, vy, target_z, yaw)

            settled = (
                distance <= self.position_tolerance
                and abs(error_z) <= self.altitude_tolerance
                and math.hypot(state.velocity[0], state.velocity[1]) <= self.velocity_tolerance
                and abs(state.velocity[2]) <= self.velocity_tolerance
            )
            now = rospy.Time.now()
            if settled:
                if stable_since is None:
                    stable_since = now
                elif (now - stable_since).to_sec() >= self.stable_seconds:
                    # Prometheus 的 XY_VEL_Z_POS 在 vx=vy=0 时会使用其内部
                    # 速度控制起始点作为位置锚点，可能把无人机拉回起始位置。
                    # 到达航点后应使用当前位置悬停命令锁定当前点。
                    self._hover()
                    return True, "reached"
            else:
                stable_since = None
            if (now - started).to_sec() >= travel_timeout:
                self._hover()
                return False, "waypoint_timeout"
            rate.sleep()
        return False, "ros_shutdown"

    def _run_takeoff(self, payload: Dict[str, Any]) -> bool:
        ready, reason = self._ready()
        if not ready:
            self._publish_status("takeoff_failed", error=reason)
            return False
        state, _ = self._snapshot()
        assert state is not None
        if getattr(self, "identity", {}):
            try:
                self._refresh_flight_speed_limit()
                requested = float(self._assignment["task"].get("speed_mps", self.search_speed))
                if max(requested, self.return_speed) > min(self.max_speed, self.flight_speed_limit):
                    raise ValueError("任务航速超过已读取的飞控限速，请调整规划与已核验的配置")
            except (ValueError, rospy.ROSException, rospy.ServiceException) as error:
                self._publish_status("takeoff_failed", error=str(error))
                return False
        target_z = float(payload["target_altitude_m"])
        self._home = (
            float(state.position[0]),
            float(state.position[1]),
            float(state.position[2]),
            float(state.attitude[2]),
        )
        global_task = bool(self._assignment and self._assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST")
        if global_task:
            try:
                self._gps_home = self._valid_gps(state)
                points = resolve_waypoints(self._assignment["task"], self._home, self._gps_home, self.max_distance_from_home)
                self._progress["resolved_waypoints"] = points
                target_z += self._home[2]
            except ValueError as error:
                self._publish_status("takeoff_failed", error=str(error))
                return False
        elif self._assignment:
            try:
                resolve_waypoints(self._assignment["task"], self._home, None, self.max_distance_from_home)
            except ValueError as error:
                self._publish_status("takeoff_failed", error=str(error))
                return False
        if self._assignment and self._assignment.get("controller_mode", "internal") == "external":
            external_frame = str(self._assignment.get("coordinate_frame") or "WGS84").upper()
            if external_frame == "WGS84":
                lat = float(getattr(state, "latitude", 0.0))
                lon = float(getattr(state, "longitude", 0.0))
                alt = float(getattr(state, "altitude", 0.0))
                if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(alt)
                        and abs(lat) > 1e-6 and abs(lon) > 1e-6):
                    self._publish_status("takeoff_failed", error="GPS latitude/longitude is invalid")
                    return False
                self._gps_home = (lat, lon, alt)
            else:
                # 实验室外部程序 B 使用相对起飞点的 ENU，不需要 GPS。
                self._gps_home = None
        self._checkpoint("taking_off", next_waypoint=0, execution=dict(payload))
        self._publish_status("taking_off", target_altitude_m=target_z)
        ok, reason = self._fly_to(
            self._home[0], self._home[1], target_z, self._home[3], self.search_speed
        )
        if ok:
            self._publish_status("at_altitude", target_altitude_m=target_z)
            return True
        else:
            self._publish_status("takeoff_failed", error=reason)
            return False

    def _publish_external_mission(self, payload: Dict[str, Any]) -> None:
        """向程序 B 发布经纬度或 ENU 航点，高度统一相对起飞点。"""
        with self._lock:
            assignment = self._assignment
            gps_home = self._gps_home
        if assignment is None:
            raise ValueError("external mission is not assigned")
        frame = str(assignment.get("coordinate_frame") or "WGS84").upper()
        if frame not in ("WGS84", "ENU"):
            raise ValueError("external mission coordinate frame must be WGS84 or ENU")
        if frame == "WGS84" and gps_home is None:
            raise ValueError("GPS home is not available for WGS84 external mission")
        lat0, lon0, _ = gps_home if gps_home is not None else (0.0, 0.0, 0.0)
        target_relative_alt = float(assignment["target_altitude_m"])
        task = assignment["task"]
        points = []
        direct = task.get("waypoints_gps") or task.get("waypoints_wgs84")
        if frame == "WGS84":
            if direct:
                for point in direct:
                    points.append({
                        "latitude": float(point[0] if isinstance(point, (list, tuple)) else point["latitude"]),
                        "longitude": float(point[1] if isinstance(point, (list, tuple)) else point["longitude"]),
                        # 规划高度是唯一高度源，避免旧航点的绝对海拔被透传。
                        "altitude_m": target_relative_alt,
                    })
            else:
                cos_lat = max(1e-6, abs(math.cos(math.radians(lat0))))
                for x, y in task["waypoints_m"]:
                    points.append({
                        "latitude": lat0 + float(x) / 111111.0,
                        "longitude": lon0 - float(y) / (111111.0 * cos_lat),
                        "altitude_m": target_relative_alt,
                    })
        else:
            points = [{"x_m": float(x), "y_m": float(y), "z_m": target_relative_alt}
                      for x, y in task["waypoints_m"]]
        message = {
            "type": "external_mission",
            "uav_id": self.uav_id,
            "mission_id": assignment["mission_id"],
            "assignment_checksum": assignment["assignment_checksum"],
            "flight_profile": assignment.get("flight_profile", "competition"),
            "coordinate_frame": frame,
            "altitude_frame": "RELATIVE_TO_TAKEOFF",
            "target_altitude_m": target_relative_alt,
            "relative_altitude_m": target_relative_alt,
            "waypoints": points,
            "speed_mps": task.get("speed_mps", self.search_speed),
            "waypoint_actions": task.get("waypoint_actions", []),
            "reconnaissance_mode": task.get("reconnaissance_mode", "continuous"),
            "hover_scan_seconds": task.get("hover_scan_seconds", 0),
            "return_home": ({
                "latitude": lat0, "longitude": lon0, "altitude_m": target_relative_alt,
            } if frame == "WGS84" else {
                "x_m": 0.0, "y_m": 0.0, "z_m": target_relative_alt,
            }),
            "deadline_at": payload.get("deadline_at"),
            "return_battery_threshold": payload.get("return_battery_threshold"),
        }
        path_msg = Float64MultiArray()
        if frame == "WGS84":
            path_msg.data = [value for point in points for value in (
                point["latitude"], point["longitude"], point["altitude_m"]
            )]
        else:
            path_msg.data = [value for point in points for value in (
                point["x_m"], point["y_m"], point["z_m"]
            )]
        self.external_path_pub.publish(path_msg)
        landing = assignment.get("landing_point_wgs84") if frame == "WGS84" else assignment.get("landing_point_m")
        if frame == "WGS84" and landing is None and assignment.get("landing_point_m") is not None:
            px, py = assignment["landing_point_m"]
            cos_lat = max(1e-6, abs(math.cos(math.radians(lat0))))
            landing = [
                lat0 + float(px) / 111111.0,
                lon0 - float(py) / (111111.0 * cos_lat),
                target_relative_alt,
            ]
        if landing is None:
            # 固定比赛方案的降落点就是记录的起飞点。外部程序 B 仍需收到
            # jiangluodian，因此没有显式降落点时使用任务原点/起飞点。
            landing = (
                [lat0, lon0, target_relative_alt]
                if frame == "WGS84"
                else [0.0, 0.0, target_relative_alt]
            )
        if landing is not None:
            landing_msg = Float64MultiArray()
            if frame == "WGS84":
                landing_msg.data = [float(landing[0]), float(landing[1]), target_relative_alt]
            else:
                landing_msg.data = [float(landing[0]), float(landing[1]), target_relative_alt]
            self.external_landing_pub.publish(landing_msg)
        self.external_mission_pub.publish(
            String(data=json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        )

    def _publish_recon_start_mode(self, mode: int) -> None:
        message = Int32()
        message.data = 1 if int(mode) == 1 else 0
        self.recon_start_mode_pub.publish(message)
        self._publish_status("recon_start_mode", mode=message.data)

    def _scan_requires_ack(self) -> bool:
        return (
            self.vehicle_config.get("scan_mode", "external_ack") == "external_ack"
            and str((self._assignment or {}).get("flight_profile", "competition")).lower()
            not in ("lab", "lab10")
        )

    def _run_task(self, payload: Dict[str, Any]) -> bool:
        with self._lock:
            assignment = self._assignment
        if assignment is None:
            return False
        if assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST" and (self._home is None or self._gps_home is None):
            self._publish_status("task_failed", error="地图任务尚未记录起飞锚点")
            return False
        requested_speed = float(assignment["task"].get("speed_mps", self.search_speed))
        if requested_speed > min(getattr(self, "max_speed", requested_speed), getattr(self, "flight_speed_limit", None) or requested_speed):
            self._publish_status("task_failed", error="缓存任务的航速超过当前配置或飞控上限")
            return False
        state, _ = self._snapshot()
        if state is None:
            self._publish_status("task_failed", error="UAV state is missing")
            return False
        target_z = self._target_z(assignment)
        if (
            abs(float(state.position[2]) - target_z) > self.altitude_tolerance
            or abs(float(state.velocity[2])) > self.velocity_tolerance
        ):
            self._publish_status(
                "task_failed",
                error="UAV has not stabilized at its assigned altitude",
                target_altitude_m=target_z,
                actual_altitude_m=float(state.position[2]),
            )
            return False
        yaw = float(state.attitude[2])
        waypoints = self._progress.get("resolved_waypoints") if assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST" else assignment["task"]["waypoints_m"]
        if not waypoints:
            self._publish_status("task_failed", error="缺少已核验的本地 ENU 航点，必须重新记录起飞锚点")
            return False
        deadline = payload.get("deadline_at")
        threshold = payload.get("return_battery_threshold")
        self._publish_status(
            "departing_to_first_waypoint", first_waypoint=waypoints[0]
        )
        self._publish_status("executing", waypoint_count=len(waypoints))
        for index in range(int(self._progress.get("next_waypoint", 0)), len(waypoints)):
            point = waypoints[index]
            self._checkpoint("executing", next_waypoint=index, execution=dict(payload))
            self._publish_status("executing", waypoint_index=index, waypoint=point)
            ok, reason = self._fly_to(
                point[0],
                point[1],
                target_z,
                yaw,
                float(assignment["task"].get("speed_mps", self.search_speed)),
                float(deadline) if deadline is not None else None,
                float(threshold) if threshold is not None else None,
            )
            if ok and assignment["task"].get("reconnaissance_mode") == "hover_scan":
                ok, reason = self._scan_waypoint(index, point, target_z, yaw, deadline, threshold)
            if not ok:
                if reason in ("time_limit", "low_battery"):
                    self._publish_status("return_required", reason=reason)
                    self._run_return({"reason": reason, "land_after_return": True})
                elif reason != "preempted":
                    self._publish_status("task_failed", error=reason, waypoint_index=index)
                return False
            self._checkpoint("executing", next_waypoint=index + 1)
        self._hover()
        self._publish_status("completed", waypoint_count=len(waypoints))
        self._run_return({"reason": "task_complete", "land_after_return": True})
        return True

    @staticmethod
    def _valid_gps(state):
        if state is None or not state.odom_valid or int(getattr(state, "gps_status", 0)) < 3:
            raise ValueError("地图任务需要有效的三维 GPS 定位")
        if int(getattr(state, "location_source", -1)) not in (4, 5):
            raise ValueError("地图任务的本地 ENU 锚点需使用 GPS/RTK 定位源")
        gps = tuple(float(getattr(state, name, float("nan"))) for name in ("latitude", "longitude", "altitude"))
        if not all(math.isfinite(v) for v in gps) or abs(gps[0]) > 90 or abs(gps[1]) > 180:
            raise ValueError("飞控 GPS 数据无效")
        return gps

    def _refresh_flight_speed_limit(self):
        from mavros_msgs.srv import ParamGet
        service = "/uav%d/mavros/param/get" % self.local_ros_uav_id
        rospy.wait_for_service(service, timeout=5)
        result = rospy.ServiceProxy(service, ParamGet)(param_id="MPC_XY_VEL_MAX")
        value = float(result.value.real or result.value.integer)
        if not result.success or not math.isfinite(value) or value <= 0:
            raise ValueError("无法读取飞控 MPC_XY_VEL_MAX；不自动修改飞控参数")
        self.flight_speed_limit = value

    def _target_z(self, assignment):
        relative = float(assignment["target_altitude_m"])
        return relative + self._home[2] if assignment["task"]["coordinate_frame"] == "LOCAL_NORTH_WEST" and self._home else relative

    def _scan_callback(self, message):
        try:
            payload = json.loads(message.data)
            if not isinstance(payload, dict):
                return
            with self._lock:
                if self._scan_session and payload.get("uav_id") == self.uav_id and payload.get("mission_id") == self._assignment["mission_id"]:
                    self._scan_session.accept(payload)
        except (ValueError, TypeError, KeyError):
            pass

    def _scan_waypoint(self, index, point, target_z, yaw, deadline, threshold):
        cfg = self.vehicle_config
        duration = float(self._assignment["task"]["hover_scan_seconds"])
        request_id = uuid.uuid4().hex
        session = ScanSession(
            request_id,
            duration,
            cfg.get("scan_timeout_sec", 30),
            time.monotonic(),
            self._scan_requires_ack(),
        )
        with self._lock:
            self._scan_session = session
        request = {"uav_id": self.uav_id, "mission_id": self._assignment["mission_id"],
                   "waypoint_index": index, "request_id": request_id, "duration_s": duration}
        self.scan_pub.publish(String(data=json.dumps(request, ensure_ascii=False)))
        self._publish_status("scanning", waypoint_index=index, request_id=request_id)
        rate = rospy.Rate(self.command_rate)
        drift_since = None
        drift_warning_sent = False
        try:
            while not rospy.is_shutdown():
                if self._abort_motion.is_set():
                    return False, "preempted"
                ready, reason = self._ready()
                if not ready:
                    return False, reason
                state, _ = self._snapshot()
                if deadline is not None and time.time() >= float(deadline):
                    return False, "time_limit"
                if threshold is not None and state.battery_percetage <= float(threshold):
                    return False, "low_battery"
                horizontal_error = math.hypot(
                    state.position[0] - point[0], state.position[1] - point[1]
                )
                vertical_error = abs(state.position[2] - target_z)
                hard_drift = (
                    horizontal_error > self.scan_hard_position_tolerance
                    or vertical_error > self.scan_hard_altitude_tolerance
                )
                if hard_drift:
                    now = time.monotonic()
                    if drift_since is None:
                        drift_since = now
                    elif now - drift_since >= self.scan_drift_grace_seconds:
                        return False, "悬停扫描期间持续偏离航点（超过硬限制）"
                    if not drift_warning_sent:
                        self._publish_status(
                            "scan_drift_warning",
                            waypoint_index=index,
                            horizontal_error_m=horizontal_error,
                            vertical_error_m=vertical_error,
                        )
                        drift_warning_sent = True
                elif (
                    horizontal_error > self.scan_position_tolerance
                    or vertical_error > self.scan_altitude_tolerance
                ):
                    # 软范围内的抖动不再中断扫描，只记录一次提示并继续发送悬停指令。
                    if not drift_warning_sent:
                        self._publish_status(
                            "scan_drift_warning",
                            waypoint_index=index,
                            horizontal_error_m=horizontal_error,
                            vertical_error_m=vertical_error,
                        )
                        drift_warning_sent = True
                    drift_since = None
                else:
                    drift_since = None
                    drift_warning_sent = False
                # 扫描阶段不再发送 XY_VEL_Z_POS 的零速度命令。该命令在
                # Prometheus 内部可能回到速度控制起始点；Current_Pos_Hover
                # 才是“保持当前航点”的明确指令。
                self._hover()
                status = session.state(time.monotonic())
                if status == "completed":
                    return True, "scanned"
                if status in ("failed", "timeout"):
                    return False, "扫描失败或未收到扫描完成确认"
                rate.sleep()
            return False, "ros_shutdown"
        finally:
            self._hover()
            with self._lock:
                self._scan_session = None

    def _run_return(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            assignment = self._assignment
        if assignment and assignment.get("controller_mode", "internal") == "external":
            # 外部程序 B 独占返航和降落。地面端的返航命令经 TCP 到达机载端后，
            # 只在固定 ROS 话题发布一个 Bool=true，避免向程序 B 重复传递任务数据。
            self.external_return_pub.publish(Bool(data=True))
            self._checkpoint("external_return_requested", reason=payload.get("reason", "unknown"))
            self._publish_status("returning", controller_mode="external", reason=payload.get("reason", "unknown"))
            rospy.loginfo("已在 %s 收到外部程序返航请求", self.external_return_topic)
            return
        if self._home is None:
            self._publish_status("return_failed", error="home position was not recorded")
            return
        state, _ = self._snapshot()
        if state is None:
            self._publish_status("return_failed", error="UAV state is missing")
            return
        target_z = max(float(state.position[2]), self._target_z(self._assignment))
        self._checkpoint("returning")
        self._publish_status("returning", reason=payload.get("reason", "unknown"))
        ok, reason = self._fly_to(
            self._home[0],
            self._home[1],
            target_z,
            self._home[3],
            self.return_speed,
            check_abort=False,
        )
        if not ok:
            self._publish_status("return_failed", error=reason)
            return
        if bool(payload.get("land_after_return", True)):
            self._checkpoint("landing")
            self._land()
            self._publish_status("land_commanded")
        else:
            self._hover()
            self._publish_status("returned")


def main() -> None:
    rospy.init_node("su17_competition_executor", anonymous=False)
    OnboardTaskExecutor()
    rospy.spin()


if __name__ == "__main__":
    try:
        main()
    except rospy.ROSInterruptException:
        pass

