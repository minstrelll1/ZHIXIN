"""大连南山坡赛前固定方案；运行时只校验并读取航线。"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import polygon_coverage as coverage


PROFILE = "dalian_nanshan"
RADIUS_M = 75.0
SPEED_MPS = 5.0
HOVER_SECONDS = 10.0
MAX_ASPECT_RATIO = 2.0
UAV_COUNT = 6
AREA_PATH = Path(__file__).with_name("dalian_nanshan_area.json")
PLAN_PATH = Path(__file__).with_name("dalian_nanshan_prepared.json")


def _source_digest():
    files = (Path(__file__), AREA_PATH, Path(coverage.__file__))
    return hashlib.sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in files)).hexdigest()


def _area():
    data = json.loads(AREA_PATH.read_text(encoding="utf-8"))
    if data.get("coordinate_system") != "WGS84" or data.get("coordinate_format") != "decimal_degrees":
        raise ValueError("大连场景需要已确认的 WGS84 十进制度坐标")
    boundary = data["boundary_lon_lat"]
    departure = data["departure_lon_lat"]
    if len(boundary) != 4 or len(departure) != 2:
        raise ValueError("大连场景必须包含四个边界点和一个固定起飞点")
    for longitude, latitude in [*boundary, departure]:
        if not (math.isfinite(longitude) and math.isfinite(latitude)
                and abs(longitude) <= 180 and abs(latitude) <= 90):
            raise ValueError("大连场景经纬度无效")
    latitude0, longitude0 = departure[1], departure[0]
    north, west = coverage._meters_per_degree(latitude0)
    points = [[(latitude - latitude0) * north, -(longitude - longitude0) * west]
              for longitude, latitude in boundary]
    projection = {"latitude": latitude0, "longitude": longitude0,
                  "north_m_per_degree": north, "west_m_per_degree": west,
                  "method": "WGS84_local_midlatitude"}
    return data, points, projection


def _route(region, depot):
    """子区内覆盖与连通；固定起飞点到子区的进出段按直线计入估时。"""
    points = coverage._scan_points(region, region, RADIUS_M)
    if not points:
        raise ValueError("大连固定方案存在没有侦察点的子区")
    depot_index = len(points)
    links = {}

    def link(i, j):
        if (i, j) not in links:
            start = depot if i == depot_index else points[i]
            end = depot if j == depot_index else points[j]
            path = [start, end] if depot_index in (i, j) else coverage._shortest_link(start, end, region)
            distance = sum(math.dist(a, b) for a, b in itertools.pairwise(path))
            links[i, j] = (distance, path)
            links[j, i] = (distance, path[::-1])
        return links[i, j]

    def route_length(order):
        return sum(link(i, j)[0] for i, j in itertools.pairwise([depot_index, *order, depot_index]))

    best = None
    starts = sorted(range(len(points)), key=lambda i: (link(depot_index, i)[0], i))
    starts = list(dict.fromkeys(starts[:min(5, len(starts))] + starts[-min(2, len(starts)):] ))
    for start in starts:
        order = [start]
        remaining = set(range(len(points))) - {start}
        while remaining:
            next_index = min(remaining, key=lambda index: (link(order[-1], index)[0], index))
            order.append(next_index)
            remaining.remove(next_index)
        distance = route_length(order)
        for _ in range(5):
            changed = False
            for left in range(len(order) - 1):
                for right in range(left + 1, len(order)):
                    candidate = order[:left] + order[left:right + 1][::-1] + order[right + 1:]
                    candidate_distance = route_length(candidate)
                    if candidate_distance < distance - 1e-6:
                        order, distance, changed = candidate, candidate_distance, True
            if not changed:
                break
        if best is None or distance < best[0]:
            best = (distance, order)

    distance, order = best
    scans = [list(points[index]) for index in order]
    path = [list(depot)]
    for i, j in itertools.pairwise([depot_index, *order, depot_index]):
        path.extend([list(point) for point in link(i, j)[1][1:]])
    covered = coverage.unary_union([
        coverage.Point(point).buffer(RADIUS_M - .02, quad_segs=32) for point in scans
    ])
    missed = region.difference(covered).area
    if missed > 1e-5 or not all(region.buffer(1e-6).covers(coverage.Point(point)) for point in scans):
        raise ValueError("大连固定方案的侦察覆盖或航点边界校验失败")
    return {
        "waypoints_m": scans, "scan_waypoints_m": copy.deepcopy(scans),
        "flight_path_m": path,
        "route_primitives": [
            {"kind": "line", "start": a, "end": b, "length_m": math.dist(a, b)}
            for a, b in itertools.pairwise(path)
        ],
        "route_distance_m": distance, "scan_count": len(scans),
        "travel_time_s": distance / SPEED_MPS,
        "hover_scan_time_s": len(scans) * HOVER_SECONDS,
        "mission_time_s": distance / SPEED_MPS + len(scans) * HOVER_SECONDS,
        "coverage_ratio": 1.0, "uncovered_area_m2": missed,
    }


def prepare_dalian_nanshan_plan():
    """只在赛前执行；优化六个紧凑分区的扫描与进出总机时。"""
    coverage._load_geometry()
    data, points, projection = _area()
    polygon = coverage.Polygon(points)
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError("大连边界无效或点序不正确")
    depot = (0.0, 0.0)
    candidates = coverage._partition_candidates(polygon, MAX_ASPECT_RATIO, uav_count=UAV_COUNT)
    if not candidates:
        raise ValueError("大连区域未找到六个紧凑连通的任务子区")
    route_cache = {}
    best = None
    for _, regions in candidates:
        routes = []
        for region in regions:
            key = region.wkb
            if key not in route_cache:
                route_cache[key] = _route(region, depot)
            routes.append(route_cache[key])
        cost = (sum(item["mission_time_s"] for item in routes),
                max(item["mission_time_s"] for item in routes),
                sum(region.length for region in regions))
        if best is None or cost < best[0]:
            best = (cost, regions, routes)
    cost, regions, routes = best
    joined = coverage.unary_union(regions)
    if (polygon.symmetric_difference(joined).area > 1e-5
            or sum(region.area for region in regions) - joined.area > 1e-5):
        raise ValueError("大连六个子区未能完整、无重叠地覆盖边界")

    planned = {}
    ordered = sorted(zip(regions, routes), key=lambda item: (-item[0].centroid.x, item[0].centroid.y))
    for uav_id, (region, route) in enumerate(ordered, 1):
        minx, miny, maxx, maxy = region.bounds
        task = dict(
            route,
            type="lawnmower_search", coordinate_frame="LOCAL_NORTH_WEST",
            sector=uav_id, polygon_m=[list(point) for point in list(region.exterior.coords)[:-1]],
            area_m2=region.area, required_area_m2=region.area,
            bounds_m={"x_min": minx, "x_max": maxx, "y_min": miny, "y_max": maxy},
            shape=coverage.shape_metrics(region),
            reconnaissance_radius_m=RADIUS_M, reconnaissance_mode="hover_scan",
            speed_mps=SPEED_MPS, hover_scan_seconds=HOVER_SECONDS,
            turn_radius_m=0.0,
            waypoint_actions=[
                {"index": index, "action": "hover_scan", "duration_s": HOVER_SECONDS}
                for index in range(route["scan_count"])
            ],
            waypoints_wgs84=[
                [round(projection["latitude"] + point[0] / projection["north_m_per_degree"], 10),
                 round(projection["longitude"] - point[1] / projection["west_m_per_degree"], 10)]
                for point in route["waypoints_m"]
            ],
        )
        planned[str(uav_id)] = {"uav_id": uav_id, "task": task}

    minx, miny, maxx, maxy = polygon.bounds
    # 北向下界包含边界外的固定起飞点，地图因此能同时显示任务区域与起飞点。
    display_minx, display_miny = min(minx, 0.0), min(miny, 0.0)
    display_maxx, display_maxy = max(maxx, 0.0), max(maxy, 0.0)
    duration_sum = sum(route["mission_time_s"] for route in routes)
    distance_sum = sum(route["route_distance_m"] for route in routes)
    scan_sum = sum(route["scan_count"] for route in routes)
    coverage_info = {
        "objective": "minimize_total_mission_time",
        "objective_scope": "sum_of_aircraft_reconnaissance_times_including_depot_transit",
        "algorithm": "compact_partition_beam_search_disk_cover_depot_route_2opt",
        "global_optimum_proven": False, "evaluated_partitions": len(candidates),
        "uav_count": UAV_COUNT, "reconnaissance_radius_m": RADIUS_M,
        "speed_mps": SPEED_MPS, "hover_scan_seconds": HOVER_SECONDS,
        "max_region_aspect_ratio": MAX_ASPECT_RATIO,
        "actual_max_aspect_ratio": max(coverage.shape_metrics(region)["aspect_ratio"] for region in regions),
        "total_scan_count": scan_sum, "total_distance_m": distance_sum,
        "total_mission_time_s": duration_sum, "maximum_completion_time_s": cost[1],
        "maximum_aircraft_distance_m": max(route["route_distance_m"] for route in routes),
        "area_m2": polygon.area, "required_area_m2": polygon.area,
        "excluded_area_m2": 0.0, "uncovered_area_m2": sum(route["uncovered_area_m2"] for route in routes),
        "coverage_ratio": 1.0, "coverage_margin_m": .02, "includes_transit": True,
        "projection": projection,
        "tracking_note": "六个紧凑分区仅减少交叉跟踪；跨区目标去重由任务执行系统处理",
    }
    return {
        "preview_only": True, "subject": "subject1", "mission_id": "coverage-preview",
        "controller_mode": "preview", "planned_uavs": planned, "uavs": {},
        "search_area": {
            "name": data["name"], "coordinate_mode": "gps", "coordinate_frame": "WGS84",
            "flight_profile": PROFILE, "departure_point": "fixed_dalian",
            "departure_point_m": [0.0, 0.0],
            "departure_point_wgs84": {"latitude": projection["latitude"],
                                       "longitude": projection["longitude"]},
            "departure_source": "用户指定的大连固定起飞点",
            "planned_departure_reference_only": False,
            "landing_mode": "onboard_home",
            "points": [[latitude, longitude] for longitude, latitude in data["boundary_lon_lat"]],
            "points_m": points,
            "origin_x_m": display_minx, "origin_y_m": display_miny,
            "width_m": display_maxy - display_miny,
            "height_m": display_maxx - display_minx,
            "polygon_width_m": maxy - miny, "polygon_height_m": maxx - minx,
            "gps_origin": {"latitude": projection["latitude"],
                           "longitude": projection["longitude"],
                           "source": "dalian_fixed_departure"},
            "gps_origins_by_uav": {
                str(uav_id): {"latitude": projection["latitude"],
                              "longitude": projection["longitude"],
                              "source": "dalian_fixed_departure"}
                for uav_id in range(1, UAV_COUNT + 1)
            },
            "turn_radius_m": 0.0, "coverage": coverage_info,
            "terrain": {"enabled": False, "source": "未提供大连地类边界",
                        "interpretation": "全区域均按需侦察；不套用原竞赛场地地类"},
            "terrain_layers_m": {},
        },
        "prepared_plan": {
            "id": hashlib.sha256((_source_digest() + PROFILE).encode("utf-8")).hexdigest()[:20],
            "parameters": {"radius": RADIUS_M, "speed": SPEED_MPS,
                           "hover": HOVER_SECONDS, "aspect": MAX_ASPECT_RATIO,
                           "terrain": False, "uav_count": UAV_COUNT},
            "source_digest": _source_digest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runtime_mode": "load_prepared_only", "runtime_recalculation": False,
            "flight_profile": PROFILE, "departure_point": "fixed_dalian",
        },
    }


def save_dalian_nanshan_plan(plan):
    payload = {"schema_version": 1, "source_digest": _source_digest(),
               "plan_sha256": coverage._plan_hash(plan), "plan": plan}
    temporary = PLAN_PATH.with_suffix(".tmp")
    temporary.write_text(coverage._canonical(payload), encoding="utf-8")
    temporary.replace(PLAN_PATH)
    return PLAN_PATH


def load_dalian_nanshan_plan(*, coordinate_mode="gps"):
    if str(coordinate_mode).strip().lower() != "gps":
        raise ValueError("大连南山坡外场固定使用 GPS 坐标系")
    try:
        payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        if (payload["schema_version"] != 1
                or payload["source_digest"] != _source_digest()
                or payload["plan_sha256"] != coverage._plan_hash(payload["plan"])):
            raise coverage.PlanNotPreparedError("大连固定方案校验失败，请赛前重新生成")
        plan = payload["plan"]
        if (plan["search_area"]["flight_profile"] != PROFILE
                or len(plan["planned_uavs"]) != UAV_COUNT
                or plan["prepared_plan"]["runtime_recalculation"] is not False):
            raise coverage.PlanNotPreparedError("大连固定方案结构不匹配，请赛前重新生成")
        return copy.deepcopy(plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise coverage.PlanNotPreparedError("没有匹配的大连固定方案，请赛前生成") from error
