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
from competition_shared.target_quality import category_fields, dedup_category, quality_metadata, quality_rank
from competition_shared.target_motion import classify_motion
from competition_shared.submission_format import format_submission
from competition_shared.target_time import load_time_references, map_localization_time


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
    """坐标与时间取自同一定位结果；绝不以图片、接收或上传时间补值。"""
    stamp = metadata.get("localization_time")
    if not isinstance(stamp, dict):
        return None
    seconds, nanoseconds = stamp.get("secs"), stamp.get("nsecs")
    if (type(seconds) is not int or type(nanoseconds) is not int
            or not 1_577_836_800 <= seconds < 4_102_444_800
            or not 0 <= nanoseconds < 1_000_000_000):
        # 拦截未校时的 1970 年、零值和错误格式，不推测或修补历史定位时间。
        return None
    try:
        beijing = datetime.timezone(datetime.timedelta(hours=8))
        instant = (datetime.datetime.fromtimestamp(seconds, beijing)
                   + datetime.timedelta(microseconds=nanoseconds // 1000))
        return instant.isoformat(timespec="milliseconds")
    except (ValueError, OSError, OverflowError):
        return None


def _target_id(metadata):
    return str(metadata.get("global_id") or metadata.get("target_id") or metadata.get("request_id") or "target-unknown")


def _observation_rank(metadata, timestamp, source_json):
    """按机载收到结果的先后排序；旧版记录退回目标时间戳。"""
    received = metadata.get("detection_received_at_unix_ns")
    if received is None:
        received = metadata.get("requested_at_unix_ns")
    try:
        order_ns = int(received)
        if order_ns <= 0:
            raise ValueError("invalid receipt time")
    except (TypeError, ValueError, OverflowError):
        try:
            order_ns = int(datetime.datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
        except (AttributeError, TypeError, ValueError, OverflowError):
            order_ns = 0
    try:
        sequence = int(metadata.get("sequence") or 0)
    except (TypeError, ValueError, OverflowError):
        sequence = 0
    return order_ns, sequence, str(source_json)


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
    return category_fields(metadata)[1]


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
    invalid_localization_times = []
    time_mappings = []
    category_records = []
    motion_classification = []
    references, reference_error = load_time_references(output_root)
    for source_json, metadata in _all_metadata(output_root, mission_id):
        if str(metadata.get("mission_id", "")) != str(mission_id):
            continue
        target_id = _target_id(metadata)
        source_uav = source_json.parent.parent.name
        match = re.fullmatch(r"UAV([1-6])", source_uav)
        metadata, time_audit = map_localization_time(
            metadata, int(match.group(1)) if match else None, references, reference_error)
        time_audit.update(source_json=str(source_json.relative_to(output_root)), target_id=target_id)
        time_mappings.append(time_audit)
        timestamp = _iso_from_metadata(metadata)
        if timestamp is None:
            invalid_localization_times.append(dict(
                source_json=str(source_json.relative_to(output_root)), target_id=target_id,
                localization_time=time_audit["original_localization_time"],
                reason=time_audit["reason"] or "缺少有效 localization_time，未以图片/接收时间补值"))
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
        rank = _observation_rank(metadata, timestamp, source_json)
        source_uav = source_json.parent.parent.name
        # 各机的全局 ID 只在该机内唯一，先分别整理，再交由发布端进行同机和跨机判重。
        record_key = (source_uav, target_id)
        item = records.setdefault(record_key, {
            "target_id": target_id, "source_uav": source_uav,
            "latest": metadata, "latest_rank": rank,
            "category_metadata": None, "category_rank": (-1, -1, ""),
            "moving": bool(metadata.get("is_moving", False)), "first_rank": rank,
            "first": metadata, "image": None, "image_rank": (-1, -1, ""),
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
        # 首次静/动属性按机载接收顺序确定；磁盘文件名及补传抵达顺序不改变它。
        if rank < item["first_rank"]:
            item["first"], item["first_rank"] = metadata, rank
            item["moving"] = bool(metadata.get("is_moving", False))
        if image_path and rank >= item["image_rank"]:
            item["image"], item["image_rank"] = "./images/" + image_name, rank
        if local_position:
            item.setdefault("local_positions", []).append(dict(local_position, timestamp=timestamp))

    features = []
    skipped_indoor = []
    for _, item in sorted(records.items()):
        target_id = item["target_id"]
        moving = item["moving"]
        # 先按收到先后归并，重复定位时间保留最后接收的点，再按定位时间排列。
        # 首次为动目标时，后续 is_moving=False 的有效定位也纳入这一条轨迹。
        unique_points = {}
        for observation, _, observed_rank in sorted(item["observations"], key=lambda entry: entry[2]):
            position, timestamp = _position(observation), _iso_from_metadata(observation)
            if position and timestamp:
                unique_points[timestamp] = {"coordinates": position, "timestamp": timestamp}
        points = sorted(unique_points.values(), key=lambda point: point["timestamp"])
        moving, motion_audit = classify_motion(item["first"], points)
        motion_classification.append({
            "source_uav": item["source_uav"], "target_id": target_id,
            "locked_is_moving": item["moving"], **motion_audit,
            "first_source_json": str(Path(item["first_rank"][2]).relative_to(output_root)),
            "first_received_order_ns": item["first_rank"][0],
            "last_source_json": str(Path(item["latest_rank"][2]).relative_to(output_root)),
            "last_received_order_ns": item["latest_rank"][0],
            "observation_count": len(item["observations"]),
            "contradictory_report_count": sum(bool(o.get("is_moving", False)) != item["moving"]
                                              for o, _, _ in item["observations"]),
        })
        if moving:
            metadata = item["category_metadata"] or item["latest"]
            matching_observations = [
                (observation, image, observed_rank)
                for observation, image, observed_rank in item["observations"]
                if _target_model(observation) == _target_model(metadata)
                and str(observation.get("target_type") or observation.get("category") or "其他")
                    == str(metadata.get("target_type") or metadata.get("category") or "其他")
            ]
            best_observation, best_image, _ = max(
                matching_observations,
                key=lambda entry: (quality_rank(entry[0], _confidence(entry[0])), entry[2]),
            )
        else:
            # 同一静目标 ID 多次上报时，最终结果只采用最后收到的一整条记录。
            metadata = item["latest"]
            best_observation, best_image, _ = max(item["observations"], key=lambda entry: entry[2])
        broad = dedup_category(metadata)
        formal_type, formal_model = category_fields(metadata)
        category_records.append({
            "source_uav": item["source_uav"], "target_id": target_id,
            "source_category": metadata.get("category"), "dedup_category": broad,
            "target_type": metadata.get("target_type"), "category_id": metadata.get("category_id"),
            "submission_target_type": formal_type, "submission_target_model": formal_model,
            "category_conflict": broad != formal_type,
        })
        confidence = _confidence(best_observation)
        properties = {
            "targetCategory": "移动" if moving else "固定",
            "targetType": category_fields(metadata)[0],
            "targetModel": _target_model(metadata),
            "imagePath": (best_image or item["image"]) if moving else best_image,
            "confidence": confidence,
        }
        # 可选字段没有值时省略，不向赛事接口发送 null。
        properties = {key: value for key, value in properties.items() if value is not None}
        if item.get("local_positions"):
            local = _local_position(metadata)
            if moving:
                properties["localPosition"] = item["local_positions"]
            elif local:
                properties["localPosition"] = [dict(local, timestamp=_iso_from_metadata(metadata))]
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
            position = _position(metadata)
            timestamp = _iso_from_metadata(metadata)
            if not position or not timestamp:
                reason = ("缺少有效 localization_time 定位源时间戳或对应的机地时间参考，不能以图像或接收时间代替" if not timestamp else
                          "室内 XYZ 或无效 WGS84 坐标不能写入 EPSG:4326 几何")
                skipped_indoor.append({"id": target_id, "reason": reason,
                                       "localPosition": item.get("local_positions", [])})
                continue
            # 静目标的坐标、时间、类别、置信度及图片均来自同一条最后反馈。
            properties["timestamp"] = timestamp
            geometry = {"type": "Point", "coordinates": position}
        feature = {"type": "Feature", "id": target_id, "geometry": geometry, "properties": properties}
        if publisher_dedup:
            feature["_quality"] = quality_metadata(best_observation)
            feature["_source_uav"] = item["source_uav"]
            feature["_dedup_category"] = broad
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
        decisions["motion_reclassifications"] = [row for row in motion_classification if row["converted_to_static"]]
        _atomic_json(mission_dir / "dedup-decisions.json", decisions)
    # 编号只作用于最终赛事文件；原始回传和去重记录继续保留源 ID。
    document, format_audit = format_submission(document)
    if publisher_dedup:
        format_audit["deduplication"] = {
            key: decisions[key] for key in ("raw_count", "deduplicated_count", "result_count",
                                            "backfilled_count", "backfilled", "selection", "static_quality_order", "moving_quality_order")
        }
    format_audit["category_records"] = category_records
    format_audit["motion_classification"] = motion_classification
    format_audit["target_time"] = dict(source="localization_time", timezone="Asia/Shanghai",
                                     utc_offset="+08:00", invalid_records=invalid_localization_times,
                                     mapping_method="publisher_epoch_from_onboard_monotonic",
                                     reference_file="time_references.json", records=time_mappings,
                                     corrected_count=sum(a["status"] == "corrected" for a in time_mappings),
                                     pending_count=sum(a["status"] == "pending" for a in time_mappings),
                                     unverified_legacy_count=sum(a["status"] == "unverified_legacy" for a in time_mappings))
    _atomic_json(mission_dir / "submission-format.json", format_audit)
    target = mission_dir / "target-submission.json"
    _atomic_json(target, document)
    return target
