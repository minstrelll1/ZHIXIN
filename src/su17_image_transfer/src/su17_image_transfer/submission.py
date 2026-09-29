"""将已落盘的目标反馈整理为科目一 GeoJSON 提交包。"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _iso_from_metadata(metadata):
    stamp = metadata.get("image_stamp") or {}
    try:
        seconds = int(stamp["secs"])
        nanoseconds = int(stamp["nsecs"])
        instant = datetime.datetime.fromtimestamp(
            seconds + nanoseconds / 1_000_000_000, datetime.timezone.utc
        )
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        value = metadata.get("requested_at_unix_ns")
        try:
            instant = datetime.datetime.fromtimestamp(
                int(value) / 1_000_000_000, datetime.timezone.utc
            )
            return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OSError, OverflowError):
            return None


def _target_id(metadata):
    return str(metadata.get("global_id") or metadata.get("target_id") or metadata.get("request_id") or "target-unknown")


def _position(metadata):
    if bool(metadata.get("indoor_position", False)):
        return None
    longitude = _number(metadata.get("longitude_deg", metadata.get("target_longitude")))
    latitude = _number(metadata.get("latitude_deg", metadata.get("target_latitude")))
    if longitude is None or latitude is None or not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None
    return [longitude, latitude]  # 赛事模板使用二维经纬度，高度保留在原始反馈中。


def _local_position(metadata):
    if not bool(metadata.get("indoor_position", False)):
        return None
    return {
        "east_m": _number(metadata.get("east_m")),
        "north_m": _number(metadata.get("north_m")),
        "up_m": _number(metadata.get("up_m")),
        "coordinateSystem": "local_xyz",
    }


def _target_model(metadata):
    extra = metadata.get("extra")
    if isinstance(extra, dict):
        value = extra.get("targetModel") or extra.get("target_model")
        if value:
            return str(value)
    category_id = metadata.get("category_id")
    return str(metadata.get("target_model") or ("类别%s" % category_id if category_id not in (None, "", -1) else metadata.get("target_type", "未知")))


def _all_metadata(output_root):
    for path in output_root.glob("UAV*/*/*.json"):
        if path.name == "subject1_submission.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                yield path, data
        except (OSError, ValueError):
            continue


def update_subject1_submission(output_root, mission_id, team_name=None):
    """生成指定任务的 JSON 和 images 目录，采用临时文件原子替换。"""
    output_root = Path(output_root).resolve()
    mission_dir = (output_root / "subject1_submissions" / str(mission_id)).resolve()
    if output_root not in mission_dir.parents:
        raise ValueError("unsafe submission path")
    image_dir = mission_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    for source_json, metadata in _all_metadata(output_root):
        if str(metadata.get("mission_id", "")) != str(mission_id):
            continue
        target_id = _target_id(metadata)
        timestamp = _iso_from_metadata(metadata)
        position = _position(metadata)
        local_position = _local_position(metadata)
        image_source = source_json.with_suffix(".jpg")
        image_name = "%s_%s.jpg" % (hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:16], source_json.stem)
        image_path = None
        if image_source.is_file():
            image_path = image_dir / image_name
            temp = image_path.with_suffix(".jpg.part")
            shutil.copyfile(image_source, temp)
            os.replace(temp, image_path)
        item = records.setdefault(target_id, {"points": [], "first": metadata, "image": None})
        if image_path:
            item["image"] = "./images/" + image_name
        point = {"coordinates": position, "timestamp": timestamp} if position and timestamp else None
        if point and (not item["points"] or item["points"][-1] != point):
            item["points"].append(point)
        if local_position:
            item.setdefault("local_positions", []).append(dict(local_position, timestamp=timestamp))

    features = []
    skipped_indoor = []
    for target_id, item in sorted(records.items()):
        metadata = item["first"]
        moving = bool(metadata.get("is_moving", False))
        points = sorted(item["points"], key=lambda point: point["timestamp"])
        properties = {
            "targetCategory": "移动" if moving else "固定",
            "targetType": str(metadata.get("target_type") or metadata.get("category") or "其他"),
            "targetModel": _target_model(metadata),
            "imagePath": item["image"],
            "confidence": _number(metadata.get("confidence", metadata.get("score"))),
        }
        # 可选字段没有值时省略，不向赛事接口发送 null。
        properties = {key: value for key, value in properties.items() if value is not None}
        if item.get("local_positions"):
            properties["localPosition"] = item["local_positions"]
        if moving:
            if len(points) < 2:
                # 轨迹不足时保留原始反馈，但不伪造符合模板的 LineString。
                skipped_indoor.append({"id": target_id, "reason": "动态目标轨迹少于两个有效 WGS84 点",
                                       "localPosition": item.get("local_positions", [])})
                continue
            properties["trackStartTime"] = points[0]["timestamp"]
            properties["trackEndTime"] = points[-1]["timestamp"]
            properties["trackPoints"] = points
            geometry = {"type": "LineString", "coordinates": [p["coordinates"] for p in points]}
        else:
            if not points:
                skipped_indoor.append({"id": target_id, "reason": "室内 XYZ 或无效 WGS84 坐标不能写入 EPSG:4326 几何",
                                       "localPosition": item.get("local_positions", [])})
                continue
            properties["timestamp"] = points[0]["timestamp"]
            geometry = {"type": "Point", "coordinates": points[-1]["coordinates"]}
        features.append({"type": "Feature", "id": target_id, "geometry": geometry, "properties": properties})

    document = {
        "type": "FeatureCollection",
        "name": team_name or os.environ.get("COMPETITION_TEAM_NAME", "北方自控智群队"),
        "description": "科目一目标结果（由机载时间戳图片回传生成）",
        "crs": {"type": "lonlat", "properties": {"lonlat": "EPSG:4326"}},
        "features": features,
        "metadata": {
            "version": "1.0",
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "coordinateSystem": "WGS84",
            "coordinateOrder": "[经度 longitude, 纬度 latitude]",
            "targetCategories": ["固定", "移动"],
            "targetTypes": ["车辆", "人员", "工事", "其他"],
            "indoorTargets": skipped_indoor,
        },
    }
    target = mission_dir / "target-submission.json"
    fd, temporary = tempfile.mkstemp(prefix="target-submission-", suffix=".json.part", dir=str(mission_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
