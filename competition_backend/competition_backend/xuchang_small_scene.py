"""许昌试飞场地固定 GPS 规划，内部扣除区用于进返场绕行验证。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import polygon_coverage as coverage
from .clearance_plans import _safe_route


PROFILE = "xuchang_small"
DEPARTURE = "fixed_xuchang"
RADIUS_M = 45.0
SPEED_MPS = 5.0
HOVER_SECONDS = 10.0
CLEARANCE_M = 5.0
# 六个源子区按首次定义的顺序规划；只交换接收机编号，不交换高度配置。
SOURCE_REGION_UAV_IDS = (6, 2, 3, 1, 4, 5)
AREA_PATH = Path(__file__).with_name("xuchang_small_area.json")
PLAN_PATH = Path(__file__).with_name("xuchang_small_prepared.json")


def _source_digest():
    from . import clearance_plans
    files = (Path(__file__), AREA_PATH, Path(coverage.__file__), Path(clearance_plans.__file__))
    return hashlib.sha256(b"".join(path.read_bytes().replace(b"\r\n", b"\n") for path in files)).hexdigest()


def _area():
    data = json.loads(AREA_PATH.read_text(encoding="utf-8"))
    if data.get("coordinate_system") != "WGS84" or data.get("coordinate_format") != "decimal_degrees":
        raise ValueError("许昌场景必须使用 WGS84 十进制度")
    boundary = data["boundary_lon_lat"]
    excluded = data["excluded_lon_lat"]
    departure = data["departure_lon_lat"]
    if len(boundary) != 4 or len(excluded) < 3 or len(departure) != 2:
        raise ValueError("许昌场景需要四个外边界点、至少三个扣除区点和一个起飞点")
    if len(data.get("uav4_boundary_lon_lat", [])) != 4:
        raise ValueError("许昌 UAV4 固定子区需要四个经纬度点")
    if len(data.get("uav1_requested_boundary_lon_lat", [])) != 4:
        raise ValueError("许昌 UAV1 大致子区需要四个经纬度点")
    for lon, lat in [*boundary, *excluded, departure, *data["uav4_boundary_lon_lat"],
                     *data["uav1_requested_boundary_lon_lat"]]:
        if not (math.isfinite(lon) and math.isfinite(lat) and abs(lon) <= 180 and abs(lat) <= 90):
            raise ValueError("许昌场景经纬度无效")
    lat0, lon0 = departure[1], departure[0]
    north, west = coverage._meters_per_degree(lat0)
    project = lambda point: [(point[1] - lat0) * north, -(point[0] - lon0) * west]
    projection = {"latitude": lat0, "longitude": lon0,
                  "north_m_per_degree": north, "west_m_per_degree": west,
                  "method": "WGS84_local_midlatitude"}
    return data, [project(point) for point in boundary], [project(point) for point in excluded], projection


def _regions(flyable, hole, fixed_uav1, fixed_uav4, cut_fraction):
    """按最初的子区编号计算几何；接收机编号由 SOURCE_REGION_UAV_IDS 统一分配。"""
    from shapely.geometry import box
    from shapely.ops import unary_union

    for uid, region in ((1, fixed_uav1), (4, fixed_uav4)):
        if (not region.is_valid or region.is_empty or region.geom_type != "Polygon"
                or region.interiors or region.difference(flyable).area > 1e-5):
            raise ValueError("UAV{} 子区必须是扣除禁飞区后连通的可飞区域".format(uid))
    if fixed_uav1.intersection(fixed_uav4).area > 1e-5:
        raise ValueError("UAV1 子区不能与保留的 UAV4 子区重叠")
    joined = unary_union([hole, fixed_uav1])
    remaining = flyable.difference(unary_union([fixed_uav1, fixed_uav4]))
    minx, miny, maxx, maxy = flyable.bounds
    # X 向北、Y 向西。东西分界位于两个保留区之间，南北分界同时穿过两个内环，
    # 把剩余场地分成四个无孔洞的连通子区，避免填平禁飞区或遗漏细小边角。
    y_cut = (fixed_uav4.bounds[1] + joined.bounds[3]) / 2
    low_x = max(fixed_uav4.bounds[0], joined.bounds[0])
    high_x = min(fixed_uav4.bounds[2], joined.bounds[2])
    if joined.bounds[3] >= fixed_uav4.bounds[1] or low_x >= high_x:
        raise ValueError("固定子区的位置不支持当前四向分区，请离线重新划分")
    x_cut = low_x + cut_fraction * (high_x - low_x)
    regions = [fixed_uav1,
               remaining.intersection(box(minx - 1, miny - 1, x_cut, y_cut)),
               remaining.intersection(box(x_cut, miny - 1, maxx + 1, y_cut)),
               fixed_uav4,
               remaining.intersection(box(x_cut, y_cut, maxx + 1, maxy + 1)),
               remaining.intersection(box(minx - 1, y_cut, x_cut, maxy + 1))]
    if (any(region.geom_type != "Polygon" or region.is_empty or not region.is_valid or region.interiors
            for region in regions)
            or flyable.symmetric_difference(unary_union(regions)).area > 1e-5
            or any(a.intersection(b).area > 1e-5
                   for i, a in enumerate(regions) for b in regions[i + 1:])):
        raise ValueError("许昌六机分区未能无重叠、完整且连通地覆盖可飞区域")
    return regions


def _region_route(region, safe, boundary, hole_points):
    from shapely.geometry import LineString
    from .transit_routes import clearance_routes

    initial = coverage._scan_points(region, region, RADIUS_M)
    if not initial:
        raise ValueError("许昌子区缺少侦察航点")
    task_input = {"polygon_m": [list(point) for point in list(region.exterior.coords)[:-1]],
                  "waypoints_m": [list(point) for point in initial],
                  "reconnaissance_radius_m": RADIUS_M,
                  "speed_mps": SPEED_MPS, "hover_scan_seconds": HOVER_SECONDS}
    route = _safe_route(task_input, region, safe, [0.0, 0.0])
    scans = route["waypoints_m"]
    entry, to_last = clearance_routes(boundary, [0.0, 0.0],
                                     [scans[0], scans[-1]], CLEARANCE_M, holes=[hole_points])
    flight = [*entry, *scans[1:], *list(reversed(to_last))[1:]]
    if len(scans) > 1 and not safe.buffer(1e-7).covers(LineString(scans)):
        raise ValueError("许昌扫描航线不满足边界间距")
    length = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
    route.update(flight_path_m=flight, polygon_m=task_input["polygon_m"],
                 route_primitives=[{"kind": "line", "start": a, "end": b,
                                    "length_m": math.dist(a, b)} for a, b in zip(flight, flight[1:])],
                 route_distance_m=length, travel_time_s=length / SPEED_MPS,
                 mission_time_s=length / SPEED_MPS + len(scans) * HOVER_SECONDS)
    return route


def prepare_xuchang_small_plan():
    """离线计算六机固定航点；运行时不得重新求解。"""
    from shapely.geometry import LineString, Point, Polygon, mapping
    from shapely.ops import unary_union
    from .transit_routes import clearance_region

    coverage._load_geometry()
    data, boundary, hole_points, projection = _area()
    outer, hole = Polygon(boundary), Polygon(hole_points)
    if not outer.is_valid or not hole.is_valid or not outer.contains(hole):
        raise ValueError("许昌扣除区必须完全位于有效外边界内")
    flyable = Polygon(boundary, holes=[hole_points])
    if not flyable.is_valid or not flyable.contains(Point(0, 0)):
        raise ValueError("许昌固定起降点不在可飞区域")
    # 侦察航点比进返场路线多留 1 厘米，避免多边形编码的浮点舍入使边缘点失效。
    safe = clearance_region(boundary, CLEARANCE_M + .01, holes=[hole_points])
    def project_polygon(points):
        return Polygon([[(lat - projection["latitude"]) * projection["north_m_per_degree"],
                         -(lon - projection["longitude"]) * projection["west_m_per_degree"]]
                        for lon, lat in points])

    # 配置中的 UAV1/UAV4 是源子区名称；当前接收机编号在生成任务时统一映射。
    fixed_uav4 = project_polygon(data["uav4_boundary_lon_lat"])
    requested_uav1 = project_polygon(data["uav1_requested_boundary_lon_lat"])
    if not requested_uav1.is_valid:
        raise ValueError("UAV1 大致子区边界无效")
    # 禁飞区优先，UAV4 保持原边界；UAV1 仅取用户给定范围内的剩余可飞区域。
    fixed_uav1 = requested_uav1.intersection(flyable).difference(fixed_uav4)
    best, route_cache = None, {}
    # 离线比较 19 个南北分割位置；先缩短最晚完成用时，再缩短六机总航程。
    for step in range(1, 20):
        candidate_regions = _regions(flyable, hole, fixed_uav1, fixed_uav4, step / 20)
        candidate_routes = []
        for region in candidate_regions:
            if region.wkb not in route_cache:
                route_cache[region.wkb] = _region_route(region, safe, boundary, hole_points)
            candidate_routes.append(route_cache[region.wkb])
        score = (max(route["mission_time_s"] for route in candidate_routes),
                 sum(route["route_distance_m"] for route in candidate_routes),
                 sum(route["mission_time_s"] for route in candidate_routes))
        if best is None or score < best[0]:
            best = (score, candidate_regions, candidate_routes, step / 20)
    _, regions, selected_routes, split_fraction = best
    planned, routes = {}, []
    for uid, region, selected_route in zip(SOURCE_REGION_UAV_IDS, regions, selected_routes):
        route = copy.deepcopy(selected_route)
        scans, flight = route["waypoints_m"], route["flight_path_m"]
        if (not flyable.buffer(1e-7).covers(LineString(flight))
                or LineString(flight).distance(flyable.boundary) < CLEARANCE_M):
            raise ValueError("许昌扫描或进返场航线未满足 5 米间距")
        minx, miny, maxx, maxy = region.bounds
        task = dict(route, type="lawnmower_search", coordinate_frame="LOCAL_NORTH_WEST",
                    sector=uid,
                    area_m2=region.area, required_area_m2=region.area,
                    bounds_m={"x_min": minx, "x_max": maxx, "y_min": miny, "y_max": maxy},
                    shape=coverage.shape_metrics(region),
                    reconnaissance_radius_m=RADIUS_M, reconnaissance_mode="hover_scan",
                    speed_mps=SPEED_MPS, hover_scan_seconds=HOVER_SECONDS, turn_radius_m=0.0,
                    waypoints_wgs84=[[
                        round(projection["latitude"] + p[0] / projection["north_m_per_degree"], 10),
                        round(projection["longitude"] - p[1] / projection["west_m_per_degree"], 10)
                    ] for p in scans])
        planned[str(uid)] = {"uav_id": uid, "task": task}
        routes.append(task)
    covered = unary_union([Point(point).buffer(RADIUS_M - .02, quad_segs=32)
                           for task in routes for point in task["waypoints_m"]])
    if flyable.difference(covered).area > 1e-5:
        raise ValueError("许昌六机航点未完整覆盖扣除后的任务区域")
    minx, miny, maxx, maxy = outer.bounds
    minx, miny, maxx, maxy = min(minx, 0), min(miny, 0), max(maxx, 0), max(maxy, 0)
    times = [task["mission_time_s"] for task in routes]
    coverage_info = {
        "objective": "cover_flyable_area_with_five_meter_clearance_and_short_routes",
        "algorithm": "fixed_uav1_uav4_partition_search_safe_disk_cover_visibility_graph_multistart_2opt",
        "fixed_subregion_uav_ids": sorted([SOURCE_REGION_UAV_IDS[0], SOURCE_REGION_UAV_IDS[3]]), "partition_candidates": 19,
        "source_region_by_uav": {str(uid): index for index, uid in enumerate(SOURCE_REGION_UAV_IDS, 1)},
        "north_south_split_fraction": split_fraction,
        "uav{}_requested_area_m2".format(SOURCE_REGION_UAV_IDS[0]): requested_uav1.area,
        "uav{}_removed_area_m2".format(SOURCE_REGION_UAV_IDS[0]): requested_uav1.difference(fixed_uav1).area,
        "partition_objective": "minimize_maximum_completion_time_then_total_distance",
        "global_optimum_proven": False, "uav_count": 6,
        "reconnaissance_radius_m": RADIUS_M, "speed_mps": SPEED_MPS,
        "hover_scan_seconds": HOVER_SECONDS, "boundary_clearance_m": CLEARANCE_M,
        "total_scan_count": sum(task["scan_count"] for task in routes),
        "total_distance_m": sum(task["route_distance_m"] for task in routes),
        "total_mission_time_s": sum(times), "maximum_completion_time_s": max(times),
        "maximum_aircraft_distance_m": max(task["route_distance_m"] for task in routes),
        "area_m2": outer.area, "required_area_m2": flyable.area,
        "excluded_area_m2": hole.area, "uncovered_area_m2": flyable.difference(covered).area,
        "coverage_ratio": 1.0, "coverage_margin_m": .02,
        "includes_transit": True, "projection": projection,
        "tracking_note": "内部扣除区不侦察、不穿越；所有高度使用同一平面航线"
    }
    return {
        "preview_only": True, "subject": "subject1", "mission_id": "coverage-preview",
        "controller_mode": "preview", "planned_uavs": planned, "uavs": {},
        "search_area": {
            "name": data["name"], "coordinate_mode": "gps", "coordinate_frame": "WGS84",
            "flight_profile": PROFILE, "departure_point": DEPARTURE,
            "departure_point_m": [0.0, 0.0],
            "departure_point_wgs84": {"latitude": projection["latitude"], "longitude": projection["longitude"]},
            "departure_source": "用户指定的许昌固定起飞点",
            "landing_mode": "onboard_home",
            "points": [[lat, lon] for lon, lat in data["boundary_lon_lat"]],
            "points_m": boundary,
            "excluded_points": [[lat, lon] for lon, lat in data["excluded_lon_lat"]],
            "excluded_polygons_m": [hole_points],
            "requested_subregions_lon_lat": {
                str(SOURCE_REGION_UAV_IDS[0]): copy.deepcopy(data["uav1_requested_boundary_lon_lat"]),
                str(SOURCE_REGION_UAV_IDS[3]): copy.deepcopy(data["uav4_boundary_lon_lat"])},
            "fixed_subregions_lon_lat": {
                str(SOURCE_REGION_UAV_IDS[0]): [[projection["longitude"] - point[1] / projection["west_m_per_degree"],
                       projection["latitude"] + point[0] / projection["north_m_per_degree"]]
                      for point in list(fixed_uav1.exterior.coords)[:-1]],
                str(SOURCE_REGION_UAV_IDS[3]): copy.deepcopy(data["uav4_boundary_lon_lat"])},
            "origin_x_m": minx, "origin_y_m": miny,
            "width_m": maxy - miny, "height_m": maxx - minx,
            "polygon_width_m": outer.bounds[3] - outer.bounds[1],
            "polygon_height_m": outer.bounds[2] - outer.bounds[0],
            "gps_origin": {"latitude": projection["latitude"],
                           "longitude": projection["longitude"], "source": "xuchang_fixed_departure"},
            "gps_origins_by_uav": {str(uid): {"latitude": projection["latitude"],
                                                "longitude": projection["longitude"],
                                                "source": "xuchang_fixed_departure"} for uid in range(1, 7)},
            "turn_radius_m": 0.0, "coverage": coverage_info,
            "boundary_clearance_m": CLEARANCE_M,
            "terrain": {"enabled": False, "source": "许昌测试场地内部人工扣除区",
                        "interpretation": "仅扣除区免侦察且禁止规划航线穿越"},
            "terrain_layers_m": {"excluded": mapping(hole), "required": mapping(flyable)},
        },
        "prepared_plan": {
            "id": hashlib.sha256((_source_digest() + PROFILE).encode()).hexdigest()[:20],
            "parameters": {"radius": RADIUS_M, "speed": SPEED_MPS,
                           "hover": HOVER_SECONDS, "uav_count": 6, "boundary_clearance_m": CLEARANCE_M},
            "source_digest": _source_digest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runtime_mode": "load_prepared_only", "runtime_recalculation": False,
            "flight_profile": PROFILE, "departure_point": DEPARTURE,
        },
    }


def save_xuchang_small_plan(plan):
    payload = {"schema_version": 1, "source_digest": _source_digest(),
               "plan_sha256": coverage._plan_hash(plan), "plan": plan}
    temporary = PLAN_PATH.with_suffix(".tmp")
    temporary.write_text(coverage._canonical(payload), encoding="utf-8")
    temporary.replace(PLAN_PATH)
    return PLAN_PATH


def load_xuchang_small_plan(*, coordinate_mode="gps"):
    if str(coordinate_mode).strip().lower() != "gps":
        raise ValueError("许昌试飞场地固定使用 GPS 坐标系")
    try:
        payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        if (payload["schema_version"] != 1
                or payload["source_digest"] != _source_digest()
                or payload["plan_sha256"] != coverage._plan_hash(payload["plan"])):
            raise coverage.PlanNotPreparedError("许昌固定方案校验失败，请赛前重新生成")
        plan = payload["plan"]
        if (plan["search_area"]["flight_profile"] != PROFILE
                or len(plan["planned_uavs"]) != 6
                or plan["prepared_plan"]["runtime_recalculation"] is not False):
            raise coverage.PlanNotPreparedError("许昌固定方案结构不匹配，请赛前重新生成")
        return copy.deepcopy(plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise coverage.PlanNotPreparedError("没有匹配的许昌固定方案，请赛前生成") from error
