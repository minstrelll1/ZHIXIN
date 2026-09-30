"""赛前预计算操场中央起降参考的六机悬停覆盖方案。

比赛期间只读取已保存、已校验的方案。球场中心来自旧卫星影像估算，
不是 GNSS 实测起降坪；各机实际返航点仍以机载记录的独立 home 为准。
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from pathlib import Path

from . import polygon_coverage as coverage


# 用户在 2026-09-28 指认的边界内红绿球场，旧 Esri 影像估算值。
STADIUM_CENTER_WGS84 = {"latitude": 33.86446, "longitude": 113.70563}
DEPARTURE_POINT = "stadium_center"


def _source_digest():
    return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _profile_parameters(base_plan, flight_profile, reconnaissance_radius_m, speed_mps):
    profile = str(flight_profile or "competition").strip().lower()
    extents = {"lab": 3.0, "outdoor5": 5.0, "lab10": 10.0}
    params = base_plan["prepared_plan"]["parameters"]
    if profile == "competition":
        return {"flight_profile": profile, "radius_m": float(params["radius"]),
                "speed_mps": float(params["speed"]), "source_radius_m": float(params["radius"]),
                "source_speed_mps": float(params["speed"]), "scale": 1.0}
    if profile not in extents:
        raise ValueError("未知的操场中央规划场景")
    radius = float(1.0 if reconnaissance_radius_m is None else reconnaissance_radius_m)
    default_speed = 0.5 if profile == "lab10" else 0.2
    speed = float(default_speed if speed_mps is None else speed_mps)
    if not (math.isfinite(radius) and radius > 0 and math.isfinite(speed) and speed > 0):
        raise ValueError("缩小场景侦察半径和航速须为正数")
    area = base_plan["search_area"]
    scale = extents[profile] / max(float(area["width_m"]), float(area["height_m"]))
    return {"flight_profile": profile, "radius_m": radius, "speed_mps": speed,
            "source_radius_m": radius / scale, "source_speed_mps": speed / scale,
            "scale": scale}


def _cache_path(base_plan, scenario):
    key = coverage._plan_hash(base_plan)
    if base_plan["prepared_plan"]["parameters"] == dict(
        radius=75.0, speed=5.0, hover=10.0, aspect=2.0, edge=5.0,
        terrain=True, uav_count=6,
    ):
        profile = scenario["flight_profile"]
        if profile == "competition" or (scenario["radius_m"] == 1.0 and scenario["speed_mps"] ==
                                        (0.5 if profile == "lab10" else 0.2)):
            suffix = "" if profile == "competition" else f"_{profile}"
            return Path(__file__).with_name(f"competition_coverage_stadium_center{suffix}.json")
    scenario_key = hashlib.sha256(coverage._canonical({"base": key, **scenario}).encode("utf-8")).hexdigest()[:24]
    return Path(__file__).parent / "coverage_plans" / f"{scenario_key}_stadium_center.json"


def _stadium_local(plan):
    projection = plan["search_area"]["coverage"]["projection"]
    return [
        (STADIUM_CENTER_WGS84["latitude"] - projection["latitude"])
        * projection["north_m_per_degree"],
        -(STADIUM_CENTER_WGS84["longitude"] - projection["longitude"])
        * projection["west_m_per_degree"],
    ]


def _candidate_route(region, target, polygon, depot, radius, speed, hover):
    """固定扫描覆盖后，改善从共同参考点出发并返回的闭合航线。"""
    points = coverage._scan_points(region, target, radius)
    if not points:
        return {
            "waypoints_m": [], "scan_waypoints_m": [], "flight_path_m": [],
            "route_primitives": [], "route_distance_m": 0.0, "scan_count": 0,
            "travel_time_s": 0.0, "hover_scan_time_s": 0.0,
            "mission_time_s": 0.0, "coverage_ratio": 1.0,
            "uncovered_area_m2": 0.0,
        }
    cache = {}
    depot_index = len(points)

    def link(i, j):
        if (i, j) not in cache:
            source = depot if i == depot_index else points[i]
            destination = depot if j == depot_index else points[j]
            # 扫描航段留在本子区；跨区进出共用起降点可通过整个比赛区。
            allowed = polygon if depot_index in (i, j) else region
            path = coverage._shortest_link(source, destination, allowed)
            distance = sum(math.dist(a, b) for a, b in itertools.pairwise(path))
            cache[i, j] = (distance, path)
            cache[j, i] = (distance, path[::-1])
        return cache[i, j]

    def length(order):
        chain = [depot_index, *order, depot_index]
        return sum(link(i, j)[0] for i, j in itertools.pairwise(chain))

    best = None
    starts = sorted(range(len(points)), key=lambda index: (link(depot_index, index)[0], index))
    # 近端启程通常最短，加入末端候选避免凹区的最近邻陷阱。
    starts = starts[:min(5, len(starts))] + starts[-min(2, len(starts)):]
    for start in dict.fromkeys(starts):
        order = [start]
        remaining = set(range(len(points))) - {start}
        while remaining:
            next_index = min(remaining, key=lambda index: (link(order[-1], index)[0], index))
            order.append(next_index)
            remaining.remove(next_index)
        distance = length(order)
        for _ in range(5):
            changed = False
            for left in range(len(order) - 1):
                for right in range(left + 1, len(order)):
                    candidate = order[:left] + order[left:right + 1][::-1] + order[right + 1:]
                    candidate_distance = length(candidate)
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
    coverage._load_geometry()
    covered = coverage.unary_union([
        coverage.Point(point).buffer(radius - 0.02, quad_segs=32) for point in scans
    ])
    missed = target.difference(covered).area
    if missed > 1e-5 or not all(region.buffer(1e-6).covers(coverage.Point(point)) for point in scans):
        raise ValueError("操场中央方案扫描覆盖或航点边界校验失败")
    return {
        "waypoints_m": scans,
        "scan_waypoints_m": copy.deepcopy(scans),
        "flight_path_m": path,
        "route_primitives": [
            {"kind": "line", "start": a, "end": b, "length_m": math.dist(a, b)}
            for a, b in itertools.pairwise(path)
        ],
        "route_distance_m": distance,
        "scan_count": len(scans),
        "travel_time_s": distance / speed,
        "hover_scan_time_s": len(scans) * hover,
        "mission_time_s": distance / speed + len(scans) * hover,
        "coverage_ratio": 1.0,
        "uncovered_area_m2": missed,
    }


def _power_cells(polygon, seeds, weights):
    """按带权距离划分连通紧凑的六区，允许远处区域承担较少扫描量。"""
    cells = []
    span = max(polygon.bounds[2] - polygon.bounds[0], polygon.bounds[3] - polygon.bounds[1])
    reach = max(10000.0, span * 20.0)
    for i, (sx, sy) in enumerate(seeds):
        cell = polygon
        for j, (tx, ty) in enumerate(seeds):
            if i == j:
                continue
            nx, ny = tx - sx, ty - sy
            normal_squared = nx * nx + ny * ny
            normal_length = math.sqrt(normal_squared)
            if normal_length < 1e-6:
                return None
            threshold = (tx * tx + ty * ty - sx * sx - sy * sy + weights[i] - weights[j]) / 2
            px, py = nx * threshold / normal_squared, ny * threshold / normal_squared
            ux, uy = nx / normal_length, ny / normal_length
            vx, vy = -uy, ux
            half_plane = coverage.Polygon([
                (px + vx * reach, py + vy * reach),
                (px - vx * reach, py - vy * reach),
                (px - vx * reach - ux * reach, py - vy * reach - uy * reach),
                (px + vx * reach - ux * reach, py + vy * reach - uy * reach),
            ])
            cell = cell.intersection(half_plane)
        if cell.geom_type != "Polygon" or cell.area < polygon.area * 0.02:
            return None
        cells.append(cell)
    return cells


def prepare_stadium_center_plan(base_plan, flight_profile="competition",
                                reconnaissance_radius_m=None, speed_mps=None):
    """仅由赛前命令调用；均衡各机完成时间，随后尽量缩短总航程。"""
    coverage._load_geometry()
    params = base_plan["prepared_plan"]["parameters"]
    scenario = _profile_parameters(base_plan, flight_profile, reconnaissance_radius_m, speed_mps)
    radius = scenario["source_radius_m"]
    speed = scenario["source_speed_mps"]
    uav_count = int(params["uav_count"])
    preset = coverage.area_preset()
    points, projection = coverage.local_projection(preset["points"])
    polygon = coverage.Polygon(points)
    depot = _stadium_local(base_plan)
    if not polygon.covers(coverage.Point(depot)):
        raise ValueError("旧影像估算的球场中心不在比赛区域内")
    required, terrain, layers = coverage._terrain(
        polygon, projection, bool(params["terrain"]), float(params["edge"]),
    )
    candidates = []
    for heading in (0, -6, 6, 30, 60):
        rotated = coverage.rotate(polygon, -heading, origin=(0, 0))
        for _, regions in coverage._partition_candidates(
            rotated, float(params["aspect"]), uav_count=uav_count,
        ):
            candidates.append((heading, [coverage.rotate(part, heading, origin=(0, 0)) for part in regions]))
    if not candidates:
        raise ValueError("没有满足区域形状限制的操场中央候选分区")
    route_cache = {}
    best = None
    for heading, regions in candidates:
        routes = []
        for region in regions:
            key = region.wkb
            if key not in route_cache:
                route_cache[key] = _candidate_route(
                    region, required.intersection(region), polygon, depot,
                    radius, speed, float(params["hover"]),
                )
            routes.append(route_cache[key])
        times = [route["mission_time_s"] for route in routes]
        distances = [route["route_distance_m"] for route in routes]
        # 执行时间由扫描次数与航程共同决定；先追求六机均衡，等效候选优先总航程。
        score = (max(times) - min(times), sum(distances), max(times), sum(region.length for region in regions))
        if best is None or score < best[0]:
            best = (score, heading, regions, routes)
    # 紧凑候选偏向等面积；中央起降时远区的往返耗时不同。以最优候选的
    # 六个质心为种子，离线移动带权分界，在保持连通与长宽比限制下均衡时间。
    initial_regions = best[2]
    best_method = "compact_candidate"
    seeds = [(region.centroid.x, region.centroid.y) for region in initial_regions]
    weights = [0.0] * uav_count
    for iteration in range(28):
        regions = _power_cells(polygon, seeds, weights)
        if regions is None:
            break
        if any(coverage.shape_metrics(region)["aspect_ratio"] > float(params["aspect"]) + 1e-8
               for region in regions):
            break
        routes = []
        for region in regions:
            key = region.wkb
            if key not in route_cache:
                route_cache[key] = _candidate_route(
                    region, required.intersection(region), polygon, depot,
                    radius, speed, float(params["hover"]),
                )
            routes.append(route_cache[key])
        times = [route["mission_time_s"] for route in routes]
        distances = [route["route_distance_m"] for route in routes]
        score = (max(times) - min(times), sum(distances), max(times), sum(region.length for region in regions))
        if score < best[0]:
            best = (score, best[1], regions, routes)
            best_method = "weighted_power_diagram"
        mean_time = sum(times) / len(times)
        gain = 120.0 if iteration < 4 else 60.0 if iteration < 8 else 30.0 if iteration < 12 else 12.0
        weights = [weight - gain * (time - mean_time) for weight, time in zip(weights, times)]
    score, heading, regions, routes = best
    joined = coverage.unary_union(regions)
    if (polygon.symmetric_difference(joined).area > 1e-5
            or sum(region.area for region in regions) - joined.area > 1e-5):
        raise ValueError("操场中央六区完整性校验失败")
    result = copy.deepcopy(base_plan)
    tasks = {}
    ordered = sorted(zip(regions, routes), key=lambda item: (-item[0].centroid.x, item[0].centroid.y))
    for uav_id, (region, route) in enumerate(ordered, 1):
        minx, miny, maxx, maxy = region.bounds
        task = dict(
            route,
            type="lawnmower_search",
            coordinate_frame="LOCAL_NORTH_WEST",
            sector=uav_id,
            polygon_m=list(region.exterior.coords)[:-1],
            area_m2=region.area,
            required_area_m2=region.intersection(required).area,
            bounds_m={"x_min": minx, "x_max": maxx, "y_min": miny, "y_max": maxy},
            shape=coverage.shape_metrics(region),
            reconnaissance_radius_m=radius,
            reconnaissance_mode="hover_scan",
            speed_mps=speed,
            hover_scan_seconds=float(params["hover"]),
            turn_radius_m=0.0,
            waypoint_actions=[
                {"index": index, "action": "hover_scan", "duration_s": float(params["hover"])}
                for index in range(len(route["waypoints_m"]))
            ],
            waypoints_wgs84=[
                [
                    projection["latitude"] + point[0] / projection["north_m_per_degree"],
                    projection["longitude"] - point[1] / projection["west_m_per_degree"],
                ]
                for point in route["waypoints_m"]
            ],
        )
        tasks[str(uav_id)] = {"uav_id": uav_id, "task": task}
    area = result["search_area"]
    area["departure_point"] = DEPARTURE_POINT
    area["departure_point_m"] = list(depot)
    area["departure_point_wgs84"] = dict(STADIUM_CENTER_WGS84)
    area["departure_source"] = "2017 Esri 历史卫星影像估算；非 GNSS 实测起降点"
    area["landing_mode"] = "onboard_home"
    area["planned_departure_reference_only"] = True
    max_time = max(route["mission_time_s"] for route in routes)
    min_time = min(route["mission_time_s"] for route in routes)
    area["coverage"].update({
        "objective": "balance_six_completion_times_then_minimize_total_route_distance",
        "objective_scope": "scan_plus_transit_from_stadium_reference_and_return",
        "algorithm": "compact_partition_candidates_greedy_disk_cover_closed_route_2opt",
        "partition_method": best_method,
        "global_optimum_proven": False,
        "evaluated_partitions": len(candidates),
        "partition_heading_deg": heading,
        "reconnaissance_radius_m": radius,
        "speed_mps": speed,
        "total_mission_time_s": sum(route["mission_time_s"] for route in routes),
        "maximum_completion_time_s": max_time,
        "minimum_completion_time_s": min_time,
        "completion_time_spread_s": max_time - min_time,
        "maximum_aircraft_distance_m": max(route["route_distance_m"] for route in routes),
        "total_distance_m": sum(route["route_distance_m"] for route in routes),
        "total_scan_count": sum(route["scan_count"] for route in routes),
        "uncovered_area_m2": sum(route["uncovered_area_m2"] for route in routes),
        "actual_max_aspect_ratio": max(coverage.shape_metrics(region)["aspect_ratio"] for region in regions),
        "internal_boundary_length_m": (sum(region.length for region in regions) - polygon.length) / 2,
        "includes_transit": True,
        "departure_reference_note": "往返距离按球场中心参考点估计；实飞须以各机独立 home 为起降点并核对间隔",
    })
    result["planned_uavs"] = tasks
    result["prepared_plan"] = dict(base_plan["prepared_plan"])
    result["prepared_plan"]["id"] = hashlib.sha256(
        (base_plan["prepared_plan"]["id"] + DEPARTURE_POINT
         + coverage._canonical(scenario) + _source_digest()).encode("utf-8")
    ).hexdigest()[:20]
    result["prepared_plan"]["parameters"] = dict(params, departure_point=DEPARTURE_POINT)
    result["prepared_plan"]["departure_point"] = DEPARTURE_POINT
    result["prepared_plan"]["departure_source"] = area["departure_source"]
    result["prepared_plan"]["optimized_scenario"] = scenario
    result["prepared_plan"]["runtime_recalculation"] = False
    return result


def save_prepared_stadium_plan(base_plan, plan, flight_profile="competition",
                               reconnaissance_radius_m=None, speed_mps=None):
    scenario = _profile_parameters(base_plan, flight_profile, reconnaissance_radius_m, speed_mps)
    path = _cache_path(base_plan, scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "base_plan_sha256": coverage._plan_hash(base_plan),
        "planner_sha256": _source_digest(),
        "scenario": scenario,
        "plan_sha256": coverage._plan_hash(plan),
        "plan": plan,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(coverage._canonical(data), encoding="utf-8")
    temporary.replace(path)
    return path


def load_prepared_stadium_plan(base_plan, flight_profile="competition",
                               reconnaissance_radius_m=None, speed_mps=None):
    scenario = _profile_parameters(base_plan, flight_profile, reconnaissance_radius_m, speed_mps)
    path = _cache_path(base_plan, scenario)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema_version"] != 1
                or data["base_plan_sha256"] != coverage._plan_hash(base_plan)
                or data["planner_sha256"] != _source_digest()
                or data["scenario"] != scenario
                or data["plan_sha256"] != coverage._plan_hash(data["plan"])):
            raise coverage.PlanNotPreparedError("操场中央方案与当前边界或规划代码不匹配，请赛前重新生成")
        result = data["plan"]
        if (result["search_area"]["departure_point"] != DEPARTURE_POINT
                or result["prepared_plan"]["departure_point"] != DEPARTURE_POINT
                or result["prepared_plan"]["optimized_scenario"] != scenario):
            raise coverage.PlanNotPreparedError("操场中央方案标识不正确，请赛前重新生成")
        return copy.deepcopy(result)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise coverage.PlanNotPreparedError("没有匹配参数的操场中央固定方案，请赛前生成；比赛时不会重复求解") from error


def anchor_stadium_plan(adapted, source_plan):
    """把各场景局部零点移到球场中心；实验室 GPS 原点即起飞机的实时位置。"""
    result = copy.deepcopy(adapted)
    area = result["search_area"]
    source_area = source_plan["search_area"]
    source_points = source_area["points_m"]
    source_minx = min(float(point[0]) for point in source_points)
    source_miny = min(float(point[1]) for point in source_points)
    scale = float(area["width_m"]) / float(source_area["width_m"])
    source_depot = source_area["departure_point_m"]
    offset_x = (source_depot[0] - source_minx) * scale
    offset_y = (source_depot[1] - source_miny) * scale

    def shift(point):
        if math.dist((float(point[0]), float(point[1])), (offset_x, offset_y)) < 2e-6:
            return [0.0, 0.0]
        return [round(float(point[0]) - offset_x, 6), round(float(point[1]) - offset_y, 6)]

    area["points_m"] = [shift(point) for point in area["points_m"]]
    area["origin_x_m"] = round(float(area["origin_x_m"]) - offset_x, 6)
    area["origin_y_m"] = round(float(area["origin_y_m"]) - offset_y, 6)
    if area["coordinate_mode"] == "xyz":
        # 旧场景缩放函数只递归 GeoJSON 的 coordinates/geometry 键。
        # 图层字典需逐层处理，才能与中央局部零点、影像和航线重合。
        area["terrain_layers_m"] = {
            name: coverage._scale_local_geometry(geometry, scale, source_depot[0], source_depot[1])
            for name, geometry in source_area.get("terrain_layers_m", {}).items()
        }
    area["departure_point"] = DEPARTURE_POINT
    area["departure_point_m"] = [0.0, 0.0]
    area["departure_source"] = source_area["departure_source"]
    area["landing_mode"] = "onboard_home"
    area["planned_departure_reference_only"] = True
    mode = area["coordinate_mode"]
    profile = area["flight_profile"]
    if mode == "xyz":
        area["points"] = copy.deepcopy(area["points_m"])
        area["departure_point_wgs84"] = dict(STADIUM_CENTER_WGS84)
    elif profile == "competition":
        area["departure_point_wgs84"] = dict(STADIUM_CENTER_WGS84)
        area["gps_origin"] = dict(STADIUM_CENTER_WGS84)
        north, west = coverage._meters_per_degree(STADIUM_CENTER_WGS84["latitude"])
        area["coverage"]["projection"] = {
            "latitude": STADIUM_CENTER_WGS84["latitude"],
            "longitude": STADIUM_CENTER_WGS84["longitude"],
            "north_m_per_degree": north,
            "west_m_per_degree": west,
            "method": "WGS84_local_midlatitude",
        }
    else:
        origin = area.get("gps_origin") or {}
        latitude = float(origin["latitude"])
        longitude = float(origin["longitude"])
        north, west = coverage._meters_per_degree(latitude)
        area["departure_point_wgs84"] = {"latitude": latitude, "longitude": longitude}
        area["points"] = [
            [latitude + point[0] / north, longitude - point[1] / west]
            for point in area["points_m"]
        ]
    for item in result["planned_uavs"].values():
        task = item["task"]
        for key in ("polygon_m", "waypoints_m", "scan_waypoints_m", "flight_path_m"):
            task[key] = [shift(point) for point in task.get(key, [])]
        bounds = task["bounds_m"]
        for key in ("x_min", "x_max"):
            bounds[key] = round(float(bounds[key]) - offset_x, 6)
        for key in ("y_min", "y_max"):
            bounds[key] = round(float(bounds[key]) - offset_y, 6)
        for primitive in task.get("route_primitives", []):
            primitive["start"] = shift(primitive["start"])
            primitive["end"] = shift(primitive["end"])
        if mode == "gps" and profile != "competition":
            origin = area["gps_origins_by_uav"][str(item["uav_id"])]
            latitude = float(origin["latitude"])
            longitude = float(origin["longitude"])
            north, west = coverage._meters_per_degree(latitude)
            task["waypoints_wgs84"] = [
                [latitude + point[0] / north, longitude - point[1] / west]
                for point in task["waypoints_m"]
            ]
    mission_times = [
        float(item["task"]["mission_time_s"])
        for item in result["planned_uavs"].values()
    ]
    area["coverage"]["minimum_completion_time_s"] = min(mission_times)
    area["coverage"]["maximum_completion_time_s"] = max(mission_times)
    area["coverage"]["completion_time_spread_s"] = max(mission_times) - min(mission_times)
    result["prepared_plan"]["departure_point"] = DEPARTURE_POINT
    result["prepared_plan"]["departure_source"] = area["departure_source"]
    return result
