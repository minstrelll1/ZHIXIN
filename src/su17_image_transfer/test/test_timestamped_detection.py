"""缓存和回传集成测试；ROS 边界用桩替代，OpenCV、TCP、磁盘使用真实实现。"""

import importlib.util
import json
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
from su17_image_transfer.frame_cache import (FrameCache, detection_metadata,
                                             completed_target_metadata, clipped_rectangle)


def frame(nsecs=123456789, value=0):
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(secs=1_789_000_000, nsecs=nsecs), frame_id="camera"),
        data=np.full((80, 120, 3), value, dtype=np.uint8),
    )


def detection(image=None, **values):
    result = dict(
        header=(image or frame()).header, request_id="request-1", target_id="track-7",
        center_x=50.0, center_y=40.0, width=20.0, height=20.0,
        target_longitude=113.707, target_latitude=33.864, target_altitude=16.0,
        confidence=0.93, target_type="车辆", extra_json='{"型号":"测试","uav_id":6}',
    )
    result.update(values)
    return SimpleNamespace(**result)


class FrameCacheTest(unittest.TestCase):
    def test_30fps_keeps_60_frames_and_exact_nanoseconds(self):
        now = [0.0]
        cache = FrameCache(clock=lambda: now[0])
        keys = []
        for i in range(75):
            now[0] = i / 30.0
            keys.append(cache.add(frame(i + 1)))
        self.assertEqual(60, len(cache.frames))
        self.assertIsNone(cache.get(keys[14]))
        self.assertEqual(16, cache.get(keys[15]).header.stamp.nsecs)
        self.assertEqual(75, cache.get(keys[74]).header.stamp.nsecs)
        self.assertIsNone(cache.get(keys[74] + 1))

    def test_expiry_when_camera_stops_and_duplicate_does_not_refresh(self):
        now = [0.0]
        cache = FrameCache(duration_sec=0.5, clock=lambda: now[0])
        original = frame()
        key = cache.add(original)
        now[0] = 0.4
        cache.add(frame(value=255))
        self.assertIs(original, cache.get(key))
        now[0] = 0.5
        cache.prune()
        self.assertEqual(0, len(cache.frames))

    def test_out_of_order_image_stamps_are_indexed_exactly(self):
        cache = FrameCache()
        later = cache.add(frame(200))
        earlier = cache.add(frame(100))
        self.assertEqual(200, cache.get(later).header.stamp.nsecs)
        self.assertEqual(100, cache.get(earlier).header.stamp.nsecs)

    def test_older_than_two_seconds_uses_frame_nearest_window_start(self):
        cache = FrameCache()
        old = frame(100)
        old.header.stamp.secs = 1_789_000_000
        first = frame(400)
        first.header.stamp.secs = 1_789_000_001
        newest = frame(300)
        newest.header.stamp.secs = 1_789_000_003
        for image in (old, first, newest):
            cache.add(image)
        selected, key, policy = cache.resolve(1_789_000_000_000000099)
        self.assertIs(selected, first)
        self.assertEqual(key, 1_789_000_001_000000400)
        self.assertEqual(policy, "clamped_to_cache_window")
        self.assertIsNone(cache.resolve(1_789_000_002_000000001))
        self.assertEqual(cache.resolve(1_789_000_003_000000300)[2], "exact")

    def test_metadata_preserves_extra_without_overwriting_identity(self):
        key, metadata = detection_metadata(detection())
        self.assertEqual(1_789_000_000_123456789, key)
        self.assertEqual(123456789, metadata["image_stamp"]["nsecs"])
        self.assertEqual({"型号": "测试", "uav_id": 6}, metadata["extra"])
        self.assertNotIn("uav_id", metadata)

    def test_invalid_detection_is_rejected(self):
        for values in (
            {"width": 0}, {"height": -1}, {"center_x": float("nan")},
            {"confidence": 1.1}, {"target_latitude": 91}, {"target_longitude": -181},
            {"target_altitude": float("inf")}, {"target_type": ""},
            {"extra_json": "[]"}, {"extra_json": '{"x":NaN}'},
            {"extra_json": '{"x":1e999}'}, {"extra_json": "x" * 65537},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                detection_metadata(detection(**values))

    def test_bbox_clips_to_image_and_rejects_no_overlap(self):
        box = {"center_x": 2, "center_y": 2, "width": 20, "height": 20}
        self.assertEqual((0, 0, 11, 11), clipped_rectangle(box, 120, 80))
        box["center_x"] = -100
        with self.assertRaises(ValueError):
            clipped_rectangle(box, 120, 80)


def load_sender():
    fake_ros = ModuleType("rospy")
    fake_ros.params = {}
    fake_ros.get_param = lambda key, default=None: fake_ros.params.get(key, default)
    fake_ros.is_shutdown = lambda: False
    fake_ros.Duration = lambda seconds: seconds
    fake_ros.Timer = lambda *args, **kwargs: SimpleNamespace(shutdown=lambda: None)
    fake_ros.on_shutdown = lambda callback: None
    fake_ros.Subscriber = lambda topic, kind, callback, **kwargs: SimpleNamespace(
        topic=topic, callback=callback, kind=kind, options=kwargs)
    fake_ros.Publisher = lambda *args, **kwargs: SimpleNamespace(publish=lambda message: None)
    for name in ("loginfo", "logwarn", "logerr", "logwarn_throttle"):
        setattr(fake_ros, name, lambda *args, **kwargs: None)
    fake_bridge = ModuleType("cv_bridge")
    fake_bridge.CvBridge = lambda: SimpleNamespace(imgmsg_to_cv2=lambda message, **kwargs: message.data)
    fake_bridge.CvBridgeError = type("CvBridgeError", (Exception,), {})
    modules = {
        "rospy": fake_ros, "cv_bridge": fake_bridge, "message_filters": ModuleType("message_filters"),
        "sensor_msgs.msg": SimpleNamespace(CompressedImage=type("CompressedImage", (), {}), Image=type("Image", (), {}), NavSatFix=type("NavSatFix", (), {})),
        "std_msgs.msg": SimpleNamespace(String=lambda **kwargs: SimpleNamespace(**kwargs)),
        "std_srvs.srv": SimpleNamespace(Trigger=object, TriggerResponse=lambda **kwargs: SimpleNamespace(**kwargs)),
        "su17_image_transfer.msg": SimpleNamespace(
            TargetDetection=type("TargetDetection", (), {}),
            CompletedTargetArray=type("CompletedTargetArray", (), {}),
        ),
    }
    spec = importlib.util.spec_from_file_location("timestamp_sender_under_test", PACKAGE_ROOT / "scripts" / "onboard_image_sender.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


sender_module = load_sender()


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        sender_module.rospy.params = {
            "~uav_id": 3, "~cache_root": self.directory.name, "~auth_token": "test-secret",
            "~input_mode": "timestamped_detection",
        }
        # 不启动 ROS 定时器或无人机网络连接，测试显式驱动各回调。
        with patch.object(threading.Thread, "start"):
            self.sender = sender_module.OnboardImageSender()
        self.sender._start_mission("test-task")
        self.statuses = []
        self.sender.status_publisher.publish = lambda msg: self.statuses.append(json.loads(msg.data))
        self.addCleanup(self.sender.shutdown)

    def process_next(self):
        image, metadata = self.sender.detection_jobs.get_nowait()
        self.sender.detection_jobs.task_done()
        result = self.sender._enqueue_image(image, metadata["request_id"], detection=metadata)
        self.assertTrue(result[0], result)
        return self.sender.jobs.get_nowait()

    def test_default_subscriptions_and_full_frame_box_only(self):
        self.assertEqual("/uav1/gimbal/image_original", self.sender.image_subscriber.topic)
        self.assertEqual("/uav3/image_transfer/target_detection", self.sender.detection_subscriber.topic)
        self.assertEqual(60, self.sender.image_subscriber.options["queue_size"])
        image = frame()
        self.sender._camera_cache_callback(image)
        self.sender._detection_callback(detection(image))
        jpeg, metadata = self.process_next()
        returned = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.data.shape, returned.shape)
        self.assertGreater(int(returned[30, 50, 1]), 100)
        self.assertEqual(0, np.count_nonzero(image.data))
        self.assertEqual(3, metadata["uav_id"])
        self.assertEqual("车辆", metadata["target_type"])
        self.assertEqual(0.93, metadata["confidence"])
        self.assertEqual({"x_min": 40, "y_min": 30, "x_max": 59, "y_max": 49}, metadata["rendered_bbox"])
        saved = json.loads(Path(metadata["cache_path"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertNotIn("auth_token", saved)
        self.assertEqual(metadata["extra"], saved["extra"])

    def test_detection_before_frame_and_two_boxes_do_not_pollute_cache(self):
        image = frame()
        self.sender._detection_callback(detection(image))
        self.assertEqual("waiting_for_frame", self.statuses[-1]["state"])
        self.sender._camera_cache_callback(image)
        first, _ = self.process_next()
        self.sender._detection_callback(detection(image, request_id="request-2", center_x=90))
        second, _ = self.process_next()
        decoded = cv2.imdecode(np.frombuffer(second, np.uint8), cv2.IMREAD_COLOR)
        self.assertLess(int(decoded[30, 50].max()), 20)
        self.assertGreater(int(decoded[30, 90, 1]), 100)
        self.assertNotEqual(first, second)
        self.assertEqual(0, np.count_nonzero(image.data))

    def test_near_timestamp_never_substituted_and_pending_expires(self):
        self.sender._camera_cache_callback(frame(100))
        self.sender._detection_callback(detection(frame(101)))
        self.assertTrue(self.sender.detection_jobs.empty())
        key, _, metadata = self.sender.pending_detections[0]
        self.sender.pending_detections[0] = (key, time.monotonic() - 1, metadata)
        self.sender._frame_cache_timer(None)
        self.assertEqual("frame_not_found", self.statuses[-1]["state"])
        self.assertTrue(self.sender.jobs.empty())

    def test_old_program_b_timestamp_returns_two_second_frame_and_keeps_requested_stamp(self):
        two_seconds_old = frame(100, value=20)
        two_seconds_old.header.stamp.secs = 1_789_000_001
        latest = frame(0, value=80)
        latest.header.stamp.secs = 1_789_000_003
        self.sender._camera_cache_callback(two_seconds_old)
        self.sender._camera_cache_callback(latest)
        requested = frame(90)
        self.sender._detection_callback(detection(requested))
        _, metadata = self.process_next()
        self.assertEqual(metadata["image_match_policy"], "clamped_to_2s")
        self.assertEqual(metadata["requested_image_stamp"], {"secs": 1_789_000_000, "nsecs": 90})
        self.assertEqual(metadata["image_stamp"], {"secs": 1_789_000_000, "nsecs": 90})
        self.assertEqual(metadata["selected_image_stamp"], {"secs": 1_789_000_001, "nsecs": 100})
        self.assertEqual(metadata["source_stamp_sec"], 1_789_000_001)

    def test_program_b_result_json_is_cached_before_image_matching(self):
        target = SimpleNamespace(
            header=SimpleNamespace(stamp=SimpleNamespace(secs=1_789_000_003, nsecs=0)),
            global_id="global-7", target_type="车辆", timestamp=frame(99).header.stamp,
            image_stamp=frame(99).header.stamp, localization_time=frame(99).header.stamp,
            cx=50.0, cy=40.0, w=20.0, h=20.0, score=0.9,
            category_id=0, speed_mps=0.0, category="车辆", is_moving=False,
            indoor_position=False, east_m=0.0, north_m=0.0, up_m=0.0,
            latitude_deg=34.1, longitude_deg=113.9, altitude_gps_m=52.0)
        self.sender.input_mode = "completed_target_array"
        self.sender._detection_callback(SimpleNamespace(targets=[target]))
        self.assertEqual(1, self.sender.result_jobs.qsize())
        self.assertTrue(self.sender.detection_jobs.empty())
        result = self.sender.result_jobs.get_nowait()
        json_path = (Path(self.directory.name) / result["mission_id"] /
                     Path(result["file_name"]).with_suffix(".json").name)
        self.assertTrue(json_path.is_file())
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["target_id"], "global-7")
        self.assertEqual(result["message_type"], "target_result")

    def test_cached_json_reaches_ground_without_any_matching_image(self):
        target = SimpleNamespace(
            header=SimpleNamespace(stamp=SimpleNamespace(secs=1_789_000_003, nsecs=0)),
            global_id="global-8", target_type="车辆", timestamp=frame(99).header.stamp,
            image_stamp=frame(99).header.stamp, localization_time=frame(99).header.stamp,
            cx=50.0, cy=40.0, w=20.0, h=20.0, score=0.9,
            category_id=0, speed_mps=0.0, category="车辆", is_moving=False,
            indoor_position=False, east_m=0.0, north_m=0.0, up_m=0.0,
            latitude_deg=34.1, longitude_deg=113.9, altitude_gps_m=52.0)
        self.sender.input_mode = "completed_target_array"
        self.sender._detection_callback(SimpleNamespace(targets=[target]))
        result = self.sender.result_jobs.get_nowait()
        receiver_spec = importlib.util.spec_from_file_location(
            "result_only_ground_receiver", PACKAGE_ROOT / "ground" / "ground_image_receiver.py")
        receiver_module = importlib.util.module_from_spec(receiver_spec)
        receiver_spec.loader.exec_module(receiver_module)
        with tempfile.TemporaryDirectory() as output:
            receiver = receiver_module.GroundImageReceiver(
                "127.0.0.1", 0, Path(output), "test-secret", [3], status_interval=0)
            server = threading.Thread(target=receiver.serve_forever, daemon=True)
            server.start()
            deadline = time.monotonic() + 2
            while receiver.server is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(receiver.server)
            self.sender.ground_host = "127.0.0.1"
            self.sender.ground_port = receiver.server.getsockname()[1]
            try:
                self.sender._retry_pending_results(result["mission_id"])
                saved = Path(output) / "UAV3" / result["mission_id"]
                self.assertTrue((saved / Path(result["file_name"]).with_suffix(".json").name).is_file())
                self.assertFalse((saved / result["file_name"]).exists())
                self.assertTrue((Path(self.directory.name) / result["mission_id"] /
                                 Path(result["file_name"]).with_suffix(".jsonacked").name).is_file())
            finally:
                receiver.stop()
                server.join(timeout=2)

    def test_matched_frame_and_mission_survive_eviction_and_task_switch(self):
        image = frame()
        self.sender._camera_cache_callback(image)
        self.sender._detection_callback(detection(image))
        old_mission = self.sender.active_mission_id
        self.sender.frame_cache.frames.clear()
        with patch.object(threading.Thread, "start"):
            self.sender._start_mission("new-task")
        _, metadata = self.process_next()
        self.assertEqual(old_mission, metadata["mission_id"])

    def test_frame_id_mismatch_and_queue_full_are_visible_rejections(self):
        self.sender._camera_cache_callback(frame())
        request = detection()
        request.header.frame_id = "another-camera"
        self.sender._detection_callback(request)
        self.assertEqual("detection_rejected", self.statuses[-1]["state"])
        self.sender.detection_jobs = queue.Queue(maxsize=1)
        self.sender._detection_callback(detection())
        self.sender._detection_callback(detection())
        self.assertEqual("processing_queue_full", self.statuses[-1]["state"])

    def test_background_worker_processes_selected_frame(self):
        image = frame()
        self.sender._camera_cache_callback(image)
        self.sender._detection_callback(detection(image))
        worker = threading.Thread(target=self.sender._detection_worker_loop, daemon=True)
        worker.start()
        jpeg, metadata = self.sender.jobs.get(timeout=2)
        self.sender.stop_event.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(jpeg)
        self.assertEqual("request-1", metadata["request_id"])

    def test_tcp_failure_then_manifest_recovery_preserves_all_metadata(self):
        image = frame()
        self.sender._camera_cache_callback(image)
        self.sender._detection_callback(detection(image))
        jpeg, metadata = self.process_next()
        # 此用例只模拟图片连接故障；独立 JSON 通道已经收到确认。
        Path(metadata["cache_path"]).with_suffix(".jsonacked").touch()
        # 即时发送失败后仍保留原来的图片、框和扩展信息。
        self.sender.jobs.put((jpeg, metadata))
        with patch.object(self.sender, "_send_packet_with_ack", side_effect=OSError("模拟断网")):
            worker = threading.Thread(target=self.sender._worker_loop, daemon=True)
            worker.start()
            deadline = time.monotonic() + 2
            while not self.sender.jobs.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.sender.stop_event.set()
            worker.join(timeout=2)
        self.assertTrue(self.sender.recovery_pending)
        self.assertTrue(Path(metadata["cache_path"]).is_file())
        spec = importlib.util.spec_from_file_location("recovery_receiver", PACKAGE_ROOT / "ground" / "ground_image_receiver.py")
        receiver_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(receiver_module)
        with tempfile.TemporaryDirectory() as ground_directory:
            receiver = receiver_module.GroundImageReceiver("127.0.0.1", 0, Path(ground_directory), "test-secret", [3], status_interval=0)
            left, right = socket.socketpair()
            self.sender.connection = left
            receive_thread = threading.Thread(target=receiver._handle_client, args=(right, ("127.0.0.1", 0)), daemon=True)
            receive_thread.start()
            self.sender.stop_event.clear()
            try:
                total, recovered = self.sender._reconcile_once(metadata["mission_id"])
                self.assertEqual((1, 1), (total, recovered))
                ground_metadata = next(path for path in Path(ground_directory).rglob("*.json")
                                       if path.name != "target-submission.json")
                saved = json.loads(ground_metadata.read_text(encoding="utf-8"))
                for key in ("bbox", "rendered_bbox", "image_stamp", "target_latitude", "target_longitude", "target_altitude", "confidence", "target_type", "target_id", "extra"):
                    self.assertEqual(metadata[key], saved[key])
                self.assertTrue(saved["retransmission"])
                self.assertNotIn("auth_token", saved)
                self.assertEqual(jpeg, ground_metadata.with_suffix(".jpg").read_bytes())
            finally:
                self.sender._close_connection_locked()
                receive_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
