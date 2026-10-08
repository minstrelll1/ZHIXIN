"""目标类别与质量排序；质量字段仅用于原始结果和本地整理审计。"""
import math
import re
from .recognition import CATEGORY_CATALOG


def category_fields(metadata):
    value = metadata.get("category_id")
    if type(value) is int and 0 <= value < len(CATEGORY_CATALOG):
        item = CATEGORY_CATALOG[value]
        return ("车辆" if value < 7 else "工事" if value < 11 else "人员", item["name"])
    kind = str(metadata.get("target_type") or metadata.get("category") or "其他").strip()
    match = re.fullmatch(r"(vehicle|building|solider|soldier|dsolider|dsoldier)([1-7])", kind.lower())
    if match:
        prefix, number = match.groups()
        number = int(number)
        offset, maximum = {"vehicle": (0, 7), "building": (7, 4), "solider": (11, 4),
                           "soldier": (11, 4), "dsolider": (15, 4), "dsoldier": (15, 4)}[prefix]
        if number <= maximum:
            return category_fields({"category_id": offset + number - 1})
    extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    model = str(extra.get("targetModel") or extra.get("target_model") or metadata.get("target_model") or kind)
    broad = {"车": "车辆", "人": "人员", "建筑物": "工事", "car": "车辆", "person": "人员"}.get(kind, kind)
    if broad not in ("车辆", "人员", "工事", "其他"):
        for item in CATEGORY_CATALOG:
            if kind == item["name"]:
                return category_fields({"category_id": item["id"]})
        broad = "其他"
    return broad, model


def quality_metadata(metadata):
    count = metadata.get("detection_count", 0)
    if type(count) is not int or not 0 <= count <= 18446744073709551615:
        count = 0
    return {"tracking_success": metadata.get("tracking_success") is True,
            "detection_count": count}


def quality_rank(quality, score):
    q = quality_metadata(quality)
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        score = -1.0
    return int(q["tracking_success"]), q["detection_count"], score
