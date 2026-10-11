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
AREA_PATH = Path(__file__).with_name("xuchang_small_area.json")
PLAN_PATH = Path(__file__).with_name("xuchang_small_prepared.json")
PARTITION_REFERENCE_PATH = Path(__file__).with_name("xuchang_small_partition_reference.json")
EAST_TRANSFER_REFERENCE_PATH = Path(__file__).with_name("xuchang_small_east_transfer_reference.json")


def _source_digest():
    from . import clearance_plans
    files = (Path(__file__), AREA_PATH, PARTITION_REFERENCE_PATH, EAST_TRANSFER_REFERENCE_PATH,
             Path(coverage.__file__), Path(clearance_plans.__file__),
             Path(__file__).with_name("xuchang_transit_lanes.py"))
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


def _partition_reference(boundary, hole_points):
    """固定本轮修改前的几何，避免反复生成使“南移30米”等约束累积漂移。"""
    from shapely import set_precision
    from shapely.geometry import Polygon
    data = json.loads(PARTITION_REFERENCE_PATH.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or data.get("boundary_m") != boundary
            or data.get("excluded_polygons_m") != [hole_points]):
        raise ValueError("许昌分区参考与外边界或禁飞区不一致，须重新核验离线约束")
    return {int(uid): set_precision(Polygon(points), 1e-9)
            for uid, points in data["regions_m"].items()}, data


def _regions(flyable, reference, north_cap_m, west_limit_m, west_split_m):
    """六个连通块：2继承旧1，4位于6北侧，5限制南北界，其余区域完整补齐。"""
    from shapely import set_precision
    from shapely.geometry import box
    from shapely.ops import unary_union

    flyable = set_precision(flyable, 1e-9)
    fixed_uav2 = reference[1]
    north_uav5 = reference[5].bounds[2] - 30.0
    # 取旧1西南角的北坐标：删去南界微小斜角形成的数米宽横向尾巴，交由UAV3覆盖。
    south_uav5 = min(x for x, y in reference[1].exterior.coords
                     if y >= reference[1].bounds[3] - 1e-7)
    fixed_uav5 = flyable.intersection(box(south_uav5, -110, north_uav5, 1000)).difference(fixed_uav2)
    remaining = flyable.difference(unary_union([fixed_uav2, fixed_uav5]))
    uav6 = remaining.intersection(box(north_uav5, -west_limit_m, west_split_m, 1000))
    uav4 = (remaining.difference(uav6).intersection(box(west_split_m, -1000, 1000, 1000))
            .intersection(unary_union([box(-1000, -west_limit_m, 1000, 1000),
                                       box(north_cap_m, -1000, 1000, 1000)])))
    uav1 = (remaining.difference(unary_union([uav4, uav6]))
            .intersection(box(reference[3].bounds[0], reference[1].bounds[1], 1000, 1000)))
    if uav1.geom_type == "MultiPolygon":
        # 东边旧子区斜边的孤立小角交给相邻UAV3，不能成为UAV1独立小岛。
        uav1 = max(uav1.geoms, key=lambda region: region.area)
    uav3 = remaining.difference(unary_union([uav1, uav4, uav6]))
    regions = [uav1, fixed_uav2, uav3, uav4, fixed_uav5, uav6]
    if (any(region.geom_type != "Polygon" or region.is_empty or not region.is_valid
            or region.interiors or region.area < 7500 for region in regions)
            or flyable.symmetric_difference(unary_union(regions)).area > 1e-5
            or any(a.intersection(b).area > 1e-5
                   for i, a in enumerate(regions) for b in regions[i + 1:])):
        raise ValueError("许昌六机分区未能无重叠、完整且连通地覆盖可飞区域")
    metrics = [coverage.shape_metrics(region) for region in regions]
    if (max(item["aspect_ratio"] for item in metrics) > 3.0
            or min(item["short_side_m"] for item in metrics) < 70
            or min(item["rectangle_fill_ratio"] for item in metrics) < .40
            or metrics[3]["rectangle_fill_ratio"] < .65
            or metrics[5]["rectangle_fill_ratio"] < .85):
        raise ValueError("许昌候选子区过于狭长或零碎，已排除")
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


def _prepare_partition_plan():
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
    reference, reference_data = _partition_reference(boundary, hole_points)
    best, route_cache, candidate_count = None, {}, 0
    # 先排除狭长、孔洞、孤岛和遗漏，再按实际进返场+扫描用时选择固定方案。
    for north_cap in range(140, 221, 10):
        for west_limit in range(65, 111, 5):
            for west_split in range(80, 151, 10):
                try:
                    candidate_regions = _regions(flyable, reference, north_cap, west_limit, west_split)
                except ValueError:
                    continue
                candidate_count += 1
                candidate_routes = []
                for region in candidate_regions:
                    if region.wkb not in route_cache:
                        route_cache[region.wkb] = _region_route(region, safe, boundary, hole_points)
                    candidate_routes.append(route_cache[region.wkb])
                score = (max(route["mission_time_s"] for route in candidate_routes),
                         sum(route["route_distance_m"] for route in candidate_routes),
                         sum(route["mission_time_s"] for route in candidate_routes))
                if best is None or score < best[0]:
                    best = (score, candidate_regions, candidate_routes,
                            {"north_cap_m": north_cap, "west_limit_m": west_limit,
                             "west_split_m": west_split})
    if best is None:
        raise ValueError("没有满足许昌分区范围与紧凑度约束的候选方案")
    _, regions, selected_routes, partition_cuts = best
    planned, routes = {}, []
    for uid, region, selected_route in zip(range(1, 7), regions, selected_routes):
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
        "algorithm": "constrained_compact_partition_safe_disk_cover_visibility_graph_multistart_2opt",
        "fixed_subregion_uav_ids": [2], "partition_candidates": candidate_count,
        "partition_reference_sha256": reference_data["source_plan_sha256"],
        "partition_cuts_m": partition_cuts,
        "partition_constraints": {
            "uav2_equals_previous_uav1": True,
            "uav1_east_limit_m": -reference[1].bounds[1],
            "uav5_north_limit_m": reference[5].bounds[2] - 30.0,
            "uav5_south_limit_m": reference[1].bounds[0],
            "uav4_north_of_uav6": True,
            "maximum_aspect_ratio": 3.0, "minimum_short_side_m": 70.0},
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
            "requested_subregions_lon_lat": {},
            "fixed_subregions_lon_lat": {
                "2": [[projection["longitude"] - point[1] / projection["west_m_per_degree"],
                       projection["latitude"] + point[0] / projection["north_m_per_degree"]]
                      for point in list(regions[1].exterior.coords)[:-1]]},
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


def _prepare_east_transfer_plan():
    """仅转移UAV4东侧六点及对应区域；其他四机和UAV4剩余点不重新优化。"""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    from .transit_routes import clearance_region, clearance_routes

    coverage._load_geometry()
    reference = json.loads(EAST_TRANSFER_REFERENCE_PATH.read_text(encoding="utf-8"))
    base = reference["base_plan"]
    if reference.get("schema_version") != 1 or reference.get("base_plan_sha256") != coverage._plan_hash(base):
        raise ValueError("许昌局部转区基准校验失败，请核对原固定方案")
    data, boundary, hole_points, projection = _area()
    area = base["search_area"]
    if area["points_m"] != boundary or area["excluded_polygons_m"] != [hole_points]:
        raise ValueError("许昌局部转区基准与现有场地边界或禁飞区不一致")
    plan = copy.deepcopy(base)
    task4 = base["planned_uavs"]["4"]["task"]
    points4 = task4["waypoints_m"]
    indices = reference["transferred_waypoint_indices_1based"]
    actual = sorted(i + 1 for i, point in sorted(enumerate(points4), key=lambda pair: pair[1][1])[:6])
    if indices != actual or len(points4) != 16:
        raise ValueError("许昌局部调整必须对应基准UAV4最东侧六个航点")
    moved_points = [copy.deepcopy(point) for i, point in enumerate(points4, 1) if i in indices]
    kept_points = [copy.deepcopy(point) for i, point in enumerate(points4, 1) if i not in indices]
    cut = reference["cut"]
    def west_limit(north):
        return -(cut["east_anchor_m"] + cut["east_change_per_north_m"] * (north - cut["north_anchor_m"]))
    west_half = Polygon([[-1000, 1000], [-1000, west_limit(-1000)],
                         [1000, west_limit(1000)], [1000, 1000]])
    original4 = Polygon(task4["polygon_m"])
    region4 = original4.intersection(west_half)
    transferred = original4.difference(region4)
    region3 = Polygon(base["planned_uavs"]["3"]["task"]["polygon_m"]).union(transferred)
    for region in (region3, region4):
        if region.geom_type != "Polygon" or not region.is_valid or region.interiors:
            raise ValueError("许昌局部转区后必须保留连通且无孔洞的子区域")
    if any(not region4.buffer(1e-7).covers(Point(point)) for point in kept_points):
        raise ValueError("许昌UAV4剩余十点必须保持在西侧子区内")
    if any(not transferred.buffer(1e-7).covers(Point(point)) for point in moved_points):
        raise ValueError("许昌UAV4东侧六点必须全部转入UAV3子区")
    flyable = Polygon(boundary, holes=[hole_points])
    safe = clearance_region(boundary, CLEARANCE_M + .01, holes=[hole_points])
    task3_input = dict(base["planned_uavs"]["3"]["task"],
                       polygon_m=[list(p) for p in list(region3.exterior.coords)[:-1]],
                       waypoints_m=[*base["planned_uavs"]["3"]["task"]["waypoints_m"], *moved_points])
    route3 = _safe_route(task3_input, region3, safe, [0., 0.])
    # 西侧十点保留原坐标与顺序，只更新进返场、时长和子区边界。
    route4 = dict(copy.deepcopy(task4), waypoints_m=kept_points,
                  scan_waypoints_m=copy.deepcopy(kept_points), scan_count=len(kept_points),
                  original_scan_count=len(kept_points), coverage_added_scan_count=0,
                  turning_point_count=0, hover_scan_time_s=len(kept_points) * HOVER_SECONDS,
                  waypoint_actions=[{"index": i, "action": "hover_scan", "duration_s": HOVER_SECONDS}
                                    for i in range(len(kept_points))])
    for uid, region, route in ((3, region3, route3), (4, region4, route4)):
        scans = route["waypoints_m"]
        entry, to_last = clearance_routes(boundary, [0., 0.], [scans[0], scans[-1]], CLEARANCE_M, holes=[hole_points])
        flight = [*entry, *scans[1:], *list(reversed(to_last))[1:]]
        line = LineString(flight)
        if (not flyable.buffer(1e-7).covers(line) or line.distance(flyable.boundary) < CLEARANCE_M):
            raise ValueError("许昌局部转区航段未满足5米间距")
        covered = unary_union([Point(point).buffer(RADIUS_M - .02, quad_segs=32) for point in scans])
        if region.difference(covered).area > 1e-5:
            raise ValueError("许昌局部转区航点未完整覆盖自身子区域")
        length = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
        minx, miny, maxx, maxy = region.bounds
        task = dict(copy.deepcopy(base["planned_uavs"][str(uid)]["task"]), **route)
        task.update(polygon_m=[list(p) for p in list(region.exterior.coords)[:-1]],
                    area_m2=region.area, required_area_m2=region.area,
                    bounds_m={"x_min": minx, "x_max": maxx, "y_min": miny, "y_max": maxy},
                    shape=coverage.shape_metrics(region), flight_path_m=flight,
                    route_primitives=[{"kind": "line", "start": a, "end": b, "length_m": math.dist(a, b)}
                                      for a, b in zip(flight, flight[1:])],
                    route_distance_m=length, travel_time_s=length / SPEED_MPS,
                    mission_time_s=length / SPEED_MPS + len(scans) * HOVER_SECONDS,
                    waypoints_wgs84=[[
                        round(projection["latitude"] + point[0] / projection["north_m_per_degree"], 10),
                        round(projection["longitude"] - point[1] / projection["west_m_per_degree"], 10)
                    ] for point in scans])
        plan["planned_uavs"][str(uid)]["task"] = task
    tasks = [plan["planned_uavs"][str(uid)]["task"] for uid in range(1, 7)]
    regions = [Polygon(task["polygon_m"]) for task in tasks]
    if (flyable.symmetric_difference(unary_union(regions)).area > 1e-5
            or any(a.intersection(b).area > 1e-5 for i, a in enumerate(regions) for b in regions[i + 1:])):
        raise ValueError("许昌局部转区发生遗漏或重叠")
    info = plan["search_area"]["coverage"]
    info.update(objective="transfer_eastern_six_points_keep_other_aircraft_unchanged",
                algorithm="fixed_reference_diagonal_cut_safe_disk_cover_visibility_graph_multistart_2opt",
                base_partition_candidates=info["partition_candidates"], partition_candidates=1,
                partition_objective="reduce_uav4_workload_then_cover_uav3_with_safe_short_route",
                fixed_subregion_uav_ids=[1, 2, 5, 6],
                total_scan_count=sum(task["scan_count"] for task in tasks),
                total_distance_m=sum(task["route_distance_m"] for task in tasks),
                total_mission_time_s=sum(task["mission_time_s"] for task in tasks),
                maximum_completion_time_s=max(task["mission_time_s"] for task in tasks),
                maximum_aircraft_distance_m=max(task["route_distance_m"] for task in tasks),
                eastern_transfer={"source_uav_id": 4, "destination_uav_id": 3,
                                  "base_plan_sha256": reference["base_plan_sha256"],
                                  "transferred_waypoint_indices_1based": indices,
                                  "transferred_area_m2": transferred.area, "cut": cut,
                                  "uav4_kept_original_order": True,
                                  "uav3_added_coverage_points": route3["coverage_added_scan_count"]})
    plan["prepared_plan"].update(
        id=hashlib.sha256((_source_digest() + PROFILE).encode()).hexdigest()[:20],
        source_digest=_source_digest(), generated_at=datetime.now(timezone.utc).isoformat())
    return plan


def prepare_xuchang_small_plan():
    """许昌专用进返场方向：只调整固定路线与1/4点序，不改变高度或控制流程。"""
    from .xuchang_transit_lanes import apply_transit_lanes
    plan = _prepare_east_transfer_plan()
    # 从不变的参考方案交换一次，重复离线生成不会再次交换回来。
    planned = plan["planned_uavs"]
    planned["2"], planned["6"] = planned["6"], planned["2"]
    for uid in ("2", "6"):
        planned[uid]["uav_id"] = int(uid)
        planned[uid]["task"]["sector"] = int(uid)
    area = plan["search_area"]
    for field in ("fixed_subregions_lon_lat", "requested_subregions_lon_lat"):
        area[field] = {str({2: 6, 6: 2}.get(int(uid), int(uid))): points
                       for uid, points in area.get(field, {}).items()}
    info = area["coverage"]
    constraints = info["partition_constraints"]
    constraints["uav6_equals_previous_uav1"] = constraints.pop("uav2_equals_previous_uav1")
    constraints["uav4_north_of_uav2"] = constraints.pop("uav4_north_of_uav6")
    info["assignment_swap"] = {"uav_ids": [2, 6], "height_by_uav_unchanged": True,
                               "transit_lanes_follow_regions": True}
    return apply_transit_lanes(plan)


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
