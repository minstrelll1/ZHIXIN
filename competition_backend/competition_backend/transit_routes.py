"""赛前固定进场/逐航点返航路径；运行时仅校验并转换坐标。"""
import hashlib
import heapq
import json
import math
from pathlib import Path

CACHE = Path(__file__).with_name('external_transit_prepared.json')
PROFILES = ('lab', 'outdoor5', 'lab10', 'outdoor100', 'outdoor200', 'competition', 'dalian_nanshan')


def scene_plan(profile, departure):
    from .polygon_coverage import plan_competition_coverage, adapt_competition_plan
    from .stadium_departure import load_prepared_stadium_plan, anchor_stadium_plan
    from .scaled_scene_plans import load_scaled_scene_plan
    if profile == 'dalian_nanshan':
        from .fixed_gps_scene import load_dalian_nanshan_plan
        return load_dalian_nanshan_plan()
    base = plan_competition_coverage()
    if profile in ('outdoor100', 'outdoor200'):
        return load_scaled_scene_plan(base, flight_profile=profile, departure_point=departure)
    source = load_prepared_stadium_plan(base, flight_profile=profile) if departure == 'stadium_center' else base
    plan = adapt_competition_plan(source, coordinate_mode='xyz', flight_profile=profile,
                                 max_extent_m={'lab': 3, 'outdoor5': 5, 'lab10': 10}.get(profile, 3),
                                 lab_speed_mps=.5 if profile == 'lab10' else .2)
    return anchor_stadium_plan(plan, source) if departure == 'stadium_center' else plan


def shortest_routes(boundary, departure, targets):
    from shapely.geometry import Polygon, Point, LineString
    polygon = Polygon(boundary)
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError('进返场规划边界无效')
    tolerance = 2e-5  # 仅容忍已保存坐标的微米级四舍五入。
    region = polygon.buffer(tolerance)
    if any(not region.covers(Point(p)) for p in targets):
        raise ValueError('侦察航点不在任务区域内部')
    points = [list(departure)] + [list(p) for p in boundary] + [list(p) for p in targets]
    outside = not region.covers(Point(departure))
    graph = [[] for _ in points]
    for i, a in enumerate(points):
        for j in range(i + 1, len(points)):
            b = points[j]
            line = LineString([a, b])
            allowed = region.covers(line)
            if i == 0 and outside:
                # 交集必须是以区域内终点结束的单段，禁止中途离开后再次进入。
                intersection = line.intersection(region)
                allowed = (intersection.geom_type == 'LineString' and not intersection.is_empty
                           and intersection.distance(Point(b)) <= tolerance)
            if allowed or math.dist(a, b) < 1e-9:
                distance = math.dist(a, b)
                graph[i].append((j, distance))
                # 区域外出发点只作为路径起点，不能作为内部路径的中转点。
                if i != 0 or not outside:
                    graph[j].append((i, distance))
    distances, previous, queue = {0: 0.0}, {}, [(0.0, 0)]
    while queue:
        distance, i = heapq.heappop(queue)
        if distance != distances[i]:
            continue
        for j, length in graph[i]:
            candidate = distance + length
            if candidate + 1e-10 < distances.get(j, math.inf):
                distances[j], previous[j] = candidate, i
                heapq.heappush(queue, (candidate, j))
    result = []
    for i in range(1 + len(boundary), len(points)):
        if i not in distances:
            raise ValueError('无法在任务区域内连接出发点与航点')
        route = [points[i]]
        while i:
            i = previous[i]
            if math.dist(route[-1], points[i]) > 1e-9:
                route.append(points[i])
        route.reverse()
        if len(route) == 1:
            route.append(list(route[0]))
        result.append(route)
    return result


def prepare_routes(plan):
    area = plan['search_area']
    boundary = area['points_m']
    departure = area.get('departure_point_m', [0., 0.])
    wrappers = plan['planned_uavs']
    targets = [p for item in wrappers.values() for p in item['task']['waypoints_m']]
    paths = iter(shortest_routes(boundary, departure, targets))
    vehicles = {}
    for uid, item in wrappers.items():
        scans = item['task']['waypoints_m']
        routes = [next(paths) for _ in scans]
        vehicles[str(uid)] = dict(waypoints_m=scans, entry_path_m=routes[0],
                                 return_paths_m=[list(reversed(route)) for route in routes])
    return dict(boundary_m=boundary, departure_m=departure, vehicles=vehicles)


def save_all_routes():
    scenes = {}
    for profile in PROFILES:
        for departure in (('fixed_dalian',) if profile == 'dalian_nanshan' else ('southeast', 'stadium_center')):
            scenes[profile + '/' + departure] = prepare_routes(scene_plan(profile, departure))
    canonical = json.dumps(scenes, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    document = dict(schema_version=1, sha256=hashlib.sha256(canonical.encode()).hexdigest(), scenes=scenes)
    CACHE.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return document


def attach_routes(plan):
    """不求解路径。高度在机载发布时统一填入本机任务相对高度。"""
    area = plan['search_area']
    key = area['flight_profile'] + '/' + area.get('departure_point', 'southeast')
    data = json.loads(CACHE.read_text(encoding='utf-8'))
    digest = hashlib.sha256(json.dumps(data['scenes'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if data.get('schema_version') != 1 or data.get('sha256') != digest:
        raise ValueError('固定进返场航线文件校验失败，请重新生成')
    saved = data['scenes'][key]
    # GPS 原竞赛场景的 points_m 保留地图投影原点，XYZ 已平移；仅允许整体平移。
    boundary, expected = area['points_m'], saved['boundary_m']
    shift = [boundary[0][i] - expected[0][i] for i in (0, 1)]
    if len(boundary) != len(expected) or any(math.dist([p[0]-shift[0], p[1]-shift[1]], q) > 1e-4 for p, q in zip(boundary, expected)):
        raise ValueError('任务边界与固定进返场方案不匹配')
    for uid, item in plan['planned_uavs'].items():
        task = item['task']
        fixed = saved['vehicles'][str(uid)]
        points = task['waypoints_m']
        if len(points) != len(fixed['waypoints_m']) or any(math.dist(a, b) > 1e-4 for a, b in zip(points, fixed['waypoints_m'])):
            raise ValueError('UAV%s 航点与固定进返场方案不匹配' % uid)
        frame = 'WGS84' if area['coordinate_mode'] == 'gps' else 'ENU'
        convert = lambda p: list(p)
        if frame == 'WGS84':
            gps = task['waypoints_wgs84']
            projection = area['coverage']['projection']
            factors = [1 / projection['north_m_per_degree'], -1 / projection['west_m_per_degree']]
            for axis in (0, 1):
                low = min(range(len(points)), key=lambda i: points[i][axis])
                high = max(range(len(points)), key=lambda i: points[i][axis])
                span = points[high][axis] - points[low][axis]
                if span > 1e-6:
                    factors[axis] = (gps[high][axis] - gps[low][axis]) / span
            def to_gps(p):
                lat = gps[0][0] + (p[0] - points[0][0]) * factors[0]
                lon = gps[0][1] + (p[1] - points[0][1]) * factors[1]
                return [lon, lat]
            if any(math.dist(to_gps(p), [g[1], g[0]]) > 2e-8 for p, g in zip(points, gps)):
                raise ValueError('GPS 航点与进返场投影不一致')
            convert = to_gps
        task['transit_routes'] = dict(schema_version=1, plan_sha256=data['sha256'], coordinate_frame=frame,
            coordinate_order='longitude_latitude' if frame == 'WGS84' else 'x_y',
            departure=convert(saved['departure_m']), entry_path=[convert(p) for p in fixed['entry_path_m']],
            return_paths=[dict(waypoint_index=i+1, path=[convert(p) for p in route]) for i, route in enumerate(fixed['return_paths_m'])])
    area['landing_mode'] = 'selected_departure'
    area['departure_point_m'] = list(saved['departure_m'])
    plan['prepared_transit_sha256'] = data['sha256']
    return plan
