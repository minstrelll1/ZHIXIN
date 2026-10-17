"""Latest deduplicated subject-one targets shared with ground and ROS."""

import copy
import math
import re

from .recognition import CATEGORY_CATALOG


def _category_id(target_type, target_model):
    target_type = {"车": "车辆", "人": "人员", "建筑物": "工事"}.get(target_type, target_type)
    for item in CATEGORY_CATALOG:
        if item["name"] == target_model:
            identifier = item["id"]
            if identifier < 7 and target_type == "车辆":
                return identifier
            if 7 <= identifier <= 10 and target_type == "工事":
                return identifier
            if 11 <= identifier <= 14 and target_type == "人员":
                return identifier
    return None


def empty_fixed_targets():
    return dict(schema_version=1, coordinate_frame="WGS84", source_mission_id="", targets=[])


def validate_fixed_targets(document):
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("定点目标 schema_version 必须为 1")
    if document.get("coordinate_frame") != "WGS84":
        raise ValueError("定点目标坐标系必须为 WGS84")
    source = document.get("source_mission_id")
    if not isinstance(source, str) or (source and not re.fullmatch(r"subject1-[A-Za-z0-9_.-]{1,119}", source)):
        raise ValueError("定点目标必须来自科目一任务")
    targets = document.get("targets")
    if not isinstance(targets, list) or len(targets) > 16:
        raise ValueError("定点目标列表无效")
    seen = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("定点目标必须为 JSON 对象")
        for key in ("target_id", "target_type", "target_model"):
            if not isinstance(target.get(key), str) or not target[key].strip():
                raise ValueError("定点目标缺少 " + key)
        if target["target_id"] in seen:
            raise ValueError("定点目标编号重复")
        seen.add(target["target_id"])
        target_category = target.get("target_category", "固定")  # 兼容旧版工事记录。
        if target_category != "固定":
            raise ValueError("定点目标只允许固定目标")
        category = target.get("category_id")
        expected = _category_id(target["target_type"], target["target_model"])
        if expected is None and any(item["name"] == target["target_model"] for item in CATEGORY_CATALOG):
            raise ValueError("固定目标类型与型号不一致")
        if expected is not None and (type(category) is not int or category != expected):
            raise ValueError("固定目标类别编号与型号不一致")
        if expected is None and category is not None and (type(category) is not int or not 0 <= category <= 18):
            raise ValueError("未知型号的目标类别编号必须为 0～18 或 null")
        for key, bound in (("latitude", 90), ("longitude", 180)):
            value = target.get(key)
            if type(value) not in (int, float) or abs(value) > bound or not math.isfinite(value):
                raise ValueError("定点目标经纬度无效：" + key)
    return copy.deepcopy(document)


def fixed_targets_from_submission(document, mission_id):
    """Keep every fixed target in the final submission's existing order."""
    if document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
        raise ValueError("缺少已整理的目标成果")
    result = empty_fixed_targets()
    result["source_mission_id"] = mission_id
    for feature in document["features"]:
        props = feature["properties"]
        if props.get("targetCategory") != "固定":
            continue
        target_type, model = props["targetType"], props["targetModel"]
        geometry = feature["geometry"]
        position = geometry.get("coordinates")
        if geometry.get("type") != "Point":
            raise ValueError("固定目标须为 Point 经纬度")
        if not isinstance(position, list) or len(position) != 2:
            raise ValueError("目标缺少有效经纬度")
        result["targets"].append(dict(
            target_id=feature["id"], target_category="固定", target_type=target_type, target_model=model,
            category_id=_category_id(target_type, model),
            longitude=position[0], latitude=position[1],
        ))
    return validate_fixed_targets(result)


def fixed_targets_message(assignment):
    document = validate_fixed_targets(assignment.get("fixed_targets", empty_fixed_targets()))
    return dict(document, mission_id=assignment.get("mission_id", ""),
                uav_id=assignment["uav_id"], assignment_checksum=assignment.get("assignment_checksum", ""))
