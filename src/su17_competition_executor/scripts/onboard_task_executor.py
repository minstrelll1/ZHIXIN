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
from std_msgs.msg import String, Float64MultiArray, Int32

from su17_competition_executor.tcp_link import OnboardTcpLink
from su17_competition_executor.task_protocol import (
    TaskValidationError,
    load_assignment,
    save_assignment_atomic,
    validate_assignment,
)


class OnboardTaskExecutor:
    def __init__(self) -> None:
        # uav_id is the fleet identity used by ground TCP/task routing.
        # SU17 vendor stacks on every aircraft use /uav1 locally, therefore the
        # local ROS namespace is deliberately configured independently.
        self.uav_id = int(rospy.get_param("~uav_id", 1))
        self.local_ros_uav_id = int(rospy.get_param("~local_ros_uav_id", 1))
        self.enable_motion = bool(rospy.get_param("~enable_motion", False))
        self.transport = str(rospy.get_param("~transport", "tcp")).strip().lower()
        self.ground_host = str(rospy.get_param("~ground_host", "192.168.1.123"))
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
        ).strip() or "/ground_mission_planner/vehicle_1/path_stage_1"
        self.external_landing_topic = str(
            rospy.get_param("~external_landing_topic", "")
        ).strip() or "/ground_mission_planner/vehicle_1/jiangluodian"
        self.restart_all_command = str(rospy.get_param("~restart_all_command", "")).strip()
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

        if self.transport in ("tcp", "both"):
            self.tcp_link = OnboardTcpLink(
                uav_id=self.uav_id,
                ground_host=self.ground_host,
                ground_port=self.ground_port,
                auth_token=self.auth_token,
                command_handler=self._handle_high_level_payload,
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
            "UAV%d competition executor ready; local_ros=/uav%d, transport=%s, "
            "ground=%s:%d, enable_motion=%s",
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
            rospy.logwarn("Ignoring invalid external status: %s", error)

    def _validate_parameters(self) -> None:
        if self.uav_id <= 0:
            raise ValueError("~uav_id must be positive")
        if self.local_ros_uav_id <= 0:
            raise ValueError("~local_ros_uav_id must be positive")
        for label, value in (
            ("search_speed_mps", self.search_speed),
            ("return_speed_mps", self.return_speed),
        ):
            if not 0.0 < value <= 2.0:
                raise ValueError("~{} must be in (0, 2.0]".format(label))
        if self.max_distance_from_home <= 0.0:
            raise ValueError("~max_distance_from_home_m must be positive")
        if not 5.0 <= self.command_rate <= 50.0:
            raise ValueError("~command_rate_hz must be in [5, 50]")
        if self.transport not in ("tcp", "ros", "both"):
            raise ValueError("~transport must be tcp, ros, or both")
        if not 1.0 <= self.telemetry_rate <= 20.0:
            raise ValueError("~telemetry_rate_hz must be in [1, 20]")
        if not 1 <= self.ground_port <= 65535:
            raise ValueError("~ground_port must be a valid TCP port")

    def _state_callback(self, message: UAVState) -> None:
        with self._lock:
            self._state = message
            self._state_received = time.monotonic()

    def _control_callback(self, message: UAVControlState) -> None:
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
            index = int(progress["next_waypoint"])
            if not 0 <= index <= len(assignment["task"]["waypoints_m"]):
                raise ValueError("invalid saved waypoint")
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
            rospy.logerr("Task recovery rejected: %s", error)

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
            self._checkpoint("external_waiting", execution=dict(payload))
            self._publish_status("external_mission_published", controller_mode="external")
            return True
        if phase == "returning" or (payload.get("deadline_at") is not None and time.time() >= payload["deadline_at"]):
            self._run_return({"reason": "recovery", "land_after_return": True})
            return True
        if phase == "taking_off":
            if self._home is None:
                return False
            ok, _ = self._fly_to(self._home[0], self._home[1], float(self._assignment["target_altitude_m"]), self._home[3], self.search_speed)
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
        self.tcp_link.update_telemetry(
            {
                "type": "telemetry",
                "uav_id": self.uav_id,
                "timestamp_unix": time.time(),
                "connected": bool(state.connected),
                "armed": bool(state.armed),
                "odom_valid": bool(state.odom_valid),
                "failsafe": bool(control.failsafe),
                "control_state": int(control.control_state),
                "battery_percentage": float(state.battery_percetage),
                "position": [float(value) for value in state.position],
                "velocity": [float(value) for value in state.velocity],
                "task_phase": self._progress.get("phase", "idle"),
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
            rospy.logerr("High-level command rejected: %s", error)
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
                        raise ValueError("restart_all_command is not executable")
                    subprocess.Popen([self.restart_all_command], close_fds=True)
                else:
                    rospy.logwarn("restart_all_command is not configured; restarting executor only")
                threading.Timer(0.5, lambda: rospy.signal_shutdown("operator restart all")).start()
            elif command_type == "restart_takeoff":
                if self._assignment is None:
                    raise ValueError("no saved assignment to restart")
                with self._lock:
                    if self._motion_thread is not None and self._motion_thread.is_alive():
                        raise ValueError("executor is busy")
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
            rospy.logerr("High-level command rejected: %s", error)
            self._publish_status("command_rejected", error=str(error))

    def _accept_assignment(self, payload: Dict[str, Any]) -> None:
        try:
            assignment = validate_assignment(payload, self.uav_id)
            if self._assignment and assignment["assignment_checksum"] == self._assignment["assignment_checksum"]:
                self._publish_status("task_received")
                return
            if self._motion_thread is not None and self._motion_thread.is_alive():
                raise TaskValidationError("cannot replace an active mission")
            waypoints = assignment["task"]["waypoints_m"]
            if any(math.hypot(point[0], point[1]) > self.max_distance_from_home for point in waypoints):
                raise TaskValidationError(
                    "waypoint exceeds max_distance_from_home_m safety limit"
                )
            save_assignment_atomic(self.cache_path, assignment)
        except (OSError, TaskValidationError) as error:
            self._assignment_acked = False
            rospy.logerr("Task assignment rejected: %s", error)
            self._publish_status("task_rejected", error=str(error))
            return

        with self._lock:
            self._assignment = assignment
            self._assignment_acked = True
            self._home = None
            self._gps_home = None
            self._resume_pending = False
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
            "Task received: mission=%s, UAV%d, waypoints=%d, cached=%s",
            assignment["mission_id"],
            self.uav_id,
            len(waypoints),
            self.cache_path,
        )
        rospy.loginfo(
            "Complete task SHA-256: %s", assignment["assignment_checksum"]
        )
        if self.log_full_task:
            rospy.loginfo(
                "Complete assigned task JSON:\n%s",
                json.dumps(assignment, ensure_ascii=False, indent=2, sort_keys=True),
            )

    def _accept_motion_command(self, name: str, payload: Dict[str, Any], target: Any) -> None:
        if not self.enable_motion:
            self._publish_status("motion_disabled", command=name)
            rospy.logwarn("Ignoring %s because enable_motion is false", name)
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
                    "Execute command queued; UAV%d will start its first waypoint "
                    "immediately after reaching %.2fm",
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
        thread.start()

    def _motion_entry(self, name: str, target: Any, payload: Dict[str, Any]) -> None:
        try:
            succeeded = bool(target(payload))
        except Exception as error:
            succeeded = False
            rospy.logerr("%s execution crashed: %s", name, error)
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
                "UAV%d reached its assigned altitude; departing to first waypoint now",
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
        state, control = self._snapshot()
        if state is None or control is None:
            return False, "waiting for UAV state"
        if time.monotonic() - min(self._state_received, self._control_received) > 2.0:
            return False, "UAV state is stale"
        if not state.connected:
            return False, "flight controller is disconnected"
        if not state.armed:
            return False, "UAV is not armed"
        if not state.odom_valid:
            return False, "odometry is invalid"
        if control.control_state != UAVControlState.COMMAND_CONTROL:
            return False, "control state is not COMMAND_CONTROL"
        if control.pos_controller != UAVControlState.PX4_ORIGIN:
            return False, "position controller is not PX4_ORIGIN"
        if control.failsafe:
            return False, "failsafe is active"
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
        started = rospy.Time.now()
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
                    self._publish_velocity(0.0, 0.0, target_z, yaw)
                    return True, "reached"
            else:
                stable_since = None
            if (now - started).to_sec() >= self.waypoint_timeout:
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
        target_z = float(payload["target_altitude_m"])
        self._home = (
            float(state.position[0]),
            float(state.position[1]),
            float(state.position[2]),
            float(state.attitude[2]),
        )
        if self._assignment and self._assignment.get("controller_mode", "internal") == "external":
            lat = float(getattr(state, "latitude", 0.0))
            lon = float(getattr(state, "longitude", 0.0))
            alt = float(getattr(state, "altitude", 0.0))
            if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(alt)
                    and abs(lat) > 1e-6 and abs(lon) > 1e-6):
                self._publish_status("takeoff_failed", error="GPS latitude/longitude is invalid")
                return False
            self._gps_home = (lat, lon, alt)
        self._checkpoint("taking_off", next_waypoint=0, execution=dict(payload))
        self._publish_status("taking_off", target_altitude_m=target_z)
        ok, reason = self._fly_to(
            self._home[0], self._home[1], target_z, self._home[3], self.search_speed
        )
        if ok:
            start_mode = Int32()
            start_mode.data = int(payload.get("recon_start_mode", 0))
            if start_mode.data not in (0, 1):
                start_mode.data = 0
            self.recon_start_mode_pub.publish(start_mode)
            self._publish_status("recon_start_mode", mode=int(start_mode.data))
            self._publish_status("at_altitude", target_altitude_m=target_z)
            return True
        else:
            self._publish_status("takeoff_failed", error=reason)
            return False

    def _publish_external_mission(self, payload: Dict[str, Any]) -> None:
        """Publish a WGS84 mission for external autonomous program B."""
        with self._lock:
            assignment = self._assignment
            gps_home = self._gps_home
        if assignment is None or gps_home is None:
            raise ValueError("GPS home is not available for external mission")
        lat0, lon0, alt0 = gps_home
        target_relative_alt = float(assignment["target_altitude_m"])
        task = assignment["task"]
        direct = task.get("waypoints_gps") or task.get("waypoints_wgs84")
        points = []
        if direct:
            for point in direct:
                points.append({
                    "latitude": float(point[0] if isinstance(point, (list, tuple)) else point["latitude"]),
                    "longitude": float(point[1] if isinstance(point, (list, tuple)) else point["longitude"]),
                    "altitude_m": float(point[2] if isinstance(point, (list, tuple)) else point.get("altitude_m", alt0 + target_relative_alt)),
                })
        else:
            cos_lat = max(1e-6, abs(math.cos(math.radians(lat0))))
            # Planner convention: +X is north/up on the map and +Y is west/left.
            for x, y in task["waypoints_m"]:
                points.append({
                    "latitude": lat0 + float(x) / 111111.0,
                    "longitude": lon0 - float(y) / (111111.0 * cos_lat),
                    "altitude_m": alt0 + target_relative_alt,
                })
        absolute_target_alt = float(points[0]["altitude_m"]) if points else alt0 + target_relative_alt
        message = {
            "type": "external_mission",
            "uav_id": self.uav_id,
            "mission_id": assignment["mission_id"],
            "assignment_checksum": assignment["assignment_checksum"],
            "coordinate_frame": "WGS84",
            "target_altitude_m": absolute_target_alt,
            "relative_altitude_m": target_relative_alt,
            "waypoints": points,
            "return_home": {
                "latitude": lat0,
                "longitude": lon0,
                "altitude_m": alt0,
            },
            "deadline_at": payload.get("deadline_at"),
            "return_battery_threshold": payload.get("return_battery_threshold"),
        }
        path_msg = Float64MultiArray()
        path_msg.data = [value for point in points for value in (
            point["latitude"], point["longitude"], point["altitude_m"]
        )]
        self.external_path_pub.publish(path_msg)
        landing = assignment.get("landing_point_wgs84")
        if landing is None and assignment.get("landing_point_m") is not None:
            px, py = assignment["landing_point_m"]
            cos_lat = max(1e-6, abs(math.cos(math.radians(lat0))))
            landing = [
                lat0 + float(px) / 111111.0,
                lon0 - float(py) / (111111.0 * cos_lat),
                alt0 + target_relative_alt,
            ]
        if landing is not None:
            landing_msg = Float64MultiArray()
            landing_msg.data = [float(value) for value in landing] + [
                float(assignment.get("landing_sequence", 0))
            ]
            self.external_landing_pub.publish(landing_msg)
        self.external_mission_pub.publish(
            String(data=json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        )

    def _run_task(self, payload: Dict[str, Any]) -> bool:
        with self._lock:
            assignment = self._assignment
        if assignment is None:
            return False
        state, _ = self._snapshot()
        if state is None:
            self._publish_status("task_failed", error="UAV state is missing")
            return False
        target_z = float(assignment["target_altitude_m"])
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
        waypoints = assignment["task"]["waypoints_m"]
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
                self.search_speed,
                float(deadline) if deadline is not None else None,
                float(threshold) if threshold is not None else None,
            )
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

    def _run_return(self, payload: Dict[str, Any]) -> None:
        if self._home is None:
            self._publish_status("return_failed", error="home position was not recorded")
            return
        state, _ = self._snapshot()
        if state is None:
            self._publish_status("return_failed", error="UAV state is missing")
            return
        target_z = max(float(state.position[2]), float(self._assignment["target_altitude_m"]))
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
