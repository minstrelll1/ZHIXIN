from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List


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
    if task.get("coordinate_frame") != "ENU":
        raise TaskValidationError("task coordinate_frame must be ENU")
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

    target_altitude = _finite_number(
        message.get("target_altitude_m"), "target_altitude_m"
    )
    if target_altitude <= 0.0:
        raise TaskValidationError("target_altitude_m must be positive")

    normalized = json.loads(json.dumps(message))
    normalized["uav_id"] = expected_uav_id
    normalized["mission_id"] = mission_id
    normalized["target_altitude_m"] = target_altitude
    normalized["task"]["waypoints_m"] = waypoints
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
