"""赛前生成 100/200 米固定六机方案；运行时只读取并校验快照。"""
from __future__ import annotations

import copy
import hashlib
import itertools
import math
from pathlib import Path

from . import polygon_coverage as coverage
from . import stadium_departure


SCENES = {"outdoor100": 100.0, "outdoor200": 200.0}
RADIUS_M = 75.0
SPEED_MPS = 5.0
HOVER_SECONDS = 10.0


def _digest():
    return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _source(base_plan, departure_point):
    if departure_point == "southeast":
        return base_plan
    if departure_point == "stadium_center":
        return stadium_departure.load_prepared_stadium_plan(base_plan, flight_profile="competition")
    raise ValueError("未知的出发点")


def _cache_path(profile, departure_point):
    if profile not in SCENES:
        raise ValueError("未知的缩小场景")
    if departure_point not in ("southeast", "stadium_center"):
        raise ValueError("未知的出发点")
    return Path(__file__).with_name("competition_coverage_{}_{}.json".format(profile, departure_point))


def _source_departure(source, departure_point):
    area = source["search_area"]
    if departure_point == "stadium_center":
        return [float(x) for x in area["departure_point_m"]]
    points = area["points_m"]
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    span_x = max(point[0] for point in points) - min_x
    span_y = max(point[1] for point in points) - min_y
    # 本地坐标 X 向北、Y 向西；东南角取两轴归一化之和最小的边界顶点。
    return list(min(points, key=lambda p: ((p[0] - min_x) / span_x + (p[1] - min_y) / span_y)))


def _proportional_route(task, *, region, target, boundary, depot, departure_point):
    """Keep every source scan and its order when reducing the competition plan."""
    def move(point):
        return [float(point[0]) - depot[0], float(point[1]) - depot[1]]

    scans = [move(point) for point in task["waypoints_m"]]
    if not scans:
        if target.area > 1e-5:
            raise ValueError("等比缩小方案缺少覆盖目标区域的航点")
        return {
            "waypoints_m": [], "scan_waypoints_m": [], "flight_path_m": [],
            "route_primitives": [], "route_distance_m": 0.0, "scan_count": 0,
            "travel_time_s": 0.0, "hover_scan_time_s": 0.0,
            "mission_time_s": 0.0, "coverage_ratio": 1.0,
            "uncovered_area_m2": 0.0,
        }

    # The prepared stadium route already begins and ends at the common depot.
    # The southeast source contains only the scan path, so add safe transit
    # within the overall competition boundary without changing scan order.
    flight = [move(point) for point in task["flight_path_m"]]
    if departure_point == "southeast":
        # Six-decimal scaling can put an original boundary waypoint fractions
        # of a millimetre outside the new polygon; allow only that tolerance.
        transit_boundary = boundary.buffer(1e-4)
        outbound = coverage._shortest_link((0.0, 0.0), flight[0], transit_boundary)
        inbound = coverage._shortest_link(flight[-1], (0.0, 0.0), transit_boundary)
        flight = [list(point) for point in outbound] + flight[1:] + [list(point) for point in inbound[1:]]
    if not all(region.buffer(1e-4).covers(coverage.Point(point)) for point in scans):
        raise ValueError("等比缩小方案有航点落在子区域外")
    covered = coverage.unary_union([
        coverage.Point(point).buffer(RADIUS_M - 0.02, quad_segs=32)
        for point in scans
    ])
    missed = target.difference(covered).area
    if missed > 1e-5:
        raise ValueError("等比缩小方案未覆盖需侦察区域")
    distance = sum(math.dist(start, end) for start, end in itertools.pairwise(flight))
    travel = distance / SPEED_MPS
    hover = len(scans) * HOVER_SECONDS
    return {
        "waypoints_m": scans,
        "scan_waypoints_m": copy.deepcopy(scans),
        "flight_path_m": flight,
        "route_primitives": [
            {"kind": "line", "start": start, "end": end,
             "length_m": math.dist(start, end)}
            for start, end in itertools.pairwise(flight)
        ],
        "route_distance_m": distance,
        "scan_count": len(scans),
        "travel_time_s": travel,
        "hover_scan_time_s": hover,
        "mission_time_s": travel + hover,
        "coverage_ratio": 1.0,
        "uncovered_area_m2": missed,
    }


def prepare_scaled_scene_plan(base_plan, *, flight_profile, departure_point="southeast"):
    """仅供赛前命令调用；等比缩小已保存的六区和全部侦察航点。"""
    extent = SCENES.get(flight_profile)
    if extent is None:
        raise ValueError("未知的缩小场景")
    source = _source(base_plan, departure_point)
    source_area = source["search_area"]
    source_points = source_area["points_m"]
    min_x = min(float(point[0]) for point in source_points)
    min_y = min(float(point[1]) for point in source_points)
    span = max(float(source_area["width_m"]), float(source_area["height_m"]))
    scale = extent / span
    source_depot = _source_departure(source, departure_point)
    depot = [round((float(source_depot[0]) - min_x) * scale, 6),
             round((float(source_depot[1]) - min_y) * scale, 6)]

    # 复用原方案的形状缩放；不改写参与旧快照 SHA 校验的模块。
    result = coverage.adapt_competition_plan(
        source, coordinate_mode="xyz", flight_profile="lab10",
        max_extent_m=extent, lab_radius_m=RADIUS_M,
        lab_speed_mps=SPEED_MPS, lab_hover_seconds=HOVER_SECONDS,
    )
    coverage._load_geometry()
    from shapely.affinity import translate

    def shift(geometry):
        return translate(geometry, xoff=-depot[0], yoff=-depot[1])

    area = result["search_area"]
    polygon = shift(coverage.Polygon(area["points_m"]))
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError("缩小场景边界无效")
    source_layers = area.get("terrain_layers_m", {})
    forest = shift(coverage.shape(source_layers["forest"]))
    water = shift(coverage.shape(source_layers["water"]))
    required, excluded, forest_edge = coverage.required_area(
        polygon, forest, water, forest_edge_m=5.0, water_margin_m=10.0,
    )
    if not area.get("terrain", {}).get("enabled", True):
        required, excluded = polygon, coverage.Polygon()
    layers = {
        "forest": forest.intersection(polygon),
        "water": water.intersection(polygon),
        "forest_edge": forest_edge,
        "excluded": excluded,
        "required": required,
    }
    area["terrain_layers_m"] = {key: coverage.mapping(value) for key, value in layers.items()}
    area["points_m"] = [[round(x - depot[0], 6), round(y - depot[1], 6)]
                        for x, y in area["points_m"]]
    area["points"] = copy.deepcopy(area["points_m"])
    area["origin_x_m"] = min(point[0] for point in area["points_m"])
    area["origin_y_m"] = min(point[1] for point in area["points_m"])
    area["flight_profile"] = flight_profile
    area["departure_point"] = departure_point
    area["departure_point_m"] = [0.0, 0.0]
    area["departure_source"] = ("操场中央影像估算参考点" if departure_point == "stadium_center"
                                else "比赛区域东南边界顶点")
    area["landing_mode"] = "onboard_home"
    area["planned_departure_reference_only"] = True
    area["terrain"].update(
        forest_edge_m=5.0, water_margin_m=10.0,
        forest_area_m2=layers["forest"].area,
        water_area_m2=layers["water"].area,
        forest_edge_area_m2=forest_edge.area,
        excluded_area_m2=excluded.area,
        required_area_m2=required.area,
    )

    distance_sum = time_sum = 0.0
    scans_sum = 0
    maximum_time = 0.0
    uncovered = 0.0
    for wrapper in result["planned_uavs"].values():
        task = wrapper["task"]
        region = shift(coverage.Polygon(task["polygon_m"]))
        target = required.intersection(region)
        route = _proportional_route(
            task, region=region, target=target, boundary=polygon,
            depot=depot, departure_point=departure_point,
        )
        task.update(route)
        task["polygon_m"] = [list(point) for point in list(region.exterior.coords)[:-1]]
        minx, miny, maxx, maxy = region.bounds
        task["bounds_m"] = {"x_min": minx, "x_max": maxx, "y_min": miny, "y_max": maxy}
        task["area_m2"] = region.area
        task["required_area_m2"] = target.area
        task["shape"] = coverage.shape_metrics(region)
        task["reconnaissance_radius_m"] = RADIUS_M
        task["speed_mps"] = SPEED_MPS
        task["hover_scan_seconds"] = HOVER_SECONDS
        task["coordinate_frame"] = "ENU"
        task.pop("waypoints_wgs84", None)
        task["waypoint_actions"] = [
            {"index": index, "action": "hover_scan", "duration_s": HOVER_SECONDS}
            for index in range(route["scan_count"])
        ]
        distance_sum += route["route_distance_m"]
        time_sum += route["mission_time_s"]
        maximum_time = max(maximum_time, route["mission_time_s"])
        scans_sum += route["scan_count"]
        uncovered += route["uncovered_area_m2"]
    if uncovered > 1e-5:
        raise ValueError("缩小场景侦察覆盖未通过验证")

    area["coverage"].update(
        reconnaissance_radius_m=RADIUS_M, speed_mps=SPEED_MPS,
        hover_scan_seconds=HOVER_SECONDS,
        objective="proportional_competition_route_with_fixed_departure",
        area_m2=polygon.area, required_area_m2=required.area,
        excluded_area_m2=excluded.area, uncovered_area_m2=uncovered,
        total_mission_time_s=time_sum, maximum_completion_time_s=maximum_time,
        total_distance_m=distance_sum, total_scan_count=scans_sum,
        maximum_aircraft_distance_m=max(
            item["task"]["route_distance_m"] for item in result["planned_uavs"].values()
        ),
        includes_transit=True,
    )
    result["prepared_plan"].update(
        id=hashlib.sha256((coverage._plan_hash(source) + flight_profile + departure_point + _digest()).encode()).hexdigest()[:20],
        departure_point=departure_point, flight_profile=flight_profile,
        runtime_mode="load_prepared_only", runtime_recalculation=False,
        optimized_scenario={"flight_profile": flight_profile, "max_extent_m": extent,
                            "radius_m": RADIUS_M, "speed_mps": SPEED_MPS,
                            "hover_scan_seconds": HOVER_SECONDS},
    )
    return result


def save_prepared_scaled_scene(base_plan, plan, *, flight_profile, departure_point):
    """保存独立快照，不影响原比赛/操场中央方案。"""
    source = _source(base_plan, departure_point)
    path = _cache_path(flight_profile, departure_point)
    payload = {
        "schema_version": 1,
        "source_plan_sha256": coverage._plan_hash(source),
        "planner_sha256": _digest(),
        "flight_profile": flight_profile,
        "departure_point": departure_point,
        "plan_sha256": coverage._plan_hash(plan),
        "plan": plan,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(coverage._canonical(payload), encoding="utf-8")
    temporary.replace(path)
    return path


def load_scaled_scene_plan(base_plan, *, flight_profile, departure_point="southeast",
                           coordinate_mode="xyz", gps_origin=None, gps_origins_by_uav=None):
    """运行时只读完整、来源匹配的固定快照，随后做坐标表达转换。"""
    import json

    source = _source(base_plan, departure_point)
    path = _cache_path(flight_profile, departure_point)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema_version"] != 1
                or data["source_plan_sha256"] != coverage._plan_hash(source)
                or data["planner_sha256"] != _digest()
                or data["flight_profile"] != flight_profile
                or data["departure_point"] != departure_point
                or data["plan_sha256"] != coverage._plan_hash(data["plan"])):
            raise coverage.PlanNotPreparedError("缩小场景固定方案校验失败，请赛前重新生成")
        result = data["plan"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise coverage.PlanNotPreparedError("没有匹配的 100/200 米固定方案，请赛前生成") from error
    mode = str(coordinate_mode).strip().lower()
    if mode not in ("xyz", "gps"):
        raise ValueError("coordinate_mode must be gps or xyz")
    result = copy.deepcopy(result)
    area = result["search_area"]
    area["coordinate_mode"] = mode
    area["coordinate_frame"] = "WGS84" if mode == "gps" else "ENU"
    if mode == "gps":
        raw_origins = gps_origins_by_uav if isinstance(gps_origins_by_uav, dict) else {}
        if not raw_origins and isinstance(gps_origin, dict):
            raw_origins = {str(uav_id): gps_origin for uav_id in result["planned_uavs"]}
        origins = {}
        for uav_id in result["planned_uavs"]:
            origin = raw_origins.get(str(uav_id)) or raw_origins.get(int(uav_id))
            if not isinstance(origin, dict):
                raise ValueError("缩小场景 GPS 方案缺少 UAV{} 的经纬度参考".format(uav_id))
            try:
                latitude = float(origin["latitude"])
                longitude = float(origin["longitude"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("缩小场景 GPS 经纬度参考无效") from error
            if not (math.isfinite(latitude) and math.isfinite(longitude)
                    and abs(latitude) <= 90 and abs(longitude) <= 180):
                raise ValueError("缩小场景 GPS 经纬度参考无效")
            origins[str(uav_id)] = {"latitude": latitude, "longitude": longitude,
                                    "source": origin.get("source", "onboard_uav{}".format(uav_id))}

        def to_gps(point, origin):
            latitude = origin["latitude"]
            longitude = origin["longitude"]
            north, west = coverage._meters_per_degree(latitude)
            return [round(latitude + point[0] / north, 10),
                    round(longitude - point[1] / west, 10)]

        first = next(iter(origins.values()))
        north, west = coverage._meters_per_degree(first["latitude"])
        area["gps_origin"] = copy.deepcopy(first)
        area["gps_origins_by_uav"] = copy.deepcopy(origins)
        area["points"] = [to_gps(point, first) for point in area["points_m"]]
        area["departure_point_wgs84"] = {"latitude": first["latitude"],
                                          "longitude": first["longitude"]}
        area["coverage"]["projection"] = {
            "latitude": first["latitude"], "longitude": first["longitude"],
            "north_m_per_degree": north, "west_m_per_degree": west,
            "method": "WGS84_local_midlatitude",
        }
        for uav_id, item in result["planned_uavs"].items():
            task = item["task"]
            task["coordinate_frame"] = "LOCAL_NORTH_WEST"
            task["waypoints_wgs84"] = [to_gps(point, origins[uav_id])
                                       for point in task["waypoints_m"]]
    result["prepared_plan"]["coordinate_mode"] = mode
    return result
