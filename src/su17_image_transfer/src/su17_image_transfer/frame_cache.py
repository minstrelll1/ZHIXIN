"""原始相机帧的短时缓存及算法结果校验；不依赖 ROS 运行环境。"""

from collections import OrderedDict
import json
import math
import time
import uuid


def stamp_ns(stamp):
    seconds, nanoseconds = int(stamp.secs), int(stamp.nsecs)
    if seconds < 0 or not 0 <= nanoseconds < 1_000_000_000:
        raise ValueError("图像时间戳格式无效")
    value = seconds * 1_000_000_000 + nanoseconds
    if value == 0:
        raise ValueError("图像时间戳不能为零，请复制原始图像的 header.stamp")
    return value


class FrameCache:
    """由调用者加锁；使用整数时间戳索引，使用单调时钟限制内存驻留时间。"""

    def __init__(self, duration_sec=0.5, fps=30.0, clock=time.monotonic):
        if not math.isfinite(duration_sec) or duration_sec <= 0:
            raise ValueError("帧缓存时长必须为正数")
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("相机帧率必须为正数")
        self.duration_sec = duration_sec
        self.capacity = max(1, int(math.ceil(duration_sec * fps)))
        self.clock = clock
        self.frames = OrderedDict()

    def prune(self):
        now = self.clock()
        while self.frames:
            key, (received, _) = next(iter(self.frames.items()))
            if now - received < self.duration_sec:
                break
            self.frames.pop(key)

    def add(self, message):
        key = stamp_ns(message.header.stamp)
        self.prune()
        # 重复时间戳不覆盖原帧，也不延长缓存寿命。
        if key not in self.frames:
            self.frames[key] = (self.clock(), message)
            while len(self.frames) > self.capacity:
                self.frames.popitem(last=False)
        return key

    def get(self, key):
        self.prune()
        entry = self.frames.get(key)
        return None if entry is None else entry[1]


def _finite(value, name):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s 必须为有限数值" % name)
    return number


def _reject_constant(value):
    raise ValueError("扩展信息不能包含 %s" % value)


def detection_metadata(message):
    """将自定义 ROS 消息转换为独立的 JSON 元数据，扩展字段不覆盖协议字段。"""
    key = stamp_ns(message.header.stamp)
    box = {name: _finite(getattr(message, name), name)
           for name in ("center_x", "center_y", "width", "height")}
    if box["width"] <= 0 or box["height"] <= 0:
        raise ValueError("目标框宽高必须大于零")
    latitude = _finite(message.target_latitude, "目标纬度")
    longitude = _finite(message.target_longitude, "目标经度")
    altitude = _finite(message.target_altitude, "目标高度")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("目标经纬度超出有效范围")
    confidence = _finite(message.confidence, "置信度")
    if not 0 <= confidence <= 1:
        raise ValueError("置信度必须在 0～1 之间")
    target_type = message.target_type.strip()
    if not target_type:
        raise ValueError("目标类型不能为空")
    raw_extra = message.extra_json.strip() or "{}"
    if len(raw_extra.encode("utf-8")) > 65536:
        raise ValueError("扩展 JSON 不能超过 64 KiB")
    extra = json.loads(raw_extra, parse_constant=_reject_constant)
    if not isinstance(extra, dict):
        raise ValueError("扩展 JSON 必须是对象")
    # 也拒绝 1e999 之类被 JSON 解码为无穷大的数值。
    json.dumps(extra, allow_nan=False)
    request_id = message.request_id.strip() or "target-%d-%s" % (
        key, uuid.uuid4().hex[:8]
    )
    return key, {
        "request_id": request_id,
        "target_id": message.target_id,
        "image_stamp": {"secs": int(message.header.stamp.secs),
                        "nsecs": int(message.header.stamp.nsecs)},
        "detection_frame_id": message.header.frame_id,
        "bbox": dict(box, units="pixels", origin="top_left"),
        "target_latitude": latitude,
        "target_longitude": longitude,
        "target_altitude": altitude,
        "coordinate_system": "WGS84",
        "confidence": confidence,
        "target_type": target_type,
        "extra": extra,
    }


def completed_target_metadata(target):
    """将附件定义的 CompletedTarget 转为通用回传元数据。"""
    source_stamp = target.timestamp
    if int(source_stamp.secs) == 0 and int(source_stamp.nsecs) == 0:
        source_stamp = target.image_stamp
    key = stamp_ns(source_stamp)
    width = _finite(target.w, "目标框宽度")
    height = _finite(target.h, "目标框高度")
    if width <= 0 or height <= 0:
        raise ValueError("目标框宽高必须大于零")
    target_type = str(target.target_type or target.category).strip()
    if not target_type:
        raise ValueError("目标类型不能为空")
    raw_score = float(target.score)
    score = _finite(raw_score, "置信度") if math.isfinite(raw_score) else None
    if score is not None and not 0 <= score <= 1:
        raise ValueError("置信度必须在 0～1 之间")
    metadata = {
        "request_id": "completed-%s-%s" % (key, str(target.global_id or "target")),
        "target_id": str(target.global_id),
        "image_stamp": {"secs": int(source_stamp.secs), "nsecs": int(source_stamp.nsecs)},
        # CompletedTarget.header.frame_id 是 wgs84，不是相机 frame。
        "detection_frame_id": "",
        "bbox": {"center_x": _finite(target.cx, "目标框中心X"),
                 "center_y": _finite(target.cy, "目标框中心Y"),
                 "width": width, "height": height,
                 "units": "pixels", "origin": "top_left"},
        "confidence": score,
        "target_type": target_type,
        "category": str(target.category),
        "category_id": int(target.category_id),
        "speed_mps": _finite(target.speed_mps, "目标速度") if math.isfinite(float(target.speed_mps)) else None,
        "is_moving": bool(target.is_moving),
        "indoor_position": bool(target.indoor_position),
        "feedback_header_stamp": {"secs": int(target.header.stamp.secs),
                                   "nsecs": int(target.header.stamp.nsecs)},
        "localization_time": {"secs": int(target.localization_time.secs),
                               "nsecs": int(target.localization_time.nsecs)},
        "feedback_image_stamp": {"secs": int(target.image_stamp.secs),
                                  "nsecs": int(target.image_stamp.nsecs)},
    }
    if metadata["indoor_position"]:
        metadata.update({"east_m": _finite(target.east_m, "东向坐标"),
                         "north_m": _finite(target.north_m, "北向坐标"),
                         "up_m": _finite(target.up_m, "天向坐标"),
                         "coordinate_system": "local_xyz"})
    else:
        latitude = _finite(target.latitude_deg, "纬度")
        longitude = _finite(target.longitude_deg, "经度")
        altitude = _finite(target.altitude_gps_m, "GPS 椭球高")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("WGS84 经纬度超出有效范围")
        metadata.update({"target_latitude": latitude, "target_longitude": longitude,
                         "target_altitude": altitude, "coordinate_system": "WGS84"})
    return key, metadata


def clipped_rectangle(box, image_width, image_height):
    """像素坐标采用中心和宽高，输出 OpenCV 使用的闭区间角点。"""
    left = box["center_x"] - box["width"] / 2
    top = box["center_y"] - box["height"] / 2
    right = box["center_x"] + box["width"] / 2
    bottom = box["center_y"] + box["height"] / 2
    if right <= 0 or bottom <= 0 or left >= image_width or top >= image_height:
        raise ValueError("目标框完全位于图像范围外")
    return (
        int(math.floor(max(0, left))),
        int(math.floor(max(0, top))),
        int(math.ceil(min(image_width, right))) - 1,
        int(math.ceil(min(image_height, bottom))) - 1,
    )
