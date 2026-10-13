"""分派时发布的任务子区协议；不包含飞行控制指令。"""
import copy
import math


def validate_task_region(region, uav_id):
    if not isinstance(region, dict) or region.get("schema_version") != 1 or region.get("uav_id") != uav_id:
        raise ValueError("任务子区域版本或无人机编号不一致")
    frame = region.get("coordinate_frame")
    order = "longitude_latitude" if frame == "WGS84" else "x_y"
    if frame not in ("WGS84", "ENU") or region.get("coordinate_order") != order:
        raise ValueError("任务子区域坐标系或坐标顺序无效")
    parts = region.get("polygons")
    if not isinstance(parts, list) or not parts:
        raise ValueError("任务子区域缺少边界")
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("holes"), list):
            raise ValueError("任务子区域孔洞格式无效")
        for ring in [part.get("outer"), *part["holes"]]:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("任务子区域边界须闭合且至少有三个顶点")
            for point in ring:
                if (not isinstance(point, list) or len(point) != 2
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                    raise ValueError("任务子区域顶点须为两个有限数值")
                if frame == "WGS84" and (abs(point[0]) > 180 or abs(point[1]) > 90):
                    raise ValueError("任务子区域经纬度越界")
    return region


def task_region_message(assignment):
    region = assignment.get("task", {}).get("task_region")
    if region is None:
        return None  # 兼容尚未下发子区的旧任务。
    result = copy.deepcopy(validate_task_region(region, assignment["uav_id"]))
    result.update(mission_id=assignment["mission_id"], assignment_checksum=assignment["assignment_checksum"],
                  relative_altitude_m=assignment["target_altitude_m"], altitude_frame="RELATIVE_TO_TAKEOFF")
    return result
