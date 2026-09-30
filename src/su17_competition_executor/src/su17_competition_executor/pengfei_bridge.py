"""只读采集外部程序 B 的三个 ROS 话题，供竞赛遥测链路显示。"""

from __future__ import annotations

import copy
import importlib
import math
import os
from pathlib import Path
import site
import sys
import threading
import time


TOPIC_MESSAGES = {
    "current_target": "PengfeiCurrentTarget",
    "last_recognition": "PengfeiLastRecognition",
    "node_status": "PengfeiNodeStatus",
}

TOPIC_FIELDS = {
    "current_target": {
        "text": ("target_id", "target_type"),
        "number": (
            "latitude_deg", "longitude_deg", "altitude_gps_m",
            "velocity_north_mps", "velocity_east_mps",
        ),
        "integer": (),
    },
    "last_recognition": {
        "text": ("target_id", "target_type"),
        "number": ("latitude_deg", "longitude_deg"),
        "integer": (),
    },
    "node_status": {
        "text": (
            "scheduler_state", "maneuver_state", "gimbal_state", "follower_state",
        ),
        "number": (),
        "integer": ("scan_point_number",),
    },
}


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _generated_message_path():
    """程序 B 单独编译时，不要求它先于竞赛栈 source。"""
    workspace = Path(
        os.environ.get("COMPETITION_PROGRAM_B_WORKSPACE", str(Path.home() / "recon_ws"))
    ).expanduser()
    candidate = workspace / "devel" / "lib" / "python3" / "dist-packages"
    if candidate.is_dir():
        # P600 厂家工作空间也可能包含同名包；优先读取最新程序 B 的消息生成物。
        site.addsitedir(str(candidate))
        try:
            sys.path.remove(str(candidate))
        except ValueError:
            pass
        sys.path.insert(0, str(candidate))


class PengfeiReadOnlyBridge:
    """保留最后一组 ROS 消息；未收到时不伪造目标或节点状态。"""

    def __init__(self, ros, uav_id: int, retry_seconds: float = 5.0):
        self._ros = ros
        self._uav_id = int(uav_id)
        self._lock = threading.RLock()
        self._values = {key: None for key in TOPIC_MESSAGES}
        self._subscribers = []
        self._last_warning = 0.0
        self._connect()
        if len(self._subscribers) != len(TOPIC_MESSAGES):
            self._retry_timer = ros.Timer(
                ros.Duration(float(retry_seconds)), self._retry_connect
            )

    def _warn(self, error):
        now = time.monotonic()
        if now - self._last_warning >= 30.0 or not self._last_warning:
            self._last_warning = now
            self._ros.logwarn(
                "程序 B 只读状态暂不可用，竞赛任务和飞行控制继续运行：%s", error
            )

    def _connect(self):
        if len(self._subscribers) == len(TOPIC_MESSAGES):
            return
        try:
            _generated_message_path()
            messages = importlib.import_module("px4_north_camera.msg")
            classes = {
                key: getattr(messages, name) for key, name in TOPIC_MESSAGES.items()
            }
            new_subscribers = []
            for key, message_class in classes.items():
                topic = "/uav{}/pengfei/{}".format(self._uav_id, key)
                new_subscribers.append(
                    self._ros.Subscriber(
                        topic, message_class,
                        lambda message, topic_key=key: self._receive(topic_key, message),
                        queue_size=1,
                    )
                )
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError) as error:
            for subscriber in locals().get("new_subscribers", []):
                subscriber.unregister()
            self._warn(error)
            return
        self._subscribers = new_subscribers
        self._ros.loginfo("程序 B 只读状态已订阅：UAV%d，三个话题", self._uav_id)

    def _retry_connect(self, _event):
        if len(self._subscribers) == len(TOPIC_MESSAGES):
            self._retry_timer.shutdown()
            return
        self._connect()
        if len(self._subscribers) == len(TOPIC_MESSAGES):
            self._retry_timer.shutdown()

    def _receive(self, key, message):
        fields = TOPIC_FIELDS[key]
        value = {
            field: str(getattr(message, field, ""))[:256]
            for field in fields["text"]
        }
        value.update(
            (field, _finite_or_none(getattr(message, field, None)))
            for field in fields["number"]
        )
        value.update(
            (field, int(getattr(message, field, 0)))
            for field in fields["integer"]
        )
        value["received_at_unix"] = time.time()
        with self._lock:
            self._values[key] = value

    def snapshot(self):
        with self._lock:
            payload = copy.deepcopy(self._values)
        payload["sent_at_unix"] = time.time()
        return payload
