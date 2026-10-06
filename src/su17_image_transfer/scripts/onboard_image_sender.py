#!/usr/bin/env python3
"""Cache requested camera frames on disk, then transfer them to the ground."""

import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import threading
import time
import uuid
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from competition_shared.return_events import matches_return_event, competition_elapsed

import cv2
import message_filters
import numpy as np
import rospy
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import CompressedImage, Image, NavSatFix
from std_msgs.msg import String
from std_srvs.srv import Trigger, TriggerResponse

from su17_image_transfer.msg import TargetDetection, CompletedTargetArray
from su17_image_transfer.frame_cache import (FrameCache, detection_metadata,
                                             completed_target_metadata,
                                             clipped_rectangle)
from su17_image_transfer.protocol import encode_frame, receive_ack, receive_response


SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(value, fallback):
    cleaned = SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return (cleaned or fallback)[:120]


class OnboardImageSender:
    def __init__(self) -> None:
        self.uav_id = int(rospy.get_param("~uav_id", 1))
        if not 1 <= self.uav_id <= 6:
            raise ValueError("~uav_id must be between 1 and 6")
        self.local_ros_uav_id = int(rospy.get_param("~local_ros_uav_id", 1))
        if self.local_ros_uav_id <= 0:
            raise ValueError("~local_ros_uav_id must be positive")
        self.image_topic = rospy.get_param(
            "~image_topic",
            "/uav%d/gimbal/image_original" % self.local_ros_uav_id,
        )
        self.input_mode = str(rospy.get_param("~input_mode", "completed_target_array")).strip()
        self.compressed_input = bool(rospy.get_param("~compressed_input", False))
        self.ground_host = rospy.get_param("~ground_host", "192.168.1.230")
        self.ground_port = int(rospy.get_param("~ground_port", 56010))
        self.jpeg_quality = int(rospy.get_param("~jpeg_quality", 80))
        self.max_frame_age = float(rospy.get_param("~max_frame_age_sec", 2.0))
        self.connect_timeout = float(rospy.get_param("~connect_timeout_sec", 3.0))
        self.ack_timeout = float(rospy.get_param("~ack_timeout_sec", 5.0))
        self.max_retries = int(rospy.get_param("~max_retries", 3))
        self.retry_delay = float(rospy.get_param("~retry_delay_sec", 1.0))
        self.live_retry_interval = float(
            rospy.get_param("~live_retry_interval_sec", 5.0)
        )
        self.auth_token = str(rospy.get_param("~auth_token", ""))
        queue_size = int(rospy.get_param("~queue_size", 10))

        self.enable_offline_recovery = bool(
            rospy.get_param("~enable_offline_recovery", True)
        )
        self.reconcile_after_minutes = float(
            rospy.get_param("~reconcile_after_minutes", 23.5)
        )
        self.reconcile_retry_sec = float(
            rospy.get_param("~reconcile_retry_sec", 30.0)
        )
        self.cache_root = Path(
            os.path.expanduser(
                rospy.get_param(
                    "~cache_root",
                    "/home/amov/competition_development/image_cache",
                )
            )
        ).resolve()
        configured_mission_id = str(rospy.get_param("~mission_id", "")).strip()
        self.start_on_node_start = bool(
            rospy.get_param("~start_on_node_start", False)
        )

        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("~jpeg_quality must be between 1 and 100")
        if not 1 <= self.ground_port <= 65535:
            raise ValueError("~ground_port is invalid")
        if self.reconcile_retry_sec <= 0:
            raise ValueError("~reconcile_retry_sec must be greater than zero")
        if self.live_retry_interval <= 0:
            raise ValueError("~live_retry_interval_sec must be greater than zero")

        prefix = "/uav%d/image_transfer" % self.uav_id
        self.detection_topic = rospy.get_param("~detection_topic", prefix + "/target_detection")
        self.completed_target_topic = rospy.get_param(
            "~completed_target_topic", "/target_stcheduler/complemend_targets"
        )
        self.frame_cache = FrameCache(
            float(rospy.get_param("~frame_cache_sec", 2.0)),
            float(rospy.get_param("~camera_fps", 30.0)),
        )
        self.frame_lock = threading.Lock()
        self.pending_detections = []
        self.detection_jobs = queue.Queue(maxsize=queue_size)
        self.result_jobs = queue.Queue(maxsize=max(64, queue_size))
        self.max_pending_detections = queue_size
        self.image_buffer_bytes = int(rospy.get_param("~image_buffer_bytes", 67108864))
        if queue_size <= 0 or self.image_buffer_bytes <= 0:
            raise ValueError("发送队列长度和相机接收缓冲区必须大于零")
        self.target_image_topic = rospy.get_param(
            "~target_image_topic", prefix + "/target_image"
        )
        self.target_coordinate_topic = rospy.get_param(
            "~target_coordinate_topic", prefix + "/target_coordinate"
        )
        self.sync_queue_size = int(rospy.get_param("~sync_queue_size", 20))
        self.sync_slop_sec = float(rospy.get_param("~sync_slop_sec", 0.5))
        if self.sync_queue_size <= 0:
            raise ValueError("~sync_queue_size must be greater than zero")
        if self.sync_slop_sec < 0:
            raise ValueError("~sync_slop_sec cannot be negative")
        self.capture_service_name = rospy.get_param(
            "~capture_service", prefix + "/capture"
        )
        self.capture_topic = rospy.get_param(
            "~capture_topic", prefix + "/capture_request"
        )
        self.status_topic = rospy.get_param("~status_topic", prefix + "/status")
        self.mission_control_topic = rospy.get_param(
            "~mission_control_topic", prefix + "/mission_control"
        )

        self.bridge = CvBridge()
        if self.input_mode not in ("completed_target_array", "timestamped_detection", "paired_topics", "capture_request"):
            raise ValueError("输入模式无效")
        self.latest_lock = threading.Lock()
        self.latest_message = None
        self.latest_received_monotonic = 0.0
        self.jobs = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.connection = None
        self.connection_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.result_send_suppressed_until = 0.0
        self.live_send_suppressed_until = 0.0

        self.mission_lock = threading.Lock()
        self.active_mission_id = ""
        self.mission_started_monotonic = 0.0
        self.mission_started_at_unix_ns = 0
        self.sequence = 0
        self.reconcile_done = False
        self.reconcile_in_progress = False
        self.last_reconcile_attempt = 0.0
        self.recovery_revision = 0
        self.recovery_pending = False
        self.final_reconcile_done = False
        self.final_reconcile_trigger = ""
        self._competition_time = {}
        self._competition_time_received = 0.0
        self._successful_return = {}
        self._recovery_in_progress = False
        self._last_recovery_attempt = 0.0
        self._announce_pending = False

        self.status_publisher = rospy.Publisher(
            self.status_topic, String, queue_size=20
        )
        self.mission_subscriber = rospy.Subscriber(
            self.mission_control_topic,
            String,
            self._mission_control_callback,
            queue_size=10,
        )

        self.competition_time_subscriber = rospy.Subscriber(
            "/uav%d/competition/competition_time" % self.uav_id,
            String, self._competition_time_callback, queue_size=1)
        self.successful_return_subscriber = rospy.Subscriber(
            "/uav%d/competition/successful_return" % self.uav_id,
            String, self._successful_return_callback, queue_size=1)

        if configured_mission_id:
            self._start_mission(configured_mission_id)
        elif self.start_on_node_start:
            self._start_mission(self._new_mission_id())
        else:
            rospy.loginfo("图片回传节点已启动，等待一键起飞后开始图片任务计时")

        message_type = CompressedImage if self.compressed_input else Image
        self.image_subscriber = None
        self.coordinate_subscriber = None
        self.target_synchronizer = None
        self.capture_service = None
        self.command_subscriber = None
        self.detection_subscriber = None
        self.frame_timer = None
        if self.input_mode in ("completed_target_array", "timestamped_detection"):
            self.image_subscriber = rospy.Subscriber(
                self.image_topic, message_type, self._camera_cache_callback,
                queue_size=self.frame_cache.capacity, buff_size=self.image_buffer_bytes,
            )
            detection_type = (CompletedTargetArray if self.input_mode == "completed_target_array"
                              else TargetDetection)
            detection_topic = (self.completed_target_topic if self.input_mode == "completed_target_array"
                               else self.detection_topic)
            self.detection_subscriber = rospy.Subscriber(
                detection_topic, detection_type, self._detection_callback,
                queue_size=queue_size,
            )
            # 新版程序 B 在 UAV 命名空间发布；保留旧版及用户自定义话题。
            self.completed_target_subscribers = []
            if self.input_mode == "completed_target_array":
                aliases = ["/uav%d/target_scheduler/completed_targets" % self.local_ros_uav_id]
                for topic in aliases:
                    if rospy.resolve_name(topic) != rospy.resolve_name(detection_topic):
                        self.completed_target_subscribers.append(rospy.Subscriber(
                            topic, CompletedTargetArray, self._detection_callback, queue_size=queue_size))
                        rospy.loginfo("兼容新版程序 B 目标回传话题：%s", topic)
            self.detection_worker = threading.Thread(target=self._detection_worker_loop, daemon=True)
            self.detection_worker.start()
            self.frame_timer = rospy.Timer(rospy.Duration(0.05), self._frame_cache_timer)
        elif self.input_mode == "paired_topics":
            self.image_subscriber = message_filters.Subscriber(
                self.target_image_topic, message_type, queue_size=1
            )
            self.coordinate_subscriber = message_filters.Subscriber(
                self.target_coordinate_topic, NavSatFix, queue_size=20
            )
            self.target_synchronizer = message_filters.ApproximateTimeSynchronizer(
                [self.image_subscriber, self.coordinate_subscriber],
                queue_size=self.sync_queue_size,
                slop=self.sync_slop_sec,
                allow_headerless=False,
            )
            self.target_synchronizer.registerCallback(self._target_pair_callback)
        else:
            self.image_subscriber = rospy.Subscriber(
                self.image_topic, message_type, self._image_callback, queue_size=1
            )
            self.capture_service = rospy.Service(
                self.capture_service_name, Trigger, self._capture_service_callback
            )
            self.command_subscriber = rospy.Subscriber(
                self.capture_topic,
                String,
                self._capture_topic_callback,
                queue_size=20,
            )

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.result_worker = threading.Thread(target=self._result_worker_loop, daemon=True)
        self.result_worker.start()
        self.reconcile_timer = rospy.Timer(
            rospy.Duration(1.0), self._reconcile_timer_callback
        )
        rospy.on_shutdown(self.shutdown)

        rospy.loginfo(
            "图片回传节点就绪：模式=%s，相机=%s，算法结果=%s，"
            "任务话题=%s，地面=%s:%d，磁盘补传=%s，最终核对=成功返航或比赛用时 %.2f 分钟（仅一次）",
            self.input_mode,
            self.target_image_topic if self.input_mode == "paired_topics" else self.image_topic,
            self.completed_target_topic if self.input_mode == "completed_target_array" else
            (self.detection_topic if self.input_mode == "timestamped_detection" else "兼容接口"),
            self.mission_control_topic,
            self.ground_host,
            self.ground_port,
            self.enable_offline_recovery,
            self.reconcile_after_minutes,
        )

    def _reject_detection(self, metadata, state, reason):
        self._publish_status(state, dict(metadata, error=reason))
        rospy.logwarn("目标图片未回传：请求=%s，原因=%s", metadata.get("request_id", ""), reason)

    def _queue_detection(self, resolved, metadata):
        image = resolved[0] if resolved is not None else None
        if image is None:
            self._reject_detection(metadata, "frame_not_found", "原图缓存已过期")
            return
        _, _, policy = resolved
        metadata["selected_image_stamp"] = {"secs": int(image.header.stamp.secs),
                                            "nsecs": int(image.header.stamp.nsecs)}
        if policy != "exact":
            requested = dict(metadata["image_stamp"])
            metadata["requested_image_stamp"] = requested
            metadata["image_match_policy"] = "clamped_to_2s"
            rospy.logwarn("目标原图时间戳早于 2 秒缓存：请求=%s，原时间=%s，实际选帧=%s",
                          metadata.get("request_id", ""), requested, metadata["selected_image_stamp"])
        else:
            metadata["image_match_policy"] = "exact"
        frame_id = metadata["detection_frame_id"]
        if frame_id and frame_id != image.header.frame_id:
            self._reject_detection(metadata, "detection_rejected", "算法结果与原图的 frame_id 不一致")
            return
        try:
            # 保留已匹配原帧的引用；后续缓存淘汰不会改变本次回传的画面。
            self.detection_jobs.put_nowait((image, metadata))
        except queue.Full:
            self._reject_detection(metadata, "processing_queue_full", "绘框处理队列已满，本次请求未落盘")

    def _expire_pending_locked(self):
        now = time.monotonic()
        remaining = []
        for key, deadline, metadata in self.pending_detections:
            if now >= deadline:
                self._reject_detection(metadata, "frame_not_found", "缓存窗口内未找到指定时间戳的原图")
            else:
                remaining.append((key, deadline, metadata))
        self.pending_detections = remaining

    def _camera_cache_callback(self, message):
        try:
            with self.frame_lock:
                key = self.frame_cache.add(message)
                self._expire_pending_locked()
                remaining = []
                for pending_key, deadline, metadata in self.pending_detections:
                    resolved = self.frame_cache.resolve(pending_key)
                    if resolved is not None:
                        self._queue_detection(resolved, metadata)
                    else:
                        remaining.append((pending_key, deadline, metadata))
                self.pending_detections = remaining
        except (ValueError, TypeError) as exc:
            rospy.logwarn_throttle(5.0, "相机帧无法缓存：%s" % exc)

    def _detection_callback(self, message):
        if self.input_mode == "completed_target_array":
            for target in message.targets:
                try:
                    self._accept_detection_metadata(completed_target_metadata(target))
                except (ValueError, TypeError, OverflowError) as exc:
                    self._reject_detection({"request_id": str(target.global_id)}, "detection_rejected", str(exc))
            return
        try:
            key, metadata = detection_metadata(message)
        except (ValueError, TypeError, OverflowError) as exc:
            self._reject_detection({"request_id": message.request_id}, "detection_rejected", str(exc))
            return
        self._accept_detection_metadata((key, metadata))

    def _accept_detection_metadata(self, result):
        key, metadata = result
        # 固定图片任务，避免后台绘框期间切换任务导致图片被归到新任务。
        with self.mission_lock:
            mission_id = self.active_mission_id
            if mission_id:
                metadata["mission_id"] = mission_id
                metadata["mission_started_at_unix_ns"] = self.mission_started_at_unix_ns
                self.sequence += 1
                metadata["sequence"] = self.sequence
        if not mission_id:
            self._reject_detection(metadata, "mission_not_started", "尚未一键起飞，图片任务未开始")
            return
        metadata["detection_received_at_unix_ns"] = time.time_ns()
        metadata["uav_id"] = self.uav_id
        metadata["message_type"] = "target_result"
        metadata["file_name"] = "%s_UAV%02d_%06d_%s.jpg" % (
            mission_id, self.uav_id, metadata["sequence"],
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
        metadata["requested_at_unix_ns"] = metadata["detection_received_at_unix_ns"]
        try:
            self._cache_result_metadata(metadata)
        except (OSError, ValueError, TypeError) as exc:
            rospy.logerr("目标 JSON 本地缓存失败，仍尝试立即传送：%s", exc)
        try:
            self.result_jobs.put_nowait(dict(metadata))
        except queue.Full:
            rospy.logwarn("目标 JSON 即时发送队列已满，已留在机载缓存等待续传：%s",
                          metadata["file_name"])
        self._mark_reconciliation_needed(mission_id)
        with self.frame_lock:
            self._expire_pending_locked()
            resolved = self.frame_cache.resolve(key)
            if resolved is not None:
                self._queue_detection(resolved, metadata)
            elif len(self.pending_detections) < self.max_pending_detections:
                # ROS 不同话题回调可能乱序，短暂等待对应相机帧，不匹配邻近帧。
                self.pending_detections.append((key, time.monotonic() + self.frame_cache.duration_sec, metadata))
                self._publish_status("waiting_for_frame", metadata)
            else:
                self._reject_detection(metadata, "processing_queue_full", "等待原图的请求队列已满")

    def _frame_cache_timer(self, _event):
        with self.frame_lock:
            self.frame_cache.prune()
            self._expire_pending_locked()

    def _detection_worker_loop(self):
        while not self.stop_event.is_set() and not rospy.is_shutdown():
            try:
                image, metadata = self.detection_jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                accepted, _, reason = self._enqueue_image(
                    image, metadata["request_id"], detection=metadata,
                )
                if not accepted:
                    self._reject_detection(metadata, "detection_rejected", reason)
            except Exception as exc:
                self._reject_detection(metadata, "detection_rejected", str(exc))
            finally:
                self.detection_jobs.task_done()

    def _new_mission_id(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        return "subject1_%s_%s" % (
            now.strftime("%Y%m%dT%H%M%SZ"),
            uuid.uuid4().hex[:8],
        )

    def _mission_cache_dir(self, mission_id):
        return (self.cache_root / safe_component(mission_id, "mission")).resolve()

    def _start_mission(self, requested_id):
        mission_id = safe_component(requested_id, self._new_mission_id())[:48]
        # 起飞指令由执行器锁存，图片节点重连时会重放；同一任务不重置计时和序号。
        with self.mission_lock:
            if self.active_mission_id == mission_id:
                return
        mission_dir = self._mission_cache_dir(mission_id)
        if self.cache_root not in mission_dir.parents:
            raise ValueError("任务图片缓存路径不安全")
        if self.enable_offline_recovery:
            mission_dir.mkdir(parents=True, exist_ok=True)

        highest_sequence = 0
        if mission_dir.exists():
            for source_path in mission_dir.iterdir():
                if source_path.suffix not in (".jpg", ".json"):
                    continue
                match = re.search(r"_([0-9]{6})_[0-9]{8}T", source_path.name)
                if match:
                    highest_sequence = max(highest_sequence, int(match.group(1)))

        started_at_unix_ns = time.time_ns()
        with self.mission_lock:
            self.active_mission_id = mission_id
            self.mission_started_monotonic = time.monotonic()
            self.mission_started_at_unix_ns = started_at_unix_ns
            self.sequence = highest_sequence
            self.reconcile_done = False
            self.reconcile_in_progress = False
            self.last_reconcile_attempt = 0.0
            self.recovery_revision = 0
            self.recovery_pending = False
            self.final_reconcile_done = False
            self.final_reconcile_trigger = ""
            self._announce_pending = False
            # 同任务重启保持最终清单作业的触发/完成状态。
            try:
                saved = json.loads((mission_dir / ".final_reconciliation.json").read_text(encoding="utf-8"))
                if saved.get("mission_id") == mission_id:
                    self.final_reconcile_trigger = str(saved.get("trigger") or "")
                    self.final_reconcile_done = saved.get("done") is True
            except (OSError, ValueError):
                pass
            self.recovery_pending = any(not path.with_suffix(".acked").exists()
                                        for path in mission_dir.glob("*.jpg")) or any(
                                            not path.name.startswith(".")
                                            and not path.with_suffix(".jsonacked").exists()
                                            for path in mission_dir.glob("*.json"))

        mission_metadata = {
            "mission_id": mission_id,
            "mission_started_at_unix_ns": started_at_unix_ns,
        }
        self._publish_status("mission_started", mission_metadata)
        rospy.loginfo("图片任务已开始：%s", mission_id)
        threading.Thread(
            target=self._announce_mission,
            args=(mission_id, started_at_unix_ns),
            daemon=True,
        ).start()

    def _announce_mission(self, mission_id, started_at_unix_ns):
        metadata = {
            "message_type": "mission_start",
            "mission_id": mission_id,
            "uav_id": self.uav_id,
            "mission_started_at_unix_ns": started_at_unix_ns,
        }
        if self.auth_token:
            metadata["auth_token"] = self.auth_token
        try:
            self._exchange_control(metadata)
            with self.mission_lock:
                if mission_id == self.active_mission_id:
                    self._announce_pending = False
            self._publish_status("ground_mission_started", metadata)
        except Exception as exc:
            metadata["error"] = str(exc)
            # 起飞通知单独重试，不提前触发最终缺失清单。
            with self.mission_lock:
                if mission_id == self.active_mission_id:
                    self._announce_pending = True
            self._mark_reconciliation_needed(mission_id)
            self._publish_status("mission_announce_pending", metadata)
            rospy.logwarn("地面任务计时通知失败：%s", exc)

    def _mission_control_callback(self, message: String) -> None:
        command = message.data.strip()
        if command == "start":
            self._start_mission(self._new_mission_id())
        elif command.startswith("start:"):
            self._start_mission(command.split(":", 1)[1])
        elif command == "sync":
            if self.final_reconcile_trigger and not self.final_reconcile_done:
                self._schedule_reconciliation(force=True, final=True)
            else:
                rospy.loginfo("尚无待完成的最终补传作业，忽略重复补传请求")
        else:
            rospy.logwarn(
                "未知任务控制指令“%s”；请使用 start、start:<任务编号> 或 sync",
                command,
            )

    def _image_callback(self, message) -> None:
        with self.latest_lock:
            self.latest_message = message
            self.latest_received_monotonic = time.monotonic()

    def _target_pair_callback(self, image_message, coordinate_message) -> None:
        latitude = float(coordinate_message.latitude)
        longitude = float(coordinate_message.longitude)
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90.0 <= latitude <= 90.0
            or not -180.0 <= longitude <= 180.0
        ):
            rospy.logwarn(
                "目标图片与坐标配对被拒绝：经纬度无效，东经=%r，北纬=%r",
                longitude,
                latitude,
            )
            return

        stamp = image_message.header.stamp
        request_id = "target-%d-%09d" % (int(stamp.secs), int(stamp.nsecs))
        accepted, request_id, reason = self._enqueue_image(
            image_message,
            request_id,
            latitude=latitude,
            longitude=longitude,
            coordinate_message=coordinate_message,
        )
        if accepted:
            rospy.loginfo(
                "目标图片已加入队列：请求=%s，东经=%.7f，北纬=%.7f",
                request_id,
                longitude,
                latitude,
            )
        else:
            rospy.logwarn("目标图片与坐标配对被拒绝：%s", reason)

    def _capture_service_callback(self, _request) -> TriggerResponse:
        accepted, request_id, reason = self._enqueue_capture("")
        message = request_id if accepted else reason
        return TriggerResponse(success=accepted, message=message)

    def _capture_topic_callback(self, message: String) -> None:
        accepted, request_id, reason = self._enqueue_capture(message.data.strip())
        if not accepted:
            rospy.logwarn("拍照请求被拒绝：%s", reason)
        else:
            rospy.loginfo("拍照请求已加入队列：%s", request_id)

    def _enqueue_capture(self, requested_id):
        with self.latest_lock:
            image_message = self.latest_message
            received_at = self.latest_received_monotonic

        if image_message is None:
            return False, "", "相机尚未发布图像"
        age = time.monotonic() - received_at
        if self.max_frame_age > 0 and age > self.max_frame_age:
            return False, "", "最新相机帧已过期 %.2f 秒" % age

        return self._enqueue_image(image_message, requested_id)

    def _enqueue_image(
        self,
        image_message,
        requested_id,
        latitude=None,
        longitude=None,
        coordinate_message=None,
        detection=None,
    ):

        if detection is not None:
            sequence = detection["sequence"]
            mission_id = detection["mission_id"]
            mission_started_at_unix_ns = detection["mission_started_at_unix_ns"]
        else:
            with self.mission_lock:
                self.sequence += 1
                sequence = self.sequence
                mission_id = self.active_mission_id
                mission_started_at_unix_ns = self.mission_started_at_unix_ns

        request_id = requested_id or uuid.uuid4().hex
        timestamp_ns = int(detection.get("requested_at_unix_ns", time.time_ns())) if detection is not None else time.time_ns()
        utc_stamp = datetime.datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000, datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%S.%fZ")
        file_name = detection.get("file_name") if detection is not None else None
        file_name = file_name or "%s_UAV%02d_%06d_%s.jpg" % (
            mission_id,
            self.uav_id,
            sequence,
            utc_stamp,
        )
        metadata = {
            "message_type": "image",
            "protocol_version": 1,
            "request_id": request_id,
            "mission_id": mission_id,
            "mission_started_at_unix_ns": mission_started_at_unix_ns,
            "sequence": sequence,
            "file_name": file_name,
            "uav_id": self.uav_id,
            "image_topic": (
                self.target_image_topic
                if self.input_mode == "paired_topics"
                else self.image_topic
            ),
            "requested_at_unix_ns": timestamp_ns,
            "source_stamp_sec": int(image_message.header.stamp.secs),
            "source_stamp_nsec": int(image_message.header.stamp.nsecs),
            "mime_type": "image/jpeg",
            "camera_frame_id": image_message.header.frame_id,
        }
        if detection is not None:
            metadata.update(detection)
            metadata["message_type"] = "image"
            metadata["detection_topic"] = self.detection_topic
        if latitude is not None and longitude is not None:
            metadata["target_latitude"] = latitude
            metadata["target_longitude"] = longitude
            metadata["coordinate_topic"] = self.target_coordinate_topic
            metadata["coordinate_stamp_sec"] = int(
                coordinate_message.header.stamp.secs
            )
            metadata["coordinate_stamp_nsec"] = int(
                coordinate_message.header.stamp.nsecs
            )
        if self.auth_token:
            metadata["auth_token"] = self.auth_token

        try:
            jpeg, width, height = self._message_to_jpeg(
                image_message, detection=metadata if detection is not None else None
            )
            metadata["width"] = width
            metadata["height"] = height
            metadata["jpeg_size"] = len(jpeg)
            if self.enable_offline_recovery:
                image_path = self._cache_image(metadata, jpeg)
                metadata["cache_path"] = str(image_path)
                self._publish_status("cached", metadata)
                rospy.loginfo("图片已在传输前缓存：%s", image_path)
        except Exception as exc:
            return False, "", "当前图片无法写入磁盘：%s" % exc

        try:
            self.jobs.put_nowait((jpeg, metadata))
            self._publish_status("queued", metadata)
        except queue.Full:
            if not self.enable_offline_recovery:
                return False, "", "拍照处理队列已满"
            metadata["error"] = "实时传输队列已满，图片已安全保存在磁盘"
            self._mark_reconciliation_needed(metadata["mission_id"])
            self._publish_status("pending_recovery", metadata)
        return True, request_id, ""

    def _message_to_jpeg(self, message, detection=None):
        try:
            if self.compressed_input:
                encoded = np.frombuffer(message.data, dtype=np.uint8)
                image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("OpenCV could not decode compressed image")
            else:
                image = self.bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        except (CvBridgeError, ValueError) as exc:
            raise RuntimeError("camera image conversion failed: %s" % exc) from exc

        if detection is not None:
            # cv_bridge 可能返回原 ROS 数据的视图，必须复制后绘制，避免污染缓存。
            image = image.copy()
            x1, y1, x2, y2 = clipped_rectangle(detection["bbox"], image.shape[1], image.shape[0])
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            detection["rendered_bbox"] = {"x_min": x1, "y_min": y1, "x_max": x2, "y_max": y2}

        ok, encoded_jpeg = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not ok:
            raise RuntimeError("OpenCV JPEG encoding failed")
        return encoded_jpeg.tobytes(), int(image.shape[1]), int(image.shape[0])

    def _cache_image(self, metadata, jpeg):
        mission_dir = self._mission_cache_dir(metadata["mission_id"])
        mission_dir.mkdir(parents=True, exist_ok=True)
        image_path = mission_dir / metadata["file_name"]
        metadata_path = image_path.with_suffix(".json")
        image_tmp = image_path.with_suffix(".jpg.part")
        metadata_tmp = metadata_path.with_suffix(".json.part")

        with image_tmp.open("wb") as stream:
            stream.write(jpeg)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(image_tmp), str(image_path))

        public_metadata = dict(metadata)
        public_metadata.pop("auth_token", None)
        with metadata_tmp.open("w", encoding="utf-8") as stream:
            json.dump(public_metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(metadata_tmp), str(metadata_path))
        return image_path

    def _cache_result_metadata(self, metadata):
        mission_dir = self._mission_cache_dir(metadata["mission_id"])
        if self.cache_root not in mission_dir.parents:
            raise ValueError("目标 JSON 缓存路径不安全")
        mission_dir.mkdir(parents=True, exist_ok=True)
        path = mission_dir / Path(metadata["file_name"]).with_suffix(".json").name
        temporary = path.with_suffix(".json.part")
        public = dict(metadata)
        public.pop("auth_token", None)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(public, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        return path

    def _send_result_with_ack(self, metadata):
        result = dict(metadata, message_type="target_result")
        result.pop("cache_path", None)
        if self.auth_token:
            result["auth_token"] = self.auth_token
        packet = encode_frame(result, b"\x00")
        with self.result_lock, socket.create_connection(
            (self.ground_host, self.ground_port), timeout=self.connect_timeout
        ) as connection:
            connection.settimeout(self.ack_timeout)
            connection.sendall(packet)
            if not receive_ack(connection):
                raise RuntimeError("地面端拒绝目标 JSON")

    def _mark_result_acked(self, metadata):
        path = self._mission_cache_dir(metadata["mission_id"]) / metadata["file_name"]
        try:
            path.with_suffix(".jsonacked").touch()
        except OSError as error:
            rospy.logwarn("目标 JSON 发送确认标记保存失败：%s", error)

    def _result_worker_loop(self):
        while not self.stop_event.is_set() and not rospy.is_shutdown():
            try:
                metadata = self.result_jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                marker = (self._mission_cache_dir(metadata["mission_id"]) /
                          metadata["file_name"]).with_suffix(".jsonacked")
                if marker.exists() or time.monotonic() < self.result_send_suppressed_until:
                    continue
                self._send_result_with_ack(metadata)
                self._mark_result_acked(metadata)
                self.result_send_suppressed_until = 0.0
                self._publish_status("result_sent", metadata)
                rospy.loginfo("目标 JSON 已独立送达地面：%s", metadata["file_name"])
            except Exception as exc:
                self.result_send_suppressed_until = time.monotonic() + self.live_retry_interval
                self._mark_reconciliation_needed(metadata["mission_id"])
                rospy.logwarn("目标 JSON 暂未送达，等待独立续传：%s，原因=%s",
                              metadata["file_name"], exc)
            finally:
                self.result_jobs.task_done()

    def _connect_locked(self):
        self._close_connection_locked()
        connection = socket.create_connection(
            (self.ground_host, self.ground_port), timeout=self.connect_timeout
        )
        connection.settimeout(self.ack_timeout)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.connection = connection
        return connection

    def _close_connection_locked(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _send_packet_with_ack(self, packet):
        with self.connection_lock:
            connection = self.connection or self._connect_locked()
            try:
                connection.sendall(packet)
                if not receive_ack(connection):
                    raise RuntimeError("地面图片接收器拒绝了该图片")
            except Exception:
                self._close_connection_locked()
                raise

    def _exchange_control(self, metadata):
        packet = encode_frame(metadata, b"\x00")
        with self.connection_lock:
            connection = self.connection or self._connect_locked()
            try:
                connection.sendall(packet)
                success, response = receive_response(connection)
                if not success:
                    raise RuntimeError(response.get("error", "control message rejected"))
                return response
            except Exception:
                self._close_connection_locked()
                raise

    def _send_with_retries(self, packet, description):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._send_packet_with_ack(packet)
                return True, attempt, None
            except Exception as exc:
                last_error = exc
                rospy.logwarn(
                    "%s 第 %d/%d 次尝试失败：%s",
                    description,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries:
                    self.stop_event.wait(self.retry_delay)
        return False, self.max_retries, last_error

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set() and not rospy.is_shutdown():
            try:
                jpeg, metadata = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                if time.monotonic() < self.live_send_suppressed_until:
                    metadata["error"] = "传输失败后已暂时暂停网络发送"
                    self._mark_reconciliation_needed(metadata["mission_id"])
                    self._publish_status("pending_recovery", metadata)
                    continue

                packet = encode_frame(metadata, jpeg)
                try:
                    self._send_packet_with_ack(packet)
                    self.live_send_suppressed_until = 0.0
                    self._mark_image_acked(metadata)
                    self._publish_status("sent", metadata)
                    rospy.loginfo(
                        "图片已发送：文件=%s，字节=%d",
                        metadata["file_name"],
                        len(jpeg),
                    )
                except Exception as exc:
                    self.live_send_suppressed_until = (
                        time.monotonic() + self.live_retry_interval
                    )
                    metadata["error"] = str(exc)
                    self._mark_reconciliation_needed(metadata["mission_id"])
                    self._publish_status("pending_recovery", metadata)
                    rospy.logerr(
                        "图片仍保留在机载磁盘，等待补传核对：%s",
                        metadata["file_name"],
                    )
            except Exception as exc:
                metadata["error"] = str(exc)
                self._publish_status("failed", metadata)
                rospy.logerr("拍照处理失败：%s", exc)
            finally:
                self.jobs.task_done()

    def _competition_time_callback(self, message):
        try:
            state = json.loads(message.data)
            if not isinstance(state, dict):
                return
            with self.mission_lock:
                self._competition_time = state
                self._competition_time_received = time.monotonic()
        except (TypeError, ValueError):
            rospy.logwarn("忽略无效比赛计时消息")

    def _successful_return_callback(self, message):
        try:
            event = json.loads(message.data)
            with self.mission_lock:
                self._successful_return = event if isinstance(event, dict) else {}
            self._reconcile_timer_callback(None)
        except (TypeError, ValueError):
            rospy.logwarn("忽略无效成功返航消息")

    def _save_final_reconciliation_locked(self):
        path = self._mission_cache_dir(self.active_mission_id) / ".final_reconciliation.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(dict(mission_id=self.active_mission_id,
                trigger=self.final_reconcile_trigger, done=self.final_reconcile_done),
                ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError as error:
            rospy.logwarn("最终补传进度保存失败：%s", error)

    def _reconcile_timer_callback(self, _event):
        if not self.enable_offline_recovery:
            return
        now = time.monotonic()
        with self.mission_lock:
            mission = self.active_mission_id
            if not mission:
                return
            state = self._competition_time
            elapsed = competition_elapsed(state, self._competition_time_received, now, mission)
            reason = ""
            if matches_return_event(self._successful_return, mission, self.uav_id):
                reason = "successful_return"
            elif elapsed is not None and elapsed >= self.reconcile_after_minutes * 60.0:
                reason = "competition_23m30s"
            if reason and not self.final_reconcile_trigger:
                self.final_reconcile_trigger = reason
                # 最终作业首次触发立即运行，不受断连重试的节拍延迟。
                self.last_reconcile_attempt = -float("inf")
                self._save_final_reconciliation_locked()
                rospy.loginfo("最终缺失清单补传已触发：UAV%d，任务=%s，原因=%s",
                              self.uav_id, mission, reason)
            final_due = bool(self.final_reconcile_trigger) and not self.final_reconcile_done
        if final_due:
            # 同一作业失败后重试；第二个触发条件不会再创建作业。
            self._schedule_reconciliation(force=False, final=True)
        else:
            # 网络恢复只重试没有 ACK 的原始文件，不额外请求缺失清单。
            self._schedule_pending_delivery()

    def _mark_image_acked(self, metadata):
        if self.enable_offline_recovery:
            path = self._mission_cache_dir(metadata["mission_id"]) / metadata["file_name"]
            try:
                path.with_suffix(".acked").touch()
                if path.with_suffix(".json").exists():
                    self._mark_result_acked(metadata)
            except OSError as error:
                rospy.logwarn("图片发送确认标记保存失败：%s", error)

    def _schedule_pending_delivery(self):
        with self.mission_lock:
            if (self._recovery_in_progress or self.reconcile_in_progress
                    or not (self.recovery_pending or self._announce_pending)
                    or time.monotonic() - self._last_recovery_attempt < self.live_retry_interval):
                return
            self._recovery_in_progress = True
            self._last_recovery_attempt = time.monotonic()
            mission, revision = self.active_mission_id, self.recovery_revision
        threading.Thread(target=self._retry_pending_delivery, args=(mission, revision), daemon=True).start()

    def _retry_pending_delivery(self, mission, revision):
        success = False
        try:
            with self.mission_lock:
                started = self.mission_started_at_unix_ns
                announce = self._announce_pending
            if announce:
                self._announce_mission(mission, started)
            self._retry_pending_results(mission)
            for path in sorted(self._mission_cache_dir(mission).glob("*.jpg")):
                with self.mission_lock:
                    if (mission != self.active_mission_id or self.stop_event.is_set()
                            or (self.final_reconcile_trigger and not self.final_reconcile_done)):
                        return
                if path.with_suffix(".acked").exists() or not path.with_suffix(".json").exists():
                    continue
                metadata = self._load_cached_metadata(path)
                jpeg = path.read_bytes()
                metadata["jpeg_size"] = len(jpeg)
                self._send_packet_with_ack(encode_frame(metadata, jpeg))
                self._mark_image_acked(metadata)
                self._publish_status("sent", metadata)
                rospy.loginfo("断连后未确认图片已续传：任务=%s，文件=%s", mission, path.name)
            success = True
        except Exception as error:
            rospy.logwarn("未确认图片续传暂未完成，稍后继续：%s", error)
        finally:
            with self.mission_lock:
                if mission == self.active_mission_id and success and revision == self.recovery_revision:
                    self.recovery_pending = False
                self._recovery_in_progress = False

    def _retry_pending_results(self, mission):
        for path in sorted(self._mission_cache_dir(mission).glob("*.json")):
            if path.with_suffix(".jsonacked").exists():
                continue
            with self.mission_lock:
                if mission != self.active_mission_id or self.stop_event.is_set():
                    return
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or not metadata.get("target_type"):
                continue
            self._send_result_with_ack(metadata)
            self._mark_result_acked(metadata)
            rospy.loginfo("目标 JSON 已补传：任务=%s，文件=%s", mission, path.name)

    def _schedule_reconciliation(self, force, final=False):
        now = time.monotonic()
        with self.mission_lock:
            if not self.active_mission_id:
                return
            if self.reconcile_in_progress or getattr(self, "_recovery_in_progress", False):
                return
            if final and self.final_reconcile_done:
                return
            if self.reconcile_done and not force and not final:
                return
            retry_interval = min(2.0, self.reconcile_retry_sec) if final else self.reconcile_retry_sec
            if not force and now - self.last_reconcile_attempt < retry_interval:
                return
            mission_id = self.active_mission_id
            if force:
                self.reconcile_done = False
            self.reconcile_in_progress = True
            self.last_reconcile_attempt = now
            recovery_revision = self.recovery_revision
            reconcile_trigger = (
                self.final_reconcile_trigger
                if final
                else ("manual" if force else "automatic_recovery")
            )

        thread = threading.Thread(
            target=self._reconcile_worker,
            args=(mission_id, recovery_revision, final, reconcile_trigger),
            daemon=True,
        )
        thread.start()

    def _mark_reconciliation_needed(self, mission_id):
        with self.mission_lock:
            if self.active_mission_id == mission_id:
                self.reconcile_done = False
                self.recovery_revision += 1
                self.recovery_pending = True

    def _load_cached_metadata(self, image_path):
        metadata_path = image_path.with_suffix(".json")
        if metadata_path.exists():
            with metadata_path.open("r", encoding="utf-8") as stream:
                metadata = json.load(stream)
        else:
            metadata = {
                "message_type": "image",
                "request_id": image_path.stem,
                "mission_id": image_path.parent.name,
                "file_name": image_path.name,
                "uav_id": self.uav_id,
                "mime_type": "image/jpeg",
            }
        metadata["message_type"] = "image"
        metadata["file_name"] = image_path.name
        metadata["retransmission"] = True
        if self.auth_token:
            metadata["auth_token"] = self.auth_token
        return metadata

    def _reconcile_once(self, mission_id):
        mission_dir = self._mission_cache_dir(mission_id)
        self._retry_pending_results(mission_id)
        image_paths = sorted(path for path in mission_dir.glob("*.jpg") if path.is_file())
        file_names = [path.name for path in image_paths]
        with self.mission_lock:
            started_at_unix_ns = (
                self.mission_started_at_unix_ns
                if self.active_mission_id == mission_id
                else 0
            )
        manifest = {
            "message_type": "manifest",
            "mission_id": mission_id,
            "uav_id": self.uav_id,
            "mission_started_at_unix_ns": started_at_unix_ns,
            "file_names": file_names,
            "created_at_unix_ns": time.time_ns(),
        }
        if self.auth_token:
            manifest["auth_token"] = self.auth_token

        response = self._exchange_control(manifest)
        missing = response.get("missing", [])
        if not isinstance(missing, list):
            raise RuntimeError("地面图片清单响应中的缺失列表无效")
        path_by_name = {path.name: path for path in image_paths}
        missing = [name for name in missing if name in path_by_name]
        rospy.loginfo(
            "图片清单已核对：任务=%s，机载=%d，地面已有=%d，缺失=%d",
            mission_id,
            len(file_names),
            int(response.get("present", 0)),
            len(missing),
        )

        for file_name in missing:
            image_path = path_by_name[file_name]
            jpeg = image_path.read_bytes()
            metadata = self._load_cached_metadata(image_path)
            metadata["jpeg_size"] = len(jpeg)
            success, _, error = self._send_with_retries(
                encode_frame(metadata, jpeg), "补传图片 %s" % file_name
            )
            if not success:
                raise RuntimeError("无法补传 %s：%s" % (file_name, error))

        verification = self._exchange_control(manifest)
        remaining = verification.get("missing", [])
        if remaining:
            raise RuntimeError("地面端仍缺少 %d 张图片" % len(remaining))
        for path in image_paths:
            self._mark_image_acked({"mission_id": mission_id, "file_name": path.name})
        return len(file_names), len(missing)

    def _reconcile_worker(
        self, mission_id, recovery_revision, final, reconcile_trigger
    ):
        success = False
        metadata = {
            "mission_id": mission_id,
            "reconcile_trigger": reconcile_trigger,
        }
        try:
            if (getattr(getattr(self, "detection_jobs", None), "unfinished_tasks", 0)
                    or getattr(self, "pending_detections", [])):
                raise RuntimeError("仍有目标原图正在匹配或落盘，稍后完成本次清单核对")
            total, recovered = self._reconcile_once(mission_id)
            metadata.update({"total": total, "recovered": recovered})
            self._publish_status("reconcile_complete", metadata)
            rospy.loginfo(
                "图片补传核对完成：任务=%s，触发原因=%s，总数=%d，补传=%d",
                mission_id,
                reconcile_trigger,
                total,
                recovered,
            )
            success = True
        except Exception as exc:
            metadata["error"] = str(exc)
            self._publish_status("reconcile_retry_pending", metadata)
            rospy.logwarn(
                "图片补传核对失败：触发原因=%s；将在 %.1f 秒后重试：%s",
                reconcile_trigger,
                min(2.0, self.reconcile_retry_sec) if final else self.reconcile_retry_sec,
                exc,
            )
        finally:
            with self.mission_lock:
                if self.active_mission_id == mission_id:
                    current_revision = self.recovery_revision == recovery_revision
                    completed_current_state = success and current_revision
                    self.reconcile_done = completed_current_state
                    if completed_current_state:
                        self.recovery_pending = False
                        if final:
                            self.final_reconcile_done = True
                            self._save_final_reconciliation_locked()
                    self.reconcile_in_progress = False

    def _publish_status(self, state, metadata) -> None:
        status = {
            "state": state,
            "request_id": metadata.get("request_id", ""),
            "mission_id": metadata.get("mission_id", self.active_mission_id),
            "file_name": metadata.get("file_name", ""),
            "uav_id": self.uav_id,
            "timestamp_unix_ns": time.time_ns(),
        }
        for key in (
            "jpeg_size",
            "cache_path",
            "total",
            "recovered",
            "error",
            "mission_started_at_unix_ns",
            "reconcile_trigger",
            "target_latitude",
            "target_longitude",
            "target_altitude",
            "image_stamp",
            "target_id",
            "target_type",
            "confidence",
            "bbox",
        ):
            if key in metadata:
                status[key] = metadata[key]
        self.status_publisher.publish(
            String(data=json.dumps(status, ensure_ascii=False, separators=(",", ":")))
        )

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.connection_lock:
            self._close_connection_locked()


def main() -> None:
    rospy.init_node("su17_onboard_image_sender")
    OnboardImageSender()
    rospy.spin()


if __name__ == "__main__":
    main()
