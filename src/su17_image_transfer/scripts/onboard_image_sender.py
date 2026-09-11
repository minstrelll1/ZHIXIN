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

import cv2
import message_filters
import numpy as np
import rospy
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import CompressedImage, Image, NavSatFix
from std_msgs.msg import String
from std_srvs.srv import Trigger, TriggerResponse

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont
except ImportError:
    PILImage = None
    ImageDraw = None
    ImageFont = None

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
        self.input_mode = str(rospy.get_param("~input_mode", "paired_topics")).strip()
        self.compressed_input = bool(rospy.get_param("~compressed_input", False))
        self.ground_host = rospy.get_param("~ground_host", "192.168.1.123")
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
            rospy.get_param("~reconcile_after_minutes", 19.0)
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

        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("~jpeg_quality must be between 1 and 100")
        if not 1 <= self.ground_port <= 65535:
            raise ValueError("~ground_port is invalid")
        if self.reconcile_retry_sec <= 0:
            raise ValueError("~reconcile_retry_sec must be greater than zero")
        if self.live_retry_interval <= 0:
            raise ValueError("~live_retry_interval_sec must be greater than zero")

        prefix = "/uav%d/image_transfer" % self.uav_id
        self.target_image_topic = rospy.get_param(
            "~target_image_topic", prefix + "/target_image"
        )
        self.target_coordinate_topic = rospy.get_param(
            "~target_coordinate_topic", prefix + "/target_coordinate"
        )
        self.sync_queue_size = int(rospy.get_param("~sync_queue_size", 20))
        self.sync_slop_sec = float(rospy.get_param("~sync_slop_sec", 0.5))
        self.coordinate_font_size = int(rospy.get_param("~coordinate_font_size", 28))
        self.coordinate_font_path = str(
            rospy.get_param("~coordinate_font_path", "")
        ).strip()
        if self.sync_queue_size <= 0:
            raise ValueError("~sync_queue_size must be greater than zero")
        if self.sync_slop_sec < 0:
            raise ValueError("~sync_slop_sec cannot be negative")
        if self.coordinate_font_size <= 0:
            raise ValueError("~coordinate_font_size must be greater than zero")
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
        self.coordinate_font = None
        if self.input_mode == "paired_topics":
            self.coordinate_font = self._load_coordinate_font()
        elif self.input_mode != "capture_request":
            raise ValueError("~input_mode must be paired_topics or capture_request")
        self.latest_lock = threading.Lock()
        self.latest_message = None
        self.latest_received_monotonic = 0.0
        self.jobs = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.connection = None
        self.connection_lock = threading.Lock()
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

        self.status_publisher = rospy.Publisher(
            self.status_topic, String, queue_size=20
        )
        self.mission_subscriber = rospy.Subscriber(
            self.mission_control_topic,
            String,
            self._mission_control_callback,
            queue_size=10,
        )

        self._start_mission(configured_mission_id or self._new_mission_id())

        message_type = CompressedImage if self.compressed_input else Image
        self.image_subscriber = None
        self.coordinate_subscriber = None
        self.target_synchronizer = None
        self.capture_service = None
        self.command_subscriber = None
        if self.input_mode == "paired_topics":
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
        self.reconcile_timer = rospy.Timer(
            rospy.Duration(1.0), self._reconcile_timer_callback
        )
        rospy.on_shutdown(self.shutdown)

        rospy.loginfo(
            "SU17 image sender ready: mode=%s, image=%s, coordinate=%s, "
            "mission_topic=%s, "
            "ground=%s:%d, disk_recovery=%s, reconcile=%.2f min",
            self.input_mode,
            self.target_image_topic if self.input_mode == "paired_topics" else self.image_topic,
            self.target_coordinate_topic if self.input_mode == "paired_topics" else "disabled",
            self.mission_control_topic,
            self.ground_host,
            self.ground_port,
            self.enable_offline_recovery,
            self.reconcile_after_minutes,
        )

    def _load_coordinate_font(self):
        if PILImage is None or ImageDraw is None or ImageFont is None:
            raise RuntimeError(
                "paired_topics mode requires Pillow; install package python3-pil"
            )
        candidates = [
            self.coordinate_font_path,
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                rospy.loginfo("Coordinate annotation font: %s", candidate)
                return ImageFont.truetype(candidate, self.coordinate_font_size)
        raise RuntimeError(
            "no Chinese font found; install fonts-wqy-zenhei or set "
            "~coordinate_font_path"
        )

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
        mission_dir = self._mission_cache_dir(mission_id)
        if self.cache_root not in mission_dir.parents:
            raise ValueError("unsafe mission cache path")
        if self.enable_offline_recovery:
            mission_dir.mkdir(parents=True, exist_ok=True)

        highest_sequence = 0
        if mission_dir.exists():
            for image_path in mission_dir.glob("*.jpg"):
                match = re.search(r"_([0-9]{6})_[0-9]{8}T", image_path.name)
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

        mission_metadata = {
            "mission_id": mission_id,
            "mission_started_at_unix_ns": started_at_unix_ns,
        }
        self._publish_status("mission_started", mission_metadata)
        rospy.loginfo("Image mission started: %s", mission_id)
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
            self._publish_status("ground_mission_started", metadata)
        except Exception as exc:
            metadata["error"] = str(exc)
            self._publish_status("mission_announce_pending", metadata)
            rospy.logwarn("Ground mission timer announcement failed: %s", exc)

    def _mission_control_callback(self, message: String) -> None:
        command = message.data.strip()
        if command == "start":
            self._start_mission(self._new_mission_id())
        elif command.startswith("start:"):
            self._start_mission(command.split(":", 1)[1])
        elif command == "sync":
            self._schedule_reconciliation(force=True)
        else:
            rospy.logwarn(
                "Unknown mission control command '%s'; use start, start:<id>, or sync",
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
                "Target pair rejected: invalid coordinate longitude=%r latitude=%r",
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
                "Target image queued: request=%s longitude=%.7f latitude=%.7f",
                request_id,
                longitude,
                latitude,
            )
        else:
            rospy.logwarn("Target pair rejected: %s", reason)

    def _capture_service_callback(self, _request) -> TriggerResponse:
        accepted, request_id, reason = self._enqueue_capture("")
        message = request_id if accepted else reason
        return TriggerResponse(success=accepted, message=message)

    def _capture_topic_callback(self, message: String) -> None:
        accepted, request_id, reason = self._enqueue_capture(message.data.strip())
        if not accepted:
            rospy.logwarn("Capture request rejected: %s", reason)
        else:
            rospy.loginfo("Capture request queued: %s", request_id)

    def _enqueue_capture(self, requested_id):
        with self.latest_lock:
            image_message = self.latest_message
            received_at = self.latest_received_monotonic

        if image_message is None:
            return False, "", "camera has not published an image yet"
        age = time.monotonic() - received_at
        if self.max_frame_age > 0 and age > self.max_frame_age:
            return False, "", "latest camera frame is %.2f seconds old" % age

        return self._enqueue_image(image_message, requested_id)

    def _enqueue_image(
        self,
        image_message,
        requested_id,
        latitude=None,
        longitude=None,
        coordinate_message=None,
    ):

        with self.mission_lock:
            self.sequence += 1
            sequence = self.sequence
            mission_id = self.active_mission_id
            mission_started_at_unix_ns = self.mission_started_at_unix_ns

        request_id = requested_id or uuid.uuid4().hex
        timestamp_ns = time.time_ns()
        utc_stamp = datetime.datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000, datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%S.%fZ")
        file_name = "%s_UAV%02d_%06d_%s.jpg" % (
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
        }
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
                image_message, latitude=latitude, longitude=longitude
            )
            metadata["width"] = width
            metadata["height"] = height
            metadata["jpeg_size"] = len(jpeg)
            if self.enable_offline_recovery:
                image_path = self._cache_image(metadata, jpeg)
                metadata["cache_path"] = str(image_path)
                self._publish_status("cached", metadata)
                rospy.loginfo("Image cached before transfer: %s", image_path)
        except Exception as exc:
            return False, "", "could not persist current image: %s" % exc

        try:
            self.jobs.put_nowait((jpeg, metadata))
            self._publish_status("queued", metadata)
        except queue.Full:
            if not self.enable_offline_recovery:
                return False, "", "capture queue is full"
            metadata["error"] = "live transfer queue is full; image is safe on disk"
            self._mark_reconciliation_needed(metadata["mission_id"])
            self._publish_status("pending_recovery", metadata)
        return True, request_id, ""

    def _message_to_jpeg(self, message, latitude=None, longitude=None):
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

        if latitude is not None and longitude is not None:
            image = self._append_coordinate_label(image, latitude, longitude)

        ok, encoded_jpeg = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not ok:
            raise RuntimeError("OpenCV JPEG encoding failed")
        return encoded_jpeg.tobytes(), int(image.shape[1]), int(image.shape[0])

    def _append_coordinate_label(self, image, latitude, longitude):
        if self.coordinate_font is None:
            raise RuntimeError("Chinese coordinate font is not configured")
        line_height = self.coordinate_font_size + 6
        margin = 10
        bar_height = line_height * 2 + margin * 2
        canvas = np.full(
            (int(image.shape[0]) + bar_height, int(image.shape[1]), 3),
            255,
            dtype=np.uint8,
        )
        canvas[: image.shape[0], : image.shape[1]] = image
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        pil_image = PILImage.fromarray(rgb)
        draw = ImageDraw.Draw(pil_image)
        x = 12
        y = int(image.shape[0]) + margin
        draw.text(
            (x, y),
            "目标经度：%.7f°" % longitude,
            font=self.coordinate_font,
            fill=(0, 0, 0),
        )
        draw.text(
            (x, y + line_height),
            "目标纬度：%.7f°" % latitude,
            font=self.coordinate_font,
            fill=(0, 0, 0),
        )
        return cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)

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

    def _connect_locked(self):
        self._close_connection_locked()
        connection = socket.create_connection(
            (self.ground_host, self.ground_port), timeout=self.connect_timeout
        )
        connection.settimeout(self.ack_timeout)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
                    raise RuntimeError("ground receiver rejected the image")
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
                    "%s attempt %d/%d failed: %s",
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
                    metadata["error"] = "network send temporarily suppressed after failure"
                    self._mark_reconciliation_needed(metadata["mission_id"])
                    self._publish_status("pending_recovery", metadata)
                    continue

                packet = encode_frame(metadata, jpeg)
                try:
                    self._send_packet_with_ack(packet)
                    self.live_send_suppressed_until = 0.0
                    self._publish_status("sent", metadata)
                    rospy.loginfo(
                        "Image sent: file=%s bytes=%d",
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
                        "Image remains on onboard disk for reconciliation: %s",
                        metadata["file_name"],
                    )
            except Exception as exc:
                metadata["error"] = str(exc)
                self._publish_status("failed", metadata)
                rospy.logerr("Capture processing failed: %s", exc)
            finally:
                self.jobs.task_done()

    def _reconcile_timer_callback(self, _event):
        if not self.enable_offline_recovery:
            return
        with self.mission_lock:
            elapsed = time.monotonic() - self.mission_started_monotonic
            final_due = (
                self.reconcile_after_minutes >= 0
                and elapsed >= self.reconcile_after_minutes * 60.0
                and not self.final_reconcile_done
            )
            recovery_pending = self.recovery_pending
        if final_due:
            # The scheduled final audit must still run even if an earlier
            # automatic recovery or manual sync already completed.
            self._schedule_reconciliation(force=False, final=True)
        elif recovery_pending:
            # A failed live transfer is retried automatically. When the network
            # comes back, the next manifest exchange recovers missing files.
            self._schedule_reconciliation(force=False, final=False)

    def _schedule_reconciliation(self, force, final=False):
        now = time.monotonic()
        with self.mission_lock:
            if self.reconcile_in_progress:
                return
            if final and self.final_reconcile_done:
                return
            if self.reconcile_done and not force and not final:
                return
            if not force and now - self.last_reconcile_attempt < self.reconcile_retry_sec:
                return
            mission_id = self.active_mission_id
            if force:
                self.reconcile_done = False
            self.reconcile_in_progress = True
            self.last_reconcile_attempt = now
            recovery_revision = self.recovery_revision
            reconcile_trigger = (
                "scheduled_final"
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
            raise RuntimeError("ground manifest response has invalid missing list")
        path_by_name = {path.name: path for path in image_paths}
        missing = [name for name in missing if name in path_by_name]
        rospy.loginfo(
            "Manifest compared: mission=%s onboard=%d ground_present=%d missing=%d",
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
                encode_frame(metadata, jpeg), "recovery image %s" % file_name
            )
            if not success:
                raise RuntimeError("could not recover %s: %s" % (file_name, error))

        verification = self._exchange_control(manifest)
        remaining = verification.get("missing", [])
        if remaining:
            raise RuntimeError("ground still misses %d images" % len(remaining))
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
            total, recovered = self._reconcile_once(mission_id)
            metadata.update({"total": total, "recovered": recovered})
            self._publish_status("reconcile_complete", metadata)
            rospy.loginfo(
                "Reconciliation complete: mission=%s trigger=%s total=%d recovered=%d",
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
                "Reconciliation failed: trigger=%s; retry in %.1f seconds: %s",
                reconcile_trigger,
                self.reconcile_retry_sec,
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
