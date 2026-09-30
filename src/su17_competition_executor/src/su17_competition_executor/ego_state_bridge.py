"""只读订阅 EGO 规划器状态；不依赖厂商或外部程序的消息包。"""

from __future__ import annotations

import importlib
import threading
import time


class EgoStateReadOnlyBridge:
    """保存最后一次真正收到的状态，遥测重发不会刷新其年龄。"""

    def __init__(self, ros, uav_id: int):
        self._ros = ros
        self._lock = threading.RLock()
        self._state = None
        self._received = None
        self._message_classes = {}
        self._last_warning = 0.0
        self.topic = "/uav{}/ego_planner/exec_state".format(int(uav_id))
        self._subscriber = None
        try:
            self._subscriber = ros.Subscriber(
                self.topic, ros.AnyMsg, self._receive, queue_size=1
            )
        except Exception as error:
            # EGO 状态仅供显示，缺失时不能阻断竞赛执行器启动。
            self._warn(error)

    def _warn(self, reason):
        now = time.monotonic()
        if now - self._last_warning >= 30.0 or not self._last_warning:
            self._last_warning = now
            self._ros.logwarn("EGO 状态消息无法解析（%s）：%s", self.topic, reason)

    def _receive(self, raw_message):
        # 厂商仓库未给出此话题的发布类型。按 TCPROS 消息头解析，兼容
        # std_msgs/Int8、Int32 等整数消息，且不改动规划器本身。
        message_type = getattr(raw_message, "_connection_header", {}).get("type", "")
        if not message_type:
            self._warn("缺少 ROS 消息类型")
            return
        try:
            message_class = self._message_classes.get(message_type)
            if message_class is None:
                message_class = importlib.import_module("roslib.message").get_message_class(message_type)
                if message_class is None:
                    raise ValueError("未知消息类型 " + message_type)
                self._message_classes[message_type] = message_class
            message = message_class()
            message.deserialize(raw_message._buff)
            value = getattr(message, "data", None)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
                raise ValueError("状态不是 0～6 的整数")
        except Exception as error:
            self._warn(error)
            return
        with self._lock:
            self._state = value
            self._received = time.monotonic()

    def snapshot(self):
        with self._lock:
            if self._received is None:
                return None, None
            return self._state, max(0.0, time.monotonic() - self._received)
