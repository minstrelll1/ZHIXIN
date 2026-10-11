"""正式科目一固定 WGS84 场地：离线覆盖优化，运行时只读取已验证方案。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import polygon_coverage as c
from .clearance_plans import _safe_route
from .stadium_departure import STADIUM_CENTER_WGS84, _power_cells

PROFILE = "subject1_actual"
NAME = "科目一比赛使用场景"
RADIUS_M, SPEED_MPS, HOVER_SECONDS = 50.0, 5.0, 10.0
RADII_BY_UAV = {uid: (45.0 if uid == 6 else RADIUS_M) for uid in range(1, 7)}
CLEARANCE_M, PLANNING_CLEARANCE_M = 5.0, 5.03
OTHER_HOME_CLEARANCE_M = 2.5
AREA_PATH = Path(__file__).with_name("subject1_actual_area.json")
PLAN_PATH = Path(__file__).with_name("subject1_actual_prepared.json")


def digest(value):
    return hashlib.sha256(c._canonical(value).encode("utf-8")).hexdigest()


def source_digest():
    paths = [Path(__file__), AREA_PATH] + [Path(__file__).with_name(name) for name in
        ("polygon_coverage.py", "clearance_plans.py", "stadium_departure.py", "landing_avoidance.py", "competition_landcover.json")]
    return hashlib.sha256(b"\n".join(path.read_bytes().replace(b"\r\n", b"\n") for path in paths)).hexdigest()


def prepare_plan(progress=print):
    """全覆盖为硬约束；最小化六机并行完成时间，其次最小化总机时。"""
    from shapely.geometry import Polygon, Point, LineString
    from shapely.ops import unary_union, nearest_points
    from shapely.affinity import rotate
    c._load_geometry()
    source = json.loads(AREA_PATH.read_text(encoding="utf-8"))
    points, projection = c.local_projection(source["points"])
    polygon = Polygon(points)
    safe = polygon.buffer(-PLANNING_CLEARANCE_M, join_style=2)
    if not polygon.is_valid or safe.geom_type != "Polygon":
        raise ValueError("正式比赛边界或5米安全区无效")
    depot = [(STADIUM_CENTER_WGS84["latitude"] - projection["latitude"]) * projection["north_m_per_degree"],
             -(STADIUM_CENTER_WGS84["longitude"] - projection["longitude"]) * projection["west_m_per_degree"]]
    if not safe.covers(Point(depot)):
        raise ValueError("操场中心不在5米安全区内")
    required, terrain, layers = c._terrain(polygon, projection, True, 5.0)
    route_cache = {}
    scan_domain = safe.intersection(required)
    # 凹边界的可见图转折点可能落在免扫树林内部。将停留转折点
    # 移到需侦察区域，连接航段仍允许越过免扫地类（它们不是禁飞区）。
    turning_candidates = [list(nearest_points(scan_domain, Point(p))[0].coords[0])
                          for p in list(safe.exterior.coords)[:-1]]

    def remove_excluded_stops(route, target):
        from .landing_avoidance import routes_from_home
        original = route["waypoints_m"]
        indices = [i for i, p in enumerate(original) if required.buffer(1e-7).covers(Point(p))]
        if len(indices) == len(original):
            return route
        rebuilt = [original[indices[0]]]
        for i, j in zip(indices, indices[1:]):
            if j == i + 1:
                rebuilt.append(original[j])
            else:
                path = routes_from_home(points, original[i], [original[j]], [], turning_points=turning_candidates)[0]
                rebuilt.extend(path[1:])
        covered = unary_union([Point(p).buffer(route["_radius"]-.02, quad_segs=32) for p in rebuilt])
        if target.difference(covered).area > 1e-5:
            raise ValueError("移除免扫区转折点后产生覆盖空缺")
        route.update(waypoints_m=rebuilt, scan_waypoints_m=copy.deepcopy(rebuilt), scan_count=len(rebuilt),
            turning_point_count=len(rebuilt)-route["original_scan_count"]-route["coverage_added_scan_count"],
            hover_scan_time_s=len(rebuilt)*HOVER_SECONDS,
            waypoint_actions=[dict(index=i, action="hover_scan", duration_s=HOVER_SECONDS) for i in range(len(rebuilt))],
            route_primitives=[dict(kind="line",start=a,end=b,length_m=math.dist(a,b)) for a,b in zip(rebuilt,rebuilt[1:])])
        return route

    def evaluate(regions):
        if len(regions) != 6 or any(r.geom_type != "Polygon" or r.area < polygon.area * .04 for r in regions):
            return None
        routes = []
        for uid, region in enumerate(regions, 1):
            radius = RADII_BY_UAV[uid]
            key = (region.wkb, radius)
            if key not in route_cache:
                target = required.intersection(region)
                allowed = safe.intersection(region).intersection(required)
                if target.is_empty or allowed.is_empty:
                    return None
                scans = c._scan_points(region, target, radius)
                scans = [list(nearest_points(allowed, Point(p))[0].coords[0]) for p in scans]
                task = dict(polygon_m=list(region.exterior.coords)[:-1], waypoints_m=scans,
                            reconnaissance_radius_m=radius, speed_mps=SPEED_MPS, hover_scan_seconds=HOVER_SECONDS)
                route = _safe_route(task, target, safe, depot)
                route["_radius"] = radius
                route = remove_excluded_stops(route, target)
                route.pop("_radius")
                # 连接转折点也作为任务航点下发，保证逐点直连不切出安全区。
                from .transit_routes import clearance_routes
                connectors = clearance_routes(points, depot, [route["waypoints_m"][0], route["waypoints_m"][-1]], CLEARANCE_M)
                flight = connectors[0] + route["waypoints_m"][1:] + list(reversed(connectors[1]))[1:]
                distance = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
                route.update(flight_path_m=flight, route_distance_m=distance,
                             travel_time_s=distance / SPEED_MPS,
                             mission_time_s=distance / SPEED_MPS + len(route["waypoints_m"]) * HOVER_SECONDS)
                route_cache[key] = route
            routes.append(route_cache[key])
        times = [r["mission_time_s"] for r in routes]
        return (max(times), sum(times), sum(r.length for r in regions)), regions, routes

    candidates = []
    # 以既有两套分区作为基线，并比较旋转紧凑分区，避免只更换场景名称。
    for filename in ("competition_coverage_default.json", "competition_coverage_stadium_center.json"):
        document = json.loads(Path(__file__).with_name(filename).read_text(encoding="utf-8"))
        baseline = document["plan"]
        candidates.append([Polygon(item["task"]["polygon_m"]) for item in baseline["planned_uavs"].values()])
    for heading in (0, -6, 6, 30, 60):
        for _, parts in c._partition_candidates(rotate(polygon, -heading, origin=(0, 0)), 2.0, beam=8):
            candidates.append([rotate(part, heading, origin=(0, 0)) for part in parts])
    best, evaluated = None, 0
    for index, regions in enumerate(candidates):
        regions = sorted(regions, key=lambda r: (-r.centroid.x, r.centroid.y))
        result = evaluate(regions)
        evaluated += 1
        if result and (best is None or result[0] < best[0]):
            best = result
        progress("候选 {}/{}，当前最长任务 {:.1f}s，总机时 {:.1f}s".format(index + 1, len(candidates), *best[0][:2]), flush=True)
    # 从最优紧凑分区调整权重，使远区承担较少覆盖工作，继续减小最晚完成时间。
    seeds = [(r.centroid.x, r.centroid.y) for r in best[1]]
    weights = [0.] * 6
    for iteration in range(20):
        regions = _power_cells(polygon, seeds, weights)
        if regions is None or any(c.shape_metrics(r)["aspect_ratio"] > 2.3 for r in regions):
            break
        result = evaluate(regions)
        evaluated += 1
        if not result:
            break
        if result[0] < best[0]:
            best = result
        times = [r["mission_time_s"] for r in result[2]]
        mean = sum(times) / 6
        gain = 60. if iteration < 6 else 20.
        weights = [w - gain * (t - mean) for w, t in zip(weights, times)]
        progress("用时优化 {}/20，当前最长任务 {:.1f}s".format(iteration + 1, best[0][0]), flush=True)
    _, regions, routes = best
    joined = unary_union(regions)
    if polygon.symmetric_difference(joined).area > 1e-5 or sum(r.area for r in regions) - joined.area > 1e-5:
        raise ValueError("六区存在空缺或重叠")
    tasks = {}
    for uid, (region, route) in enumerate(zip(regions, routes), 1):
        route = copy.deepcopy(route)
        minx, miny, maxx, maxy = region.bounds
        task = dict(route, type="lawnmower_search", coordinate_frame="LOCAL_NORTH_WEST", sector=uid,
                    polygon_m=list(map(list, list(region.exterior.coords)[:-1])), area_m2=region.area,
                    bounds_m=dict(x_min=minx, x_max=maxx, y_min=miny, y_max=maxy), shape=c.shape_metrics(region),
                    reconnaissance_radius_m=RADII_BY_UAV[uid], reconnaissance_mode="hover_scan", speed_mps=SPEED_MPS,
                    hover_scan_seconds=HOVER_SECONDS, turn_radius_m=0.,
                    waypoints_wgs84=[[projection["latitude"] + p[0] / projection["north_m_per_degree"],
                                      projection["longitude"] - p[1] / projection["west_m_per_degree"]] for p in route["waypoints_m"]])
        tasks[str(uid)] = dict(uav_id=uid, task=task)
    times = [r["mission_time_s"] for r in routes]
    minimum = min(LineString(r["flight_path_m"]).distance(polygon.boundary) for r in routes)
    if minimum < CLEARANCE_M:
        raise ValueError("完整任务航线距离边界不足5米")
    minx, miny, maxx, maxy = polygon.bounds
    coverage = dict(projection=projection, objective="full_coverage_then_minimize_makespan_then_total_time",
        objective_scope="six_simultaneous_aircraft_from_stadium_including_return",
        algorithm="compact_rotated_partitions_weighted_cells_safe_disk_cover_multistart_2opt",
        global_optimum_proven=False, evaluated_partitions=evaluated, uav_count=6,
        reconnaissance_radius_m=RADIUS_M, reconnaissance_radius_m_by_uav=RADII_BY_UAV,
        speed_mps=SPEED_MPS, hover_scan_seconds=HOVER_SECONDS,
        boundary_clearance_m=CLEARANCE_M, minimum_route_clearance_m=minimum,
        coverage_ratio=1., area_m2=polygon.area, required_area_m2=required.area,
        excluded_area_m2=polygon.area-required.area, uncovered_area_m2=sum(r["uncovered_area_m2"] for r in routes),
        maximum_completion_time_s=max(times), minimum_completion_time_s=min(times),
        completion_time_spread_s=max(times)-min(times), total_mission_time_s=sum(times),
        total_distance_m=sum(r["route_distance_m"] for r in routes), total_scan_count=sum(r["scan_count"] for r in routes),
        includes_transit=True, timing_note="按操场中央、5m/s和每点10秒估算；不含爬升、下降、发现目标后的跟踪时间")
    area = dict(name=NAME, coordinate_mode="gps", coordinate_frame="WGS84", flight_profile=PROFILE,
        departure_point="stadium_center", departure_point_m=depot, departure_point_wgs84=dict(STADIUM_CENTER_WGS84),
        departure_source="用户指定操场中心；沿用2017 Esri影像中心坐标", planned_departure_reference_only=True,
        landing_mode="onboard_home", home_reference_policy="first_valid_unarmed_gps_per_boot",
        other_home_clearance_m=OTHER_HOME_CLEARANCE_M, boundary_clearance_m=CLEARANCE_M,
        points=source["points"], points_m=list(map(list, points)), origin_x_m=minx, origin_y_m=miny,
        width_m=maxy-miny, height_m=maxx-minx, polygon_width_m=maxy-miny, polygon_height_m=maxx-minx,
        gps_origin=dict(latitude=projection["latitude"], longitude=projection["longitude"], source="fixed_competition_boundary"),
        terrain=terrain, terrain_layers_m=layers, coverage=coverage, turn_radius_m=0.)
    return dict(preview_only=True, subject="subject1", mission_id="coverage-preview", controller_mode="preview",
        planned_uavs=tasks, uavs={}, search_area=area,
        prepared_plan=dict(id=source_digest()[:20], source_digest=source_digest(), flight_profile=PROFILE,
            departure_point="stadium_center", runtime_mode="load_prepared_only", runtime_recalculation=False,
            generated_at=datetime.now(timezone.utc).isoformat(),
            parameters=dict(radius=RADIUS_M, radius_by_uav=RADII_BY_UAV, speed=SPEED_MPS, hover=HOVER_SECONDS, terrain=True, uav_count=6)))


def save_plan(plan):
    payload = dict(schema_version=1, source_digest=source_digest(), plan_sha256=digest(plan), plan=plan)
    temporary = PLAN_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PLAN_PATH)
    return PLAN_PATH


def load_plan(*, coordinate_mode="gps"):
    if coordinate_mode != "gps":
        raise ValueError(NAME + "只能使用GPS")
    payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if (payload.get("schema_version") != 1 or payload.get("source_digest") != source_digest()
            or payload.get("plan_sha256") != digest(payload["plan"])):
        raise ValueError("科目一正式比赛固定规划已过期，请赛前重新生成")
    return copy.deepcopy(payload["plan"])
