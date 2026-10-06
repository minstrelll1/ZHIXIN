"""100/200/1000 米场景的赛前 5 米边界间距覆盖规划；运行时只读缓存。"""
from __future__ import annotations

import copy
import hashlib
import heapq
import json
import math
from functools import lru_cache
from pathlib import Path

from . import polygon_coverage as coverage


PROFILES = ("outdoor100", "outdoor200", "competition")
CLEARANCE_M = 5.0
# 留出投影/经纬度编码及文件小数舍入的余量，不把正好 5 米当成容差。
PLANNING_CLEARANCE_M = 5.03
CACHE = Path(__file__).with_name("competition_clearance_prepared.json")


def _hash(value):
    return hashlib.sha256(coverage._canonical(value).encode("utf-8")).hexdigest()


def _planner_hash():
    return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _key(plan):
    area = plan["search_area"]
    return area["flight_profile"] + "/" + area.get("departure_point", "southeast")


def _source_signature(plan):
    # 坐标表达和飞行高度不参与几何缓存；任务局部坐标在 GPS/XYZ 中一致。
    tasks = {str(uid): {
        name: item["task"].get(name) for name in (
            "polygon_m", "waypoints_m", "reconnaissance_radius_m",
            "speed_mps", "hover_scan_seconds",
        )
    } for uid, item in sorted(plan["planned_uavs"].items(), key=lambda pair: int(pair[0]))}
    terrain = plan["search_area"].get("terrain", {})
    boundary = plan["search_area"]["points_m"]
    normalized_boundary = [[round(p[axis] - boundary[0][axis], 4) for axis in (0, 1)] for p in boundary]
    landcover = Path(coverage.__file__).with_name("competition_landcover.json").read_bytes().replace(b"\r\n", b"\n")
    return _hash({"tasks": tasks, "boundary_shape_m": normalized_boundary,
                  "landcover_sha256": hashlib.sha256(landcover).hexdigest(),
                  "terrain": {"enabled": terrain.get("enabled", True),
                  "forest_edge_m": float(terrain.get("forest_edge_m", 5.0)),
                  "water_margin_m": float(terrain.get("water_margin_m", 10.0))}})


def _terrain_layers(plan):
    """按真实边界仿射变换原地类，修正旧场景逐层缩放遗漏。"""
    from shapely.affinity import affine_transform
    from shapely.geometry import Polygon, shape, mapping
    from shapely.ops import transform, unary_union
    base = coverage.plan_competition_coverage()
    source = base["search_area"]
    area = plan["search_area"]
    old, new = source["points_m"], area["points_m"]
    old_span = max(p[0] for p in old) - min(p[0] for p in old)
    new_span = max(p[0] for p in new) - min(p[0] for p in new)
    scale = new_span / old_span
    dx, dy = new[0][0] - old[0][0] * scale, new[0][1] - old[0][1] * scale
    if len(old) != len(new) or any(math.dist([p[0] * scale + dx, p[1] * scale + dy], q) > 2e-4
                                   for p, q in zip(old, new)):
        raise ValueError("5 米规划边界无法与原地类边界对齐")
    polygon = Polygon(new)
    # 使用未裁剪的原始地类再求内缘，避免把任务区裁切边界误当林缘。
    landcover = json.loads(Path(coverage.__file__).with_name("competition_landcover.json").read_text(encoding="utf-8"))
    projection = source["coverage"]["projection"]
    def project(lon, lat, z=None):
        return ((lat - projection["latitude"]) * projection["north_m_per_degree"],
                -(lon - projection["longitude"]) * projection["west_m_per_degree"])
    groups = {kind: [] for kind in ("forest", "water")}
    for feature in landcover["features"]:
        kind = feature["properties"].get("kind")
        if kind in groups:
            groups[kind].append(affine_transform(transform(project, shape(feature["geometry"])),
                                                 [scale, 0, 0, scale, dx, dy]))
    forest, water = [unary_union(groups[kind]) for kind in ("forest", "water")]
    edge = float(area.get("terrain", {}).get("forest_edge_m", 5.0))
    coverage._load_geometry()
    required, excluded, forest_edge = coverage.required_area(polygon, forest, water, edge, 10.0)
    if not area.get("terrain", {}).get("enabled", True):
        required, excluded = polygon, Polygon()
    layers = {"forest": forest.intersection(polygon), "water": water.intersection(polygon),
              "forest_edge": forest_edge, "excluded": excluded, "required": required}
    return layers, {name: mapping(value) for name, value in layers.items()}


class _Links:
    """一个子任务的可见图；全部航段始终位于内缩后的任务区。"""
    def __init__(self, safe, points, depot):
        from shapely.geometry import LineString
        self.points = [list(p) for p in points] + [list(depot)]
        self.depot = len(points)
        for part in coverage._parts(safe):
            for ring in [part.exterior, *part.interiors]:
                self.points.extend([list(p) for p in list(ring.coords)[:-1]])
        graph = [[] for _ in self.points]
        allowed = safe.buffer(1e-8)
        for i, a in enumerate(self.points):
            for j in range(i + 1, len(self.points)):
                b = self.points[j]
                if math.dist(a, b) < 1e-9 or allowed.covers(LineString([a, b])):
                    distance = math.dist(a, b)
                    graph[i].append((j, distance)); graph[j].append((i, distance))
        self.routes = {}
        for start in range(self.depot + 1):
            distances, previous, queue = {start: 0.0}, {}, [(0.0, start)]
            while queue:
                distance, node = heapq.heappop(queue)
                if distance != distances[node]:
                    continue
                for other, length in graph[node]:
                    candidate = distance + length
                    if candidate + 1e-10 < distances.get(other, math.inf):
                        distances[other], previous[other] = candidate, node
                        heapq.heappush(queue, (candidate, other))
            for end in range(self.depot + 1):
                if end not in distances:
                    raise ValueError("内缩 5 米后任务区不连通，无法生成完整航线")
                indices, node = [end], end
                while node != start:
                    node = previous[node]; indices.append(node)
                self.routes[start, end] = (distances[end], [self.points[i] for i in reversed(indices)])

    def distance(self, order):
        chain = [self.depot, *order, self.depot]
        return sum(self.routes[a, b][0] for a, b in zip(chain, chain[1:]))


def _safe_route(task, target, safe, depot):
    from shapely.geometry import Point, Polygon, LineString
    from shapely.ops import nearest_points, unary_union
    region = Polygon(task["polygon_m"])
    allowed = safe.intersection(region)
    if allowed.is_empty or allowed.area < 1e-7:
        raise ValueError("原子区内没有满足 5 米边界间距的可用航点")

    def project(point):
        p = Point(point)
        q = p if allowed.covers(p) else nearest_points(allowed, p)[0]
        return [q.x, q.y]

    # 保留原等比扫描密度；不能因为缩小后半径较大就删成一两个点。
    original = task["waypoints_m"]
    points = [project(point) for point in original]
    radius = float(task["reconnaissance_radius_m"])
    speed = float(task["speed_mps"])
    hover = float(task["hover_scan_seconds"])
    disks = [Point(p).buffer(radius - .02, quad_segs=32) for p in points]
    missing = target.difference(unary_union(disks))
    for _ in range(100):
        if missing.area <= 1e-5:
            break
        pieces = coverage._parts(missing)
        candidates = []
        for part in sorted(pieces, key=lambda part: part.area, reverse=True)[:10]:
            candidates.append(project(part.representative_point()))
            candidates.extend(project(p) for p in list(part.exterior.coords)[::max(1, len(part.exterior.coords) // 8)])
        gains = [(Point(p).buffer(radius - .02, quad_segs=32).intersection(missing).area, p) for p in candidates]
        gain, point = max(gains, key=lambda pair: pair[0])
        if gain < min(1e-7, missing.area * .001):
            raise ValueError("在 5 米间距和当前侦察半径下无法覆盖完整需侦察区域")
        points.append(point)
        missing = missing.difference(Point(point).buffer(radius - .02, quad_segs=32))
    else:
        raise ValueError("5 米间距覆盖补盲点数量超过上限")

    safe_depot = nearest_points(safe, Point(depot))[0]
    links = _Links(safe, points, (safe_depot.x, safe_depot.y))
    # 比较旧顺序与多起点最近邻、2-opt，避免优化反而拉长路线。
    orders = [list(range(len(points)))]
    for start in range(len(points)):
        order, remaining = [start], set(range(len(points))) - {start}
        while remaining:
            candidate = min(remaining, key=lambda index: (links.routes[order[-1], index][0], index))
            order.append(candidate); remaining.remove(candidate)
        orders.append(order)
    best = None
    for order in orders:
        length = links.distance(order)
        for _ in range(4):
            changed = False
            for left in range(len(order) - 1):
                for right in range(left + 1, len(order)):
                    candidate = order[:left] + order[left:right + 1][::-1] + order[right + 1:]
                    value = links.distance(candidate)
                    if value < length - 1e-6:
                        order, length, changed = candidate, value, True
            if not changed:
                break
        if best is None or length < best[0]:
            best = (length, order)
    _, order = best
    waypoints = [list(points[order[0]])]
    turning = 0
    for start, end in zip(order, order[1:]):
        path = links.routes[start, end][1]
        waypoints.extend(copy.deepcopy(path[1:]))
        turning += max(0, len(path) - 2)
    # 所有转折点都进入 path_stage_1；接收端逐点直连仍满足边界要求。
    if any(not safe.buffer(1e-7).covers(Point(p)) for p in waypoints) or any(
            not safe.buffer(1e-7).covers(LineString([a, b])) for a, b in zip(waypoints, waypoints[1:])):
        raise ValueError("5 米覆盖路线包含越界航段")
    flight_distance = sum(math.dist(a, b) for a, b in zip(waypoints, waypoints[1:]))
    covered = unary_union([Point(p).buffer(radius - .02, quad_segs=32) for p in waypoints])
    missed = target.difference(covered).area
    if missed > 1e-5:
        raise ValueError("新扫描航线未完整覆盖目标区域")
    return {
        "waypoints_m": waypoints, "scan_waypoints_m": copy.deepcopy(waypoints),
        "flight_path_m": copy.deepcopy(waypoints),
        "route_primitives": [{"kind": "line", "start": a, "end": b, "length_m": math.dist(a, b)}
                             for a, b in zip(waypoints, waypoints[1:])],
        "waypoint_actions": [{"index": i, "action": "hover_scan", "duration_s": hover}
                             for i in range(len(waypoints))],
        "route_distance_m": flight_distance, "travel_time_s": flight_distance / speed,
        "hover_scan_time_s": len(waypoints) * hover,
        "mission_time_s": flight_distance / speed + len(waypoints) * hover,
        "scan_count": len(waypoints), "original_scan_count": len(original),
        "coverage_added_scan_count": len(points) - len(original), "turning_point_count": turning,
        "coverage_ratio": 1.0, "uncovered_area_m2": missed,
        "required_area_m2": target.area, "boundary_clearance_m": CLEARANCE_M,
        "planning_clearance_m": PLANNING_CLEARANCE_M,
    }


def prepare_clearance_plan(raw_xyz_plan):
    """离线生成几何覆盖结果；不写文件，不用于比赛运行时求解。"""
    from shapely.geometry import Polygon, Point, LineString
    coverage._load_geometry()
    area = raw_xyz_plan["search_area"]
    if area["flight_profile"] not in PROFILES or area["coordinate_mode"] != "xyz":
        raise ValueError("5 米固定方案生成需要目标场景的原始 XYZ 方案")
    polygon = Polygon(area["points_m"])
    safe = polygon.buffer(-PLANNING_CLEARANCE_M, join_style=2)
    if safe.is_empty or safe.geom_type != "Polygon":
        raise ValueError("任务区内缩 5 米后为空或不连通")
    layers, serialized_layers = _terrain_layers(raw_xyz_plan)
    routes = {}
    minimum = math.inf
    for uid, item in sorted(raw_xyz_plan["planned_uavs"].items(), key=lambda pair: int(pair[0])):
        task = item["task"]
        target = layers["required"].intersection(Polygon(task["polygon_m"]))
        route = _safe_route(task, target, safe, area.get("departure_point_m", [0., 0.]))
        line = LineString(route["waypoints_m"]) if len(route["waypoints_m"]) > 1 else Point(route["waypoints_m"][0])
        minimum = min(minimum, line.distance(polygon.boundary))
        if minimum < CLEARANCE_M:
            raise ValueError("新航点或航段的边界间距小于 5 米")
        routes[str(uid)] = route
    times = [route["mission_time_s"] for route in routes.values()]
    terrain = copy.deepcopy(area.get("terrain", {}))
    terrain.update(forest_area_m2=layers["forest"].area, water_area_m2=layers["water"].area,
                   forest_edge_area_m2=layers["forest_edge"].area, excluded_area_m2=layers["excluded"].area,
                   required_area_m2=layers["required"].area)
    return {
        "schema_version": 1, "key": _key(raw_xyz_plan),
        "source_signature": _source_signature(raw_xyz_plan), "planner_sha256": _planner_hash(),
        "boundary_m": copy.deepcopy(area["points_m"]), "safe_boundary_m": list(safe.exterior.coords)[:-1],
        "tasks": routes, "terrain_layers_m": serialized_layers, "terrain": terrain,
        "coverage": {"objective": "preserve_scan_density_full_cover_then_shortest_safe_routes",
                     "algorithm": "safe_projection_full_disk_cover_visibility_graph_multistart_2opt",
                     "global_optimum_proven": False, "boundary_clearance_m": CLEARANCE_M,
                     "planning_clearance_m": PLANNING_CLEARANCE_M, "minimum_route_clearance_m": minimum,
                     "coverage_ratio": 1.0, "uncovered_area_m2": sum(r["uncovered_area_m2"] for r in routes.values()),
                     "required_area_m2": layers["required"].area, "excluded_area_m2": layers["excluded"].area,
                     "total_scan_count": sum(r["scan_count"] for r in routes.values()),
                     "total_distance_m": sum(r["route_distance_m"] for r in routes.values()),
                     "total_mission_time_s": sum(times), "minimum_completion_time_s": min(times),
                     "maximum_completion_time_s": max(times), "completion_time_spread_s": max(times) - min(times),
                     "maximum_aircraft_distance_m": max(r["route_distance_m"] for r in routes.values()),
                     "includes_transit": False},
    }


def save_prepared_clearance_plan(raw_xyz_plan):
    plan = prepare_clearance_plan(raw_xyz_plan)
    document = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"schema_version": 1, "scenes": {}}
    document["scenes"][plan["key"]] = plan
    document["sha256"] = _hash(document["scenes"])
    temporary = CACHE.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CACHE)
    _read_cache.cache_clear()
    return plan


@lru_cache(maxsize=2)
def _read_cache(modified_ns, size):
    document = json.loads(CACHE.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("sha256") != _hash(document["scenes"]):
        raise ValueError("5 米边界间距固定规划文件校验失败")
    return document


def apply_clearance_plan(adapted_plan):
    """叠加只读固定几何，沿用当前 GPS 参考与本机任务高度。"""
    if adapted_plan["search_area"]["flight_profile"] not in PROFILES:
        return adapted_plan
    stat = CACHE.stat()
    saved = _read_cache(stat.st_mtime_ns, stat.st_size)["scenes"][_key(adapted_plan)]
    if adapted_plan.get("prepared_clearance_sha256"):
        if saved["planner_sha256"] != _planner_hash() or adapted_plan["prepared_clearance_sha256"] != _hash(saved):
            raise ValueError("已加载的 5 米边界间距方案已过期，请重新规划")
        return adapted_plan
    if saved["planner_sha256"] != _planner_hash() or saved["source_signature"] != _source_signature(adapted_plan):
        raise ValueError("5 米边界间距方案来源已变化，请先重新生成固定规划")
    result = copy.deepcopy(adapted_plan)
    area = result["search_area"]
    boundary, old_boundary = area["points_m"], saved["boundary_m"]
    offset = [boundary[0][axis] - old_boundary[0][axis] for axis in (0, 1)]
    if len(boundary) != len(old_boundary) or any(
            math.dist([p[0] - offset[0], p[1] - offset[1]], q) > 1e-4 for p, q in zip(boundary, old_boundary)):
        raise ValueError("5 米边界间距方案与任务边界不匹配")
    from shapely.affinity import translate
    from shapely.geometry import shape, mapping
    area["terrain_layers_m"] = {name: mapping(translate(shape(layer), xoff=offset[0], yoff=offset[1]))
                                for name, layer in saved["terrain_layers_m"].items()}
    area["terrain"].update(copy.deepcopy(saved["terrain"]))
    area["coverage"].update(copy.deepcopy(saved["coverage"]))
    area["boundary_clearance_m"] = CLEARANCE_M
    area["planning_clearance_m"] = PLANNING_CLEARANCE_M
    for uid, item in result["planned_uavs"].items():
        task = item["task"]
        gps = task.get("waypoints_wgs84")
        old_points = task["waypoints_m"]
        if area["coordinate_mode"] == "gps":
            if not gps or len(gps) != len(old_points):
                raise ValueError("5 米固定方案缺少原任务 GPS 对应关系")
            projection = area["coverage"]["projection"]
            factors = [1 / projection["north_m_per_degree"], -1 / projection["west_m_per_degree"]]
            for axis in (0, 1):
                low = min(range(len(old_points)), key=lambda index: old_points[index][axis])
                high = max(range(len(old_points)), key=lambda index: old_points[index][axis])
                span = old_points[high][axis] - old_points[low][axis]
                if span > 1e-6:
                    factors[axis] = (gps[high][axis] - gps[low][axis]) / span
            anchor, gps_anchor = old_points[0], gps[0]
        task.update(copy.deepcopy(saved["tasks"][str(uid)]))
        if area["coordinate_mode"] == "gps":
            task["waypoints_wgs84"] = [[gps_anchor[0] + (p[0] - anchor[0]) * factors[0],
                                        gps_anchor[1] + (p[1] - anchor[1]) * factors[1]]
                                       for p in task["waypoints_m"]]
    result["prepared_clearance_sha256"] = _hash(saved)
    result["prepared_plan"]["boundary_clearance_m"] = CLEARANCE_M
    return result
