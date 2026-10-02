from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from competition_shared.competition_time import normalize_competition_time


class TaskValidationError(ValueError):
    pass


def assignment_checksum(message: Dict[str, Any]) -> str:
    body = {key: value for key, value in message.items() if key != "assignment_checksum"}
    try:
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TaskValidationError("assignment is not canonical JSON: {}".format(error))
    return hashlib.sha256(canonical).hexdigest()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TaskValidationError("{} must be a finite number".format(label))
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise TaskValidationError("{} must be a finite number".format(label))
    if not math.isfinite(result):
        raise TaskValidationError("{} must be a finite number".format(label))
    return result


def validate_assignment(
    message: Dict[str, Any], expected_uav_id: int, maximum_waypoints: int = 10000
) -> Dict[str, Any]:
    if message.get("type") != "assign_task":
        raise TaskValidationError("message type must be assign_task")
    if int(message.get("uav_id", -1)) != expected_uav_id:
        raise TaskValidationError("assignment UAV ID does not match this aircraft")
    expected_checksum = str(message.get("assignment_checksum", ""))
    actual_checksum = assignment_checksum(message)
    if not expected_checksum or expected_checksum != actual_checksum:
        raise TaskValidationError("assignment checksum does not match complete task")
    mission_id = str(message.get("mission_id", "")).strip()
    if not mission_id or len(mission_id) > 128:
        raise TaskValidationError("mission_id is missing or too long")

    task = message.get("task")
    if not isinstance(task, dict) or task.get("type") != "lawnmower_search":
        raise TaskValidationError("only lawnmower_search tasks are supported")
    if task.get("coordinate_frame") not in ("ENU", "LOCAL_NORTH_WEST"):
        raise TaskValidationError("任务坐标系必须为 ENU 或带 WGS84 航点的 LOCAL_NORTH_WEST")
    raw_waypoints = task.get("waypoints_m")
    if not isinstance(raw_waypoints, list) or not raw_waypoints:
        raise TaskValidationError("task must contain at least one waypoint")
    if len(raw_waypoints) > maximum_waypoints:
        raise TaskValidationError("task contains too many waypoints")

    waypoints: List[List[float]] = []
    for index, point in enumerate(raw_waypoints):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise TaskValidationError("waypoint {} must contain X and Y".format(index))
        _finite_number(point[0], "waypoint {} X".format(index))
        _finite_number(point[1], "waypoint {} Y".format(index))
        waypoints.append([point[0], point[1]])

    if task.get("coordinate_frame") == "LOCAL_NORTH_WEST":
        gps = task.get("waypoints_wgs84")
        if not isinstance(gps, list) or len(gps) != len(waypoints):
            raise TaskValidationError("地图航点必须逐个提供 WGS84 纬度和经度")
        for point in gps:
            if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
                raise TaskValidationError("WGS84 航点格式无效")
            lat = _finite_number(point[0], "纬度")
            lon = _finite_number(point[1], "经度")
            if abs(lat) > 90 or abs(lon) > 180:
                raise TaskValidationError("WGS84 经纬度越界")
            if len(point) == 3:
                _finite_number(point[2], "高度")
    if "speed_mps" in task and _finite_number(task["speed_mps"], "航速") <= 0:
        raise TaskValidationError("航速必须为正数")
    if task.get("reconnaissance_mode") == "hover_scan":
        duration = _finite_number(task.get("hover_scan_seconds"), "悬停扫描时长")
        if duration <= 0:
            raise TaskValidationError("悬停扫描时长必须为正数")

    target_altitude = _finite_number(
        message.get("target_altitude_m"), "target_altitude_m"
    )
    if target_altitude <= 0.0:
        raise TaskValidationError("target_altitude_m must be positive")

    # 类别是可选算法提示，不能把已经通过整体 SHA-256 校验的飞行任务拒收。
    # 发布端会在话题层过滤异常类别；航点和控制参数仍严格校验。

    normalized = json.loads(json.dumps(message))
    normalized["uav_id"] = expected_uav_id
    normalized["mission_id"] = mission_id
    normalized["target_altitude_m"] = target_altitude
    normalized["task"]["waypoints_m"] = waypoints
    if "competition_time" in normalized:
        try:
            normalized["competition_time"] = normalize_competition_time(
                normalized["competition_time"], mission_id)
        except ValueError as error:
            raise TaskValidationError("比赛计时快照无效：{}".format(error)) from error
    if task.get('transit_routes') and message.get('controller_mode') == 'external':
        from .transit_protocol import route_messages
        try:
            route_messages(normalized)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise TaskValidationError('进返场航线校验失败：{}'.format(error)) from error
    return normalized


def save_assignment_atomic(path: str, assignment: Dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(assignment, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, str(target))
        # Linux permits opening a directory so its metadata can be fsynced.
        # Windows does not; the executor runs on Ubuntu, while desktop tests
        # should still exercise the atomic file replacement.
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def load_assignment(path: str) -> Dict[str, Any]:
    with Path(path).expanduser().open("r", encoding="utf-8") as stream:
        return json.load(stream)
