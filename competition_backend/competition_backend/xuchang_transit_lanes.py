"""许昌小场景固定进返场方向，仅改变几何规划，不改变飞行控制或消息协议。"""
from __future__ import annotations
import copy
import math

# 本地坐标为 [北, 西] 米。先过固定门点，再沿各机绕障通道进入任务区。
# 3、6没有本次专用通道，返回None，让原规划代码保持其既有路线。
LANES = {
    1: {"name": "north", "label": "北侧绕行", "anchors_m": [[70.0, -45.0], [130.0, -65.0]]},
    2: {"name": "east_then_south", "label": "东侧进入后南绕", "anchors_m": [[0.0, -50.0], [-90.0, -50.0]]},
    4: {"name": "northwest", "label": "西北通道", "anchors_m": [[60.0, 35.0]]},
    5: {"name": "southwest", "label": "西南通道", "anchors_m": [[-15.0, 25.0]]},
}
MERGE_RADIUS_M = 20.0


def build_recipient_routes(boundary, holes, home, waypoints_by_uav, recipient_uid):
    """返回本接收机进场及全机队逐点返航；固定门点内侧由调用方按实GPS重接。"""
    from shapely.geometry import LineString, Point, Polygon
    from .transit_routes import clearance_routes
    uid = int(recipient_uid)
    if uid not in LANES:
        return None
    lane = copy.deepcopy(LANES[uid])
    anchors = lane["anchors_m"]
    sources = [(int(source), index + 1, list(point))
               for source, points in sorted(waypoints_by_uav.items(), key=lambda item: int(item[0]))
               for index, point in enumerate(points)]
    if not sources or not any(source == uid for source, _, _ in sources):
        raise ValueError("许昌专用通道缺少接收机侦察航点")
    # 保留显式gate，重接时只替换home与gate之间的连接，不让最短路抹掉专用方向。
    prefix = [list(home)]
    for anchor in anchors:
        connection = clearance_routes(boundary, prefix[-1], [anchor], 5.0, holes=holes)[0]
        prefix.extend(connection[1:])
    if prefix[1] != anchors[0]:
        raise ValueError("许昌固定起点无法直接连接专用门点，请重新核验固定规划")
    tails = clearance_routes(boundary, anchors[-1], [point for _, _, point in sources], 5.0, holes=holes)
    entry, returns = None, []
    flyable = Polygon(boundary, holes=holes)
    for (source, index, point), tail in zip(sources, tails):
        outward = copy.deepcopy(prefix)
        for part in tail[1:]:
            if math.dist(outward[-1], part) > 1e-9:
                outward.append(list(part))
        if len(outward) < 2:
            outward.append(list(point))
        line = LineString(outward)
        if not flyable.buffer(1e-7).covers(line) or line.distance(flyable.boundary) < 5.0:
            raise ValueError("许昌专用进返场航线未满足5米边界间距")
        returning = list(reversed(outward))
        if returning[-2] != anchors[0]:
            raise ValueError("许昌专用返航路线缺少固定接近门点")
        if source == uid and index == 1:
            entry = outward
        returns.append({"source_uav_id": source, "waypoint_index": index, "path": returning})
    lane.update(schema_version=1, recipient_uav_id=uid,
                entry_gate_m=copy.deepcopy(anchors[0]), return_gate_m=copy.deepcopy(anchors[0]),
                home_merge_radius_m=MERGE_RADIUS_M,
                scope="entry_and_final_return_spatial_separation",
                collision_avoidance_guaranteed=False)
    return {"entry_path_m": entry, "fleet_return_paths_m": returns, "transit_lane": lane}


def apply_transit_lanes(plan):
    """反转1/4的现有扫描顺序；只更新固定进返场几何、时长和规划说明。"""
    from shapely.geometry import LineString, Point
    result = copy.deepcopy(plan)
    area = result["search_area"]
    for uid in ("1", "4"):
        task = result["planned_uavs"][uid]["task"]
        for name in ("waypoints_m", "scan_waypoints_m", "waypoints_wgs84"):
            task[name] = list(reversed(task[name]))
        task["waypoint_actions"] = [dict(action, index=index) for index, action in
                                    enumerate(reversed(task["waypoint_actions"]))]
    waypoints = {uid: item["task"]["waypoints_m"] for uid, item in result["planned_uavs"].items()}
    planned = {}
    for uid in LANES:
        task = result["planned_uavs"][str(uid)]["task"]
        routes = build_recipient_routes(area["points_m"], area["excluded_polygons_m"],
                                        area["departure_point_m"], waypoints, uid)
        last_return = next(item["path"] for item in routes["fleet_return_paths_m"]
                           if item["source_uav_id"] == uid and item["waypoint_index"] == len(task["waypoints_m"]))
        flight = [*routes["entry_path_m"], *task["waypoints_m"][1:], *last_return[1:]]
        length = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
        task.update(flight_path_m=flight,
                    route_primitives=[{"kind": "line", "start": a, "end": b, "length_m": math.dist(a, b)}
                                      for a, b in zip(flight, flight[1:])],
                    route_distance_m=length, travel_time_s=length / task["speed_mps"],
                    mission_time_s=length / task["speed_mps"] + task["hover_scan_time_s"],
                    transit_lane=copy.deepcopy(routes["transit_lane"]))
        planned[uid] = (routes["entry_path_m"], last_return)
    near_home = Point(area["departure_point_m"]).buffer(MERGE_RADIUS_M, quad_segs=256)
    pair_distances = {}
    for uid, first in planned.items():
        for other, second in planned.items():
            if uid >= other:
                continue
            distance = min(LineString(a).difference(near_home).distance(LineString(b).difference(near_home))
                           for a in first for b in second)
            pair_distances[str(uid) + "-" + str(other)] = distance
    tasks = [item["task"] for item in result["planned_uavs"].values()]
    info = area["coverage"]
    info["eastern_transfer"].update(uav4_kept_original_order=False,
                                    uav4_kept_original_waypoint_coordinates=True,
                                    uav4_scan_order_adjustment="reversed_for_northwest_entry")
    info.update(total_distance_m=sum(task["route_distance_m"] for task in tasks),
                total_mission_time_s=sum(task["mission_time_s"] for task in tasks),
                maximum_completion_time_s=max(task["mission_time_s"] for task in tasks),
                maximum_aircraft_distance_m=max(task["route_distance_m"] for task in tasks),
                dedicated_transit_lanes={
                    "uav_ids": list(LANES), "reversed_scan_order_uav_ids": [1, 4],
                    "unchanged_regions": True, "home_merge_radius_m": MERGE_RADIUS_M,
                    "reference_home_m": copy.deepcopy(area["departure_point_m"]),
                    "minimum_pair_distance_outside_merge_radius_m": pair_distances,
                    "evaluation_scope": "nominal_home_entry_and_final_return_xy_only",
                    "collision_avoidance_guaranteed": False,
                    "note": "固定起点的进场与最终返航平面几何；实际起降点重接、扫描或目标跟踪阶段不保证该间距"})
    return result
