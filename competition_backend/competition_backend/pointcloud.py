from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


try:  # websockets 14+
    from websockets.asyncio.client import connect as websocket_connect
except ImportError:  # pragma: no cover - compatibility with older websockets
    from websockets import connect as websocket_connect


POINTCLOUD2_FLOAT32 = 7
POINTCLOUD_SOURCE_ONBOARD = "onboard"
POINTCLOUD_SOURCE_GROUNDSTATION_RELAY = "groundstation_relay"
POINTCLOUD_SOURCE_GROUNDSTATION_SHARED = "groundstation_shared"
DEFAULT_ONBOARD_TOPIC_TEMPLATE = "/uav{uav_id}/mid_point_cloud_centers"
DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE = (
    "/uav{uav_id}/octomap_point_cloud_centers/reduce_the_frequency"
)


def parse_pointcloud_uav_ids(raw: str, default: Iterable[int]) -> List[int]:
    """Parse a comma-separated set of UAV IDs, using ``default`` when empty."""
    values = raw.split(",") if raw.strip() else [str(value) for value in default]
    result: List[int] = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        uav_id = int(value)
        if uav_id not in range(1, 7):
            raise ValueError("pointcloud UAV ID must be between 1 and 6")
        if uav_id not in result:
            result.append(uav_id)
    if not result:
        raise ValueError("at least one pointcloud UAV ID is required")
    return result


def pointcloud_topic_from_template(template: str, uav_id: int) -> str:
    """Render and validate a per-UAV ROS topic template."""
    try:
        topic = str(template).format(uav_id=int(uav_id)).strip()
    except (KeyError, ValueError) as error:
        raise ValueError("pointcloud topic template must use {uav_id}") from error
    if not topic.startswith("/"):
        raise ValueError("pointcloud topic template must render an absolute ROS topic")
    return topic


def parse_pointcloud_hosts(raw: str) -> Dict[int, str]:
    """Parse ``UAV_ID=host[:port]`` or ``UAV_ID=ws(s)://host[:port]/`` entries."""
    result: Dict[int, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, separator, value = entry.partition("=")
        if not separator:
            raise ValueError(
                "pointcloud hosts must use UAV_ID=host[:port] entries separated by ;"
            )
        uav_id = int(key.strip())
        if uav_id not in range(1, 7):
            raise ValueError("pointcloud UAV ID must be between 1 and 6")
        value = value.strip()
        if not value:
            raise ValueError("pointcloud host cannot be empty")
        result[uav_id] = value
    return result


def parse_pointcloud_topics(raw: str) -> Dict[int, str]:
    """Parse optional ``UAV_ID=/ros/topic`` overrides."""
    result: Dict[int, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, separator, value = entry.partition("=")
        if not separator:
            raise ValueError(
                "pointcloud topics must use UAV_ID=/topic entries separated by ;"
            )
        uav_id = int(key.strip())
        if uav_id not in range(1, 7):
            raise ValueError("pointcloud UAV ID must be between 1 and 6")
        topic = value.strip()
        if not topic.startswith("/"):
            raise ValueError("pointcloud ROS topic must start with /")
        result[uav_id] = topic
    return result


def _rosbridge_url(raw: str, port: int) -> str:
    value = raw.strip()
    if value.startswith(("ws://", "wss://")):
        return value
    if "://" in value:
        raise ValueError("pointcloud host must use ws:// or wss://")
    return "ws://{}:{}/".format(value.rstrip("/"), int(port))


def _field_offset(fields: Iterable[Dict[str, Any]], name: str) -> Optional[int]:
    for field in fields:
        if str(field.get("name", "")).lower() == name:
            try:
                if int(field.get("datatype", -1)) != POINTCLOUD2_FLOAT32:
                    return None
                return int(field["offset"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def decode_pointcloud2(message: Dict[str, Any], max_points: int) -> Tuple[List[List[float]], Dict[str, Any]]:
    """Decode XYZ float32 fields from one rosbridge sensor_msgs/PointCloud2 message."""
    fields = message.get("fields")
    if not isinstance(fields, list):
        raise ValueError("PointCloud2 fields must be a list")
    offsets = [_field_offset(fields, name) for name in ("x", "y", "z")]
    if any(value is None for value in offsets):
        raise ValueError("PointCloud2 must contain float32 x, y and z fields")
    try:
        point_step = int(message["point_step"])
        width = int(message.get("width", 0))
        height = int(message.get("height", 1))
        raw_data = message["data"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("PointCloud2 is missing point layout or data") from error
    if point_step <= 0 or width < 0 or height < 0:
        raise ValueError("PointCloud2 has invalid dimensions")
    if isinstance(raw_data, str):
        try:
            data = base64.b64decode(raw_data, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("PointCloud2 data is not valid base64") from error
    elif isinstance(raw_data, list):
        data = bytes(int(value) & 0xFF for value in raw_data)
    else:
        raise ValueError("PointCloud2 data must be base64 or an array")
    count = min(width * height, len(data) // point_step)
    stride = max(1, int(math.ceil(float(count) / max(1, max_points))))
    points: List[List[float]] = []
    format_prefix = ">" if bool(message.get("is_bigendian", False)) else "<"
    for index in range(0, count, stride):
        base = index * point_step
        try:
            point = [
                float(struct.unpack_from(format_prefix + "f", data, base + int(offset))[0])
                for offset in offsets
            ]
        except (struct.error, TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in point):
            points.append(point)
    header = message.get("header") if isinstance(message.get("header"), dict) else {}
    stamp = header.get("stamp") if isinstance(header.get("stamp"), dict) else {}
    try:
        stamp_seconds = float(stamp.get("secs", 0)) + float(stamp.get("nsecs", 0)) / 1e9
    except (TypeError, ValueError):
        stamp_seconds = 0.0
    return points, {
        "frame_id": str(header.get("frame_id", "")),
        "stamp": stamp_seconds,
        "width": width,
        "height": height,
        "point_step": point_step,
        "source_points": count,
        "sampled_points": len(points),
    }


@dataclass
class PointCloudFrame:
    uav_id: int
    topic: str
    received_at: float
    points: List[List[float]]
    frame_id: str
    stamp: float
    source_points: int
    path: Optional[str] = None

    def to_dict(self, include_points: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "uav_id": self.uav_id,
            "topic": self.topic,
            "received_at": self.received_at,
            "frame_id": self.frame_id,
            "stamp": self.stamp,
            "source_points": self.source_points,
            "sampled_points": len(self.points),
            "path": self.path,
        }
        if include_points:
            result["points"] = self.points
        return result


class RosbridgePointCloudCollector:
    """Subscribe to configurable ROS PointCloud2 topics and retain PCD snapshots."""

    def __init__(
        self,
        root: Path,
        hosts: Dict[int, str],
        topics: Optional[Dict[int, str]] = None,
        port: int = 9090,
        max_points: int = 20000,
        save_interval_sec: float = 1.0,
        max_frames_per_uav: int = 300,
        reconnect_sec: float = 3.0,
        source_mode: str = POINTCLOUD_SOURCE_ONBOARD,
        default_topic_template: str = DEFAULT_ONBOARD_TOPIC_TEMPLATE,
        traffic_recorder: Optional[Callable[..., None]] = None,
    ) -> None:
        self.root = root.resolve()
        self.hosts = {int(key): value for key, value in hosts.items()}
        self.topics = {int(key): value for key, value in (topics or {}).items()}
        self.port = int(port)
        self.source_mode = str(source_mode).strip() or POINTCLOUD_SOURCE_ONBOARD
        self.default_topic_template = str(default_topic_template).strip()
        self.traffic_recorder = traffic_recorder
        self.max_points = max(1, int(max_points))
        self.save_interval_sec = max(0.1, float(save_interval_sec))
        self.max_frames_per_uav = max(1, int(max_frames_per_uav))
        self.reconnect_sec = max(0.5, float(reconnect_sec))
        self._lock = threading.RLock()
        self._latest: Dict[int, PointCloudFrame] = {}
        self._errors: Dict[int, str] = {}
        self._message_count: Dict[int, int] = {}
        self._last_saved_at: Dict[int, float] = {}
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []

    def topic_for(self, uav_id: int) -> str:
        return self.topics.get(
            int(uav_id),
            pointcloud_topic_from_template(self.default_topic_template, int(uav_id)),
        )

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.hosts or self.source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            return
        self._stop_event.clear()
        self._threads = []
        for uav_id in sorted(self.hosts):
            thread = threading.Thread(
                target=self._worker,
                args=(uav_id,),
                name="pointcloud-uav{}".format(uav_id),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    def latest(self, uav_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            frame = self._latest.get(int(uav_id))
            return frame.to_dict() if frame is not None else None

    def latest_path(self, uav_id: int) -> Optional[Path]:
        with self._lock:
            frame = self._latest.get(int(uav_id))
            if frame is None or not frame.path:
                return None
            path = Path(frame.path).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        return path

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            latest = {
                str(uav_id): frame.to_dict(include_points=False)
                for uav_id, frame in self._latest.items()
            }
            return {
                "enabled": bool(self.hosts),
                "source_mode": self.source_mode,
                "configured_uav_ids": sorted(self.hosts),
                "topics": {str(uav_id): self.topic_for(uav_id) for uav_id in sorted(self.hosts)},
                "latest": latest,
                "message_count": {str(key): value for key, value in self._message_count.items()},
                "errors": dict(self._errors),
                "updated_seconds_ago": {
                    str(uav_id): max(0.0, now - frame.received_at)
                    for uav_id, frame in self._latest.items()
                },
            }

    def _worker(self, uav_id: int) -> None:
        url = _rosbridge_url(self.hosts[uav_id], self.port)
        while not self._stop_event.is_set():
            try:
                asyncio.run(self._consume(uav_id, url))
            except Exception as error:  # connection and malformed-message recovery
                with self._lock:
                    self._errors[uav_id] = str(error) or error.__class__.__name__
            self._stop_event.wait(self.reconnect_sec)

    async def _consume(self, uav_id: int, url: str) -> None:
        topic = self.topic_for(uav_id)
        async with websocket_connect(url, max_size=64 * 1024 * 1024, open_timeout=5) as socket:
            with self._lock:
                self._errors.pop(uav_id, None)
            await socket.send(json.dumps({
                "op": "subscribe", "id": "competition-pointcloud-{}".format(uav_id),
                "type": "sensor_msgs/PointCloud2", "topic": topic,
                "throttle_rate": int(self.save_interval_sec * 1000), "queue_length": 1,
            }, separators=(",", ":")))
            async for raw in socket:
                if self._stop_event.is_set():
                    return
                if not isinstance(raw, str):
                    continue
                try:
                    self.ingest_rosbridge_payload(uav_id, raw)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    with self._lock:
                        self._errors[uav_id] = str(error) or error.__class__.__name__

    def ingest_rosbridge_payload(self, uav_id: int, payload: Any) -> Dict[str, Any]:
        """Store one trusted rosbridge publish envelope without opening a socket."""
        uav_id = int(uav_id)
        if uav_id not in self.hosts:
            raise ValueError("UAV is not configured for pointcloud ingestion")
        raw_size = 0
        if isinstance(payload, str):
            raw_size = len(payload.encode("utf-8"))
            payload = json.loads(payload)
        elif isinstance(payload, dict):
            raw_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        else:
            raise ValueError("rosbridge pointcloud payload must be an object or JSON string")
        if not isinstance(payload, dict):
            raise ValueError("rosbridge pointcloud payload must be an object")
        topic = self.topic_for(uav_id)
        if payload.get("op") != "publish" or payload.get("topic") != topic:
            raise ValueError("rosbridge payload does not match the configured PointCloud2 topic")
        message = payload.get("msg")
        if not isinstance(message, dict):
            raise ValueError("rosbridge payload has no PointCloud2 message")
        # The legacy local rosbridge relay is invisible to remote-host TCP
        # counters.  Shared ingress is loopback-only and must not be counted a
        # second time because GroundStation's aircraft connection is the one
        # shown in the total already.
        if self.source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_RELAY and self.traffic_recorder is not None:
            self.traffic_recorder(uav_id, "rviz_pointcloud", received_bytes=raw_size)
        return self._store_message(uav_id, topic, message)

    def _store_message(self, uav_id: int, topic: str, message: Dict[str, Any]) -> Dict[str, Any]:
        points, metadata = decode_pointcloud2(message, self.max_points)
        received_at = time.time()
        frame = PointCloudFrame(
            uav_id=uav_id, topic=topic, received_at=received_at, points=points,
            frame_id=metadata["frame_id"], stamp=metadata["stamp"],
            source_points=metadata["source_points"],
        )
        with self._lock:
            self._message_count[uav_id] = self._message_count.get(uav_id, 0) + 1
            should_save = received_at - self._last_saved_at.get(uav_id, 0.0) >= self.save_interval_sec
            previous_path = self._latest.get(uav_id).path if uav_id in self._latest else None
        if should_save:
            frame.path = str(self._save_pcd(frame))
            with self._lock:
                self._last_saved_at[uav_id] = received_at
        else:
            frame.path = previous_path
        with self._lock:
            self._errors.pop(uav_id, None)
            self._latest[uav_id] = frame
        return frame.to_dict(include_points=False)

    def _save_pcd(self, frame: PointCloudFrame) -> Path:
        directory = self.root / "UAV{}".format(frame.uav_id)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.fromtimestamp(frame.received_at, tz=timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        target = directory / "pointcloud_{}_{}.pcd".format(timestamp, self._message_count.get(frame.uav_id, 0))
        body = b"".join(struct.pack("<fff", *point) for point in frame.points)
        header = (
            "# .PCD v0.7 - Point Cloud Data file format\n"
            "VERSION 0.7\n"
            "FIELDS x y z\n"
            "SIZE 4 4 4\n"
            "TYPE F F F\n"
            "COUNT 1 1 1\n"
            "WIDTH {}\n"
            "HEIGHT 1\n"
            "VIEWPOINT 0 0 0 1 0 0 0\n"
            "POINTS {}\n"
            "DATA binary\n"
        ).format(len(frame.points), len(frame.points)).encode("ascii")
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(header)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        self._prune(frame.uav_id, directory)
        return target

    def _prune(self, uav_id: int, directory: Path) -> None:
        files = sorted(directory.glob("pointcloud_*.pcd"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in files[self.max_frames_per_uav :]:
            try:
                path.unlink()
            except OSError:
                pass
