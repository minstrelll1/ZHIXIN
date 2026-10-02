"""将已落盘的目标反馈整理为科目一 GeoJSON 提交包。"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from contextlib import contextmanager


_SAFE_MISSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")


@contextmanager
def _submission_lock(mission_dir):
    """跨线程及进程串行化同一任务的 JSON/图片重建。"""
    lock_path = mission_dir / ".submission.lock"
    with lock_path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            # msvcrt.locking 从当前文件位置锁定一个字节。
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _confidence(metadata):
    value = _number(metadata.get("confidence", metadata.get("score")))
    return value if value is not None and 0 <= value <= 1 else None


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


def _all_metadata(output_root, mission_id):
    # 标准任务编号直接限定扫描范围，避免每次重新遍历历史任务。
    if _SAFE_MISSION_ID.fullmatch(str(mission_id)):
        paths = output_root.glob("UAV*/%s/*.json" % mission_id)
    else:
        paths = output_root.glob("UAV*/*/*.json")
    for path in sorted(paths):
        if path.name == "subject1_submission.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                yield path, data
        except (OSError, ValueError):
            continue


def update_subject1_submission(output_root, mission_id, team_name=None, *, publisher_dedup=False):
    """生成指定任务的 JSON 和 images 目录，采用进程锁与临时文件原子替换。"""
    output_root = Path(output_root).resolve()
    mission_id = str(mission_id)
    if mission_id in (".", "..") or not _SAFE_MISSION_ID.fullmatch(mission_id):
        raise ValueError("invalid submission mission id")
    # 任务编号只有一个受限路径组件，无需在多线程建目录期间再次 resolve。
    mission_dir = output_root / "subject1_submissions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    with _submission_lock(mission_dir):
        return _update_subject1_submission_locked(output_root, mission_dir, mission_id,
                                                  team_name, publisher_dedup)


def _atomic_json(path, document):
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".json.part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _update_subject1_submission_locked(output_root, mission_dir, mission_id, team_name,
                                       publisher_dedup=False):
    image_dir = mission_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    for source_json, metadata in _all_metadata(output_root, mission_id):
        if str(metadata.get("mission_id", "")) != str(mission_id):
            continue
        target_id = _target_id(metadata)
        timestamp = _iso_from_metadata(metadata)
        position = _position(metadata)
        local_position = _local_position(metadata)
        image_source = source_json.with_suffix(".jpg")
        image_name = "%s_%s_%s.jpg" % (
            hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:16],
            source_json.parent.parent.name, source_json.stem,
        )
        image_path = None
        if image_source.is_file():
            image_path = image_dir / image_name
            source_stat = image_source.stat()
            if (not image_path.is_file() or image_path.stat().st_size != source_stat.st_size
                    or image_path.stat().st_mtime_ns != source_stat.st_mtime_ns):
                fd, temporary = tempfile.mkstemp(prefix=image_name + "-", suffix=".jpg.part", dir=str(image_dir))
                os.close(fd)
                try:
                    shutil.copy2(image_source, temporary)
                    os.replace(temporary, image_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        rank = (timestamp or "", str(source_json))
        source_uav = source_json.parent.parent.name
        record_key = (source_uav, target_id) if publisher_dedup else target_id
        item = records.setdefault(record_key, {
            "target_id": target_id, "source_uav": source_uav,
            "points": [], "latest": metadata, "latest_rank": rank,
            "category_metadata": None, "category_rank": ("", ""),
            "moving": False, "image": None, "image_rank": ("", ""),
            "observations": [],
        })
        item["observations"].append((metadata, "./images/" + image_name if image_path else None, rank))
        if rank >= item["latest_rank"]:
            item["latest"], item["latest_rank"] = metadata, rank
        extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
        has_category = (
            any(metadata.get(key) not in (None, "") for key in ("target_type", "category", "target_model"))
            or metadata.get("category_id") not in (None, "", -1)
            or bool(extra.get("targetModel") or extra.get("target_model"))
        )
        if has_category and rank >= item["category_rank"]:
            item["category_metadata"], item["category_rank"] = metadata, rank
        item["moving"] = item["moving"] or bool(metadata.get("is_moving", False))
        if image_path and rank >= item["image_rank"]:
            item["image"], item["image_rank"] = "./images/" + image_name, rank
        point = {"coordinates": position, "timestamp": timestamp} if position and timestamp else None
        if point and (not item["points"] or item["points"][-1] != point):
            item["points"].append(point)
        if local_position:
            item.setdefault("local_positions", []).append(dict(local_position, timestamp=timestamp))

    features = []
    skipped_indoor = []
    for _, item in sorted(records.items()):
        target_id = item["target_id"]
        metadata = item["category_metadata"] or item["latest"]
        moving = item["moving"]
        points = sorted(item["points"], key=lambda point: point["timestamp"])
        matching_observations = [
            (observation, image, observed_rank)
            for observation, image, observed_rank in item["observations"]
            if _target_model(observation) == _target_model(metadata)
            and str(observation.get("target_type") or observation.get("category") or "其他")
                == str(metadata.get("target_type") or metadata.get("category") or "其他")
        ]
        best_observation, best_image, _ = max(
            matching_observations,
            key=lambda entry: (_confidence(entry[0]) if _confidence(entry[0]) is not None else -1.0,
                               entry[2]),
        )
        confidence = _confidence(best_observation)
        properties = {
            "targetCategory": "移动" if moving else "固定",
            "targetType": str(metadata.get("target_type") or metadata.get("category") or "其他"),
            "targetModel": _target_model(metadata),
            "imagePath": best_image or item["image"],
            "confidence": confidence,
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
            # 坐标和时间必须来自同一条真实反馈；固定目标采用最近一次有效点。
            properties["timestamp"] = points[-1]["timestamp"]
            geometry = {"type": "Point", "coordinates": points[-1]["coordinates"]}
        feature = {"type": "Feature", "id": target_id, "geometry": geometry, "properties": properties}
        if publisher_dedup:
            feature["_source_uav"] = item["source_uav"]
        features.append(feature)

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
    if publisher_dedup:
        from competition_shared.subject1_dedup import consolidate

        _atomic_json(mission_dir / "raw-targets.json", document)
        document, decisions = consolidate(document)
        _atomic_json(mission_dir / "dedup-decisions.json", decisions)
    target = mission_dir / "target-submission.json"
    _atomic_json(target, document)
    return target
