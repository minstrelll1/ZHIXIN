from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Dict

from .adapter import FleetAdapter
from .models import Telemetry


class RosFleetAdapter(FleetAdapter):
    """ROS1 bridge for high-level per-UAV competition commands and telemetry."""

    def __init__(self, uav_ids: list) -> None:
        super().__init__()
        self.uav_ids = list(uav_ids)
        self._lock = threading.Lock()
        self._latest: Dict[int, Telemetry] = {}
        self._publishers: Dict[int, Any] = {}
        self._rospy = None

    def start(self) -> None:
        try:
            import rospy
            from prometheus_msgs.msg import UAVControlState, UAVState
            from std_msgs.msg import String
        except ImportError as error:
            raise RuntimeError(
                "ROS adapter requires rospy, std_msgs, and prometheus_msgs"
            ) from error

        self._rospy = rospy
        if not rospy.core.is_initialized():
            rospy.init_node(
                "su17_competition_backend", anonymous=False, disable_signals=True
            )
        for uav_id in self.uav_ids:
            prefix = "/uav%d" % uav_id
            self._publishers[uav_id] = rospy.Publisher(
                prefix + "/competition/high_level_command",
                String,
                queue_size=10,
            )
            rospy.Subscriber(
                prefix + "/prometheus/state",
                UAVState,
                self._state_callback,
                callback_args=uav_id,
                queue_size=1,
            )
            rospy.Subscriber(
                prefix + "/prometheus/control_state",
                UAVControlState,
                self._control_callback,
                callback_args=uav_id,
                queue_size=1,
            )
            rospy.Subscriber(
                prefix + "/competition/task_status",
                String,
                self._task_callback,
                callback_args=uav_id,
                queue_size=10,
            )

    def _get(self, uav_id: int) -> Telemetry:
        telemetry = self._latest.get(uav_id)
        if telemetry is None:
            telemetry = Telemetry(uav_id=uav_id, received_at=time.time())
            self._latest[uav_id] = telemetry
        return telemetry

    def _state_callback(self, message: Any, uav_id: int) -> None:
        with self._lock:
            telemetry = self._get(uav_id)
            telemetry.received_at = time.time()
            telemetry.connected = bool(message.connected)
            telemetry.armed = bool(message.armed)
            telemetry.odom_valid = bool(message.odom_valid)
            telemetry.battery_percentage = float(message.battery_percetage)
            telemetry.position = [float(value) for value in message.position]
            telemetry.velocity = [float(value) for value in message.velocity]
            telemetry.gps_status = int(getattr(message, "gps_status", 0))
            telemetry.location_source = int(getattr(message, "location_source", -1))
            telemetry.gps_num = int(getattr(message, "gps_num", 0))
            for field_name in ("latitude", "longitude", "altitude", "rel_alt"):
                try:
                    value = float(getattr(message, field_name))
                except (AttributeError, TypeError, ValueError):
                    value = None
                setattr(telemetry, field_name, value if value is not None and math.isfinite(value) else None)
            snapshot = Telemetry(**telemetry.__dict__)
        self.emit_telemetry(snapshot)

    def _control_callback(self, message: Any, uav_id: int) -> None:
        with self._lock:
            telemetry = self._get(uav_id)
            telemetry.received_at = time.time()
            telemetry.control_state = int(message.control_state)
            telemetry.failsafe = bool(message.failsafe)
            snapshot = Telemetry(**telemetry.__dict__)
        self.emit_telemetry(snapshot)

    def _task_callback(self, message: Any, uav_id: int) -> None:
        assignment_mission_id = ""
        assignment_checksum = ""
        return_ack = None
        try:
            payload = json.loads(message.data)
            if payload.get("state") == "return_ack" and isinstance(payload.get("return_ack"), dict):
                return_ack = dict(payload["return_ack"])
            complete = payload.get("state") in ("completed", "done")
            assignment_acked = bool(payload.get("task_assignment_acked")) or payload.get("state") in (
                "task_received",
                "task_assigned",
                "accepted",
            )
            assignment_mission_id = str(payload.get("mission_id", ""))
            assignment_checksum = str(payload.get("assignment_checksum", ""))
        except (TypeError, ValueError):
            complete = str(message.data).strip().lower() in ("completed", "done")
            assignment_acked = str(message.data).strip().lower() in (
                "task_received",
                "task_assigned",
                "accepted",
            )
        with self._lock:
            telemetry = self._get(uav_id)
            telemetry.received_at = time.time()
            if return_ack is not None:
                telemetry.return_ack = return_ack
            telemetry.task_complete = telemetry.task_complete or complete
            telemetry.task_assignment_acked = (
                telemetry.task_assignment_acked or assignment_acked
            )
            if assignment_acked:
                if assignment_mission_id:
                    telemetry.task_assignment_mission_id = assignment_mission_id
                if assignment_checksum:
                    telemetry.task_assignment_checksum = assignment_checksum
            snapshot = Telemetry(**telemetry.__dict__)
        self.emit_telemetry(snapshot)

    def _publish(self, uav_id: int, command_type: str, payload: Dict[str, Any]) -> None:
        if self._rospy is None or uav_id not in self._publishers:
            raise RuntimeError("ROS adapter is not started")
        from std_msgs.msg import String

        body = {"type": command_type, **payload, "uav_id": uav_id}
        self._publishers[uav_id].publish(String(data=json.dumps(body, ensure_ascii=False)))

    def command_takeoff(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._publish(uav_id, "takeoff", payload)

    def command_assign_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._publish(uav_id, "assign_task", payload)

    def command_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._publish(uav_id, "execute_task", payload)

    def command_return(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._publish(uav_id, "return_home", payload)
