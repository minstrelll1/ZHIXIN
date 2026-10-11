"""赛前固定进场/逐航点返航路径；运行时仅校验并转换坐标。"""
import copy
import hashlib
import heapq
import json
import math
from pathlib import Path

CACHE = Path(__file__).with_name('external_transit_prepared.json')
PROFILES = ('lab', 'outdoor5', 'lab10', 'outdoor100', 'outdoor200', 'competition', 'dalian_nanshan', 'xuchang_small', 'subject1_actual')
CLEARANCE_PROFILES = ('outdoor100', 'outdoor200', 'competition')
SAFE_TRANSIT_PROFILES = CLEARANCE_PROFILES + ('xuchang_small', 'subject1_actual')
BOUNDARY_CLEARANCE_M = 5.0
BOOT_HOME_PROFILES = ('subject1_actual', 'xuchang_small')


def clearance_region(boundary, distance_m, holes=()):
    """直线航段使用保守内缩多边形；额外 2cm 余量抵消坐标舍入误差。"""
    from shapely.geometry import Polygon
    polygon = Polygon(boundary, holes=holes)
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError('带扣除区的任务边界无效')
    region = polygon.buffer(-float(distance_m) - 0.02, join_style=2)
    if region.is_empty or not region.is_valid or region.geom_type != 'Polygon':
        raise ValueError('任务区域内缩后没有连通的安全航行区域，无法满足边界间距')
    return region


def clearance_routes(boundary, departure, targets, distance_m, *, holes=(), allow_target_connectors=False):
    """内部航段保持边界间距，边缘/区域外起降点仅经专门连接段进出。"""
    from shapely.geometry import Polygon, Point, LineString
    flyable = Polygon(boundary, holes=holes)
    outer = flyable.buffer(2e-5)
    safe = clearance_region(boundary, distance_m, holes=holes)
    vertices = [list(p) for ring in (safe.exterior, *safe.interiors) for p in list(ring.coords)[:-1]]
    if any(Polygon(hole).contains(Point(departure)) for hole in holes):
        raise ValueError('实际起降点位于扣除区内，不可规划穿越扣除区的进返场航线')

    def gateway(point, allow_outside=False):
        p = Point(point)
        if safe.covers(p):
            return list(point)
        if not allow_outside and not outer.covers(p):
            raise ValueError('侦察航点不在任务区域内部')
        candidates = []
        for a, b in zip(vertices, vertices[1:] + vertices[:1]):
            edge = LineString([a, b])
            candidate = edge.interpolate(edge.project(p))
            candidates.extend((list(candidate.coords[0]), list(a)))
        for candidate in sorted(candidates, key=lambda c: math.dist(point, c)):
            segment = LineString([point, candidate])
            if outer.covers(segment):
                return candidate
            if allow_outside and not outer.covers(p):
                intersection = segment.intersection(outer)
                if (intersection.geom_type == 'LineString' and not intersection.is_empty
                        and intersection.distance(Point(candidate)) <= 2e-5):
                    return candidate
        raise ValueError('无法通过单一连接段进入内缩安全区，请调整起降点或侦察航点')

    if not allow_target_connectors and any(not safe.covers(Point(p)) for p in targets):
        raise ValueError('侦察航点距离任务边界不足要求的安全间距')
    start = gateway(departure, allow_outside=True)
    ends = [gateway(point) for point in targets]
    # 包含内环顶点的可见图，直线只有完全位于内缩后的可飞区才可用。
    nodes = [start, *vertices, *ends]
    allowed = safe.buffer(1e-8)
    graph = [[] for _ in nodes]
    for i, a in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            b = nodes[j]
            if math.dist(a, b) < 1e-9 or allowed.covers(LineString([a, b])):
                length = math.dist(a, b)
                graph[i].append((j, length))
                graph[j].append((i, length))
    distances, previous, queue = {0: 0.0}, {}, [(0.0, 0)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        for other, length in graph[node]:
            candidate = distance + length
            if candidate + 1e-10 < distances.get(other, math.inf):
                distances[other], previous[other] = candidate, node
                heapq.heappush(queue, (candidate, other))
    paths = []
    for node in range(1 + len(vertices), len(nodes)):
        if node not in distances:
            raise ValueError('侦察航点与起降点在安全区域内无法连通')
        chain = [nodes[node]]
        while node:
            node = previous[node]
            chain.append(nodes[node])
        paths.append(list(reversed(chain)))
    result = []
    for target, inner_path in zip(targets, paths):
        path = []
        for point in [list(departure)] + inner_path + [list(target)]:
            if not path or math.dist(path[-1], point) > 1e-9:
                path.append(list(point))
        if len(path) == 1:
            path.append(list(path[0]))
        result.append(path)
    return result


def scene_plan(profile, departure, apply_clearance=True):
    from .polygon_coverage import plan_competition_coverage, adapt_competition_plan
    from .stadium_departure import load_prepared_stadium_plan, anchor_stadium_plan
    from .scaled_scene_plans import load_scaled_scene_plan
    if profile == 'subject1_actual':
        from .subject1_actual_scene import load_plan
        return load_plan()
    if profile == 'dalian_nanshan':
        from .fixed_gps_scene import load_dalian_nanshan_plan
        return load_dalian_nanshan_plan()
    if profile == 'xuchang_small':
        from .xuchang_small_scene import load_xuchang_small_plan
        return load_xuchang_small_plan()
    base = plan_competition_coverage()
    if profile in ('outdoor100', 'outdoor200'):
        plan = load_scaled_scene_plan(base, flight_profile=profile, departure_point=departure)
    else:
        source = load_prepared_stadium_plan(base, flight_profile=profile) if departure == 'stadium_center' else base
        plan = adapt_competition_plan(source, coordinate_mode='xyz', flight_profile=profile,
                                     max_extent_m={'lab': 3, 'outdoor5': 5, 'lab10': 10}.get(profile, 3),
                                     lab_speed_mps=.5 if profile == 'lab10' else .2)
        if departure == 'stadium_center':
            plan = anchor_stadium_plan(plan, source)
    if apply_clearance and profile in CLEARANCE_PROFILES:
        from .clearance_plans import apply_clearance_plan
        plan = apply_clearance_plan(plan)
    return plan


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


def rebase_gps_routes_for_takeoff(area, task, takeoff_gps):
    """保持六机侦察点，按接收机实测起飞 GPS 重接进场和全机队返航路径。"""
    fixed = task['transit_routes']
    if fixed.get('coordinate_frame') != 'WGS84':
        raise ValueError('只有 GPS 航线可按实测起飞位置重接')
    scans = task['waypoints_m']
    gps_scans = task['waypoints_wgs84']
    projection = area['coverage']['projection']
    factors = [1 / projection['north_m_per_degree'], -1 / projection['west_m_per_degree']]
    for axis in (0, 1):
        low = min(range(len(scans)), key=lambda i: scans[i][axis])
        high = max(range(len(scans)), key=lambda i: scans[i][axis])
        span = scans[high][axis] - scans[low][axis]
        if span > 1e-6:
            factors[axis] = (gps_scans[high][axis] - gps_scans[low][axis]) / span
    lat, lon = float(takeoff_gps['latitude']), float(takeoff_gps['longitude'])
    home = [scans[0][0] + (lat - gps_scans[0][0]) / factors[0],
            scans[0][1] + (lon - gps_scans[0][1]) / factors[1]]
    def convert(point):
        return [gps_scans[0][1] + (point[1] - scans[0][1]) * factors[1],
                gps_scans[0][0] + (point[0] - scans[0][0]) * factors[0]]
    key = area['flight_profile'] + '/' + area.get('departure_point', 'southeast')
    saved = json.loads(CACHE.read_text(encoding='utf-8'))['scenes'][key]
    saved_boundary = saved['boundary_m']
    if area['flight_profile'] == 'xuchang_small':
        from shapely.geometry import Point
        if not clearance_region(saved_boundary, BOUNDARY_CLEARANCE_M,
                                holes=saved.get('excluded_polygons_m', [])).covers(Point(home)):
            raise ValueError('许昌实际起降点距外边界或内部扣除区不足 5 米，不能下发此航线')
    # 比赛 GPS 方案会整体平移地图边界的局部坐标；扫描点保留赛前坐标。
    # 重接路径必须与扫描点使用同一局部坐标系。
    if fixed.get('schema_version') == 2:
        sources = [(int(uid), index + 1, point) for uid, points in
                   sorted(fixed['waypoints_by_uav'].items(), key=lambda item: int(item[0]))
                   for index, point in enumerate(points)]
        # 固定局部坐标是区域内几何的依据，避免经纬度舍入后反投影将边界点推到区域外。
        targets = [saved['vehicles'][str(uid)]['waypoints_m'][index - 1] for uid, index, _ in sources]
        if any(math.dist(convert(target), point) > 2e-8 for target, (_, _, point) in zip(targets, sources)):
            raise ValueError('全机队侦察航点与固定返航几何不一致')
        first_index = next(i for i, (uid, index, _) in enumerate(sources)
                           if uid == fixed['recipient_uav_id'] and index == 1)
    else:
        sources = [(None, i + 1, [point[1], point[0]]) for i, point in enumerate(gps_scans)]
        targets, first_index = scans, 0
    distance = BOUNDARY_CLEARANCE_M if area['flight_profile'] in SAFE_TRANSIT_PROFILES else 0.0
    others = []
    if area['flight_profile'] in BOOT_HOME_PROFILES:
        for uid, gps_home in area.get('takeoff_gps_by_uav', {}).items():
            if int(uid) == int(fixed['recipient_uav_id']):
                continue
            others.append([scans[0][0] + (float(gps_home['latitude']) - gps_scans[0][0]) / factors[0],
                           scans[0][1] + (float(gps_home['longitude']) - gps_scans[0][1]) / factors[1]])
        fixed = dict(fixed, other_home_clearance_m=2.5, avoided_home_count=len(others),
                     home_reference_source=takeoff_gps.get('source', ''),
                     home_reference_boot_id=takeoff_gps.get('boot_id', ''))
    lane_vehicle = saved['vehicles'].get(str(fixed.get('recipient_uav_id')), {})
    lane = lane_vehicle.get('transit_lane') if area['flight_profile'] == 'xuchang_small' else None
    if lane:
        # 实测home只接到各机固定通道门口，不再将固定通道重算为共用最短路。
        entry_gate, return_gate = lane['entry_gate_m'], lane['return_gate_m']
        entry_fixed = lane_vehicle['entry_path_m']
        if math.dist(entry_fixed[1], entry_gate) > 1e-8:
            raise ValueError('固定进场通道入口与缓存不一致')
        connectors = clearance_routes(saved_boundary, home, [entry_gate, return_gate], distance,
                                      holes=saved.get('excluded_polygons_m', []))
        entry_path = _join_paths(connectors[0], entry_fixed[1:])
        by_source = {(int(part['source_uav_id']), int(part['waypoint_index'])): part['path']
                     for part in lane_vehicle['fleet_return_paths_m']}
        if set(by_source) != {(uid, index) for uid, index, _ in sources}:
            raise ValueError('固定分离返航通道未覆盖全部机队航点')
        paths = []
        for uid, index, _ in sources:
            path = by_source[(uid, index)]
            if (math.dist(path[-2], return_gate) > 1e-8
                    or math.dist(path[0], saved['vehicles'][str(uid)]['waypoints_m'][index-1]) > 1e-8):
                raise ValueError('固定返航通道端点与缓存不一致')
            paths.append(list(reversed(_join_paths(path[:-1], list(reversed(connectors[1]))))))
    elif area['flight_profile'] == 'subject1_actual':
        from .landing_avoidance import routes_from_home
        paths = routes_from_home(saved_boundary, home, targets, others)
        entry_path = paths[first_index]
    else:
        if distance:
            paths = clearance_routes(saved_boundary, home, targets, distance,
                                     holes=saved.get('excluded_polygons_m', []))
        else:
            paths = shortest_routes(saved_boundary, home, targets)
        entry_path = paths[first_index]
    if area['flight_profile'] == 'xuchang_small':
        from .landing_guided_avoidance import detour_guided_paths
        entry_path, *paths = detour_guided_paths(saved_boundary, [entry_path, *paths], others,
                                                holes=saved.get('excluded_polygons_m', []))
    def convert_path(path, point, reverse=False):
        points = [convert(point) for point in (reversed(path) if reverse else path)]
        if reverse:
            points[0], points[-1] = list(point), [lon, lat]
        else:
            points[0], points[-1] = [lon, lat], list(point)
        return points
    return dict(fixed, departure=[lon, lat],
                entry_path=convert_path(entry_path, sources[first_index][2]),
                return_paths=[dict(waypoint_index=index, path=convert_path(path, point, True),
                                   **({'source_uav_id': uid} if uid is not None else {}))
                              for (uid, index, point), path in zip(sources, paths)])



def _join_paths(left, right):
    result = []
    for point in [*left, *right]:
        if not result or math.dist(result[-1], point) > 1e-9:
            result.append(list(point))
    return result if len(result) > 1 else result + copy.deepcopy(result)


def prepare_routes(plan):
    area = plan['search_area']
    boundary = area['points_m']
    departure = area.get('departure_point_m', [0., 0.])
    wrappers = plan['planned_uavs']
    targets = [p for item in wrappers.values() for p in item['task']['waypoints_m']]
    distance = BOUNDARY_CLEARANCE_M if area['flight_profile'] in SAFE_TRANSIT_PROFILES else 0.0
    holes = area.get('excluded_polygons_m', [])
    paths = iter(clearance_routes(boundary, departure, targets, distance, holes=holes) if distance
                 else shortest_routes(boundary, departure, targets))
    vehicles = {}
    for uid, item in wrappers.items():
        scans = item['task']['waypoints_m']
        routes = [next(paths) for _ in scans]
        vehicles[str(uid)] = dict(waypoints_m=scans, entry_path_m=routes[0],
                                 return_paths_m=[list(reversed(route)) for route in routes])
    if area['flight_profile'] == 'xuchang_small':
        from .xuchang_transit_lanes import build_recipient_routes
        fleet_points = {str(uid): item['task']['waypoints_m'] for uid, item in wrappers.items()}
        default_fleet_returns = [dict(source_uav_id=int(uid), waypoint_index=i + 1, path=copy.deepcopy(path))
                                 for uid, vehicle in sorted(vehicles.items(), key=lambda item: int(item[0]))
                                 for i, path in enumerate(vehicle['return_paths_m'])]
        for uid, vehicle in vehicles.items():
            separated = build_recipient_routes(boundary, holes, departure, fleet_points, int(uid))
            if not separated:
                vehicle['fleet_return_paths_m'] = copy.deepcopy(default_fleet_returns)
                continue
            vehicle.update(separated)
            vehicle['return_paths_m'] = [part['path'] for part in separated['fleet_return_paths_m']
                                         if int(part['source_uav_id']) == int(uid)]
    result = dict(boundary_m=boundary, departure_m=departure, vehicles=vehicles)
    if distance:
        result.update(boundary_clearance_m=distance,
                      endpoint_clearance_policy='takeoff_landing_connector_only',
                      safe_boundary_m=[list(p) for p in list(clearance_region(boundary,distance,holes=holes).exterior.coords)[:-1]])
        if area['flight_profile'] in CLEARANCE_PROFILES:
            result['prepared_clearance_sha256'] = plan['prepared_clearance_sha256']
        if holes:
            result['excluded_polygons_m'] = copy.deepcopy(holes)
            result['safe_excluded_polygons_m'] = [list(map(list, list(ring.coords)[:-1])) for ring in
                                                   clearance_region(boundary,distance,holes=holes).interiors]
    return result


def save_all_routes():
    scenes = {}
    for profile in PROFILES:
        for departure in (('stadium_center',) if profile == 'subject1_actual' else
                          ('fixed_dalian',) if profile == 'dalian_nanshan' else
                          ('fixed_xuchang',) if profile == 'xuchang_small' else
                          ('southeast', 'stadium_center')):
            scenes[profile + '/' + departure] = prepare_routes(scene_plan(profile, departure))
    canonical = json.dumps(scenes, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    document = dict(schema_version=1, sha256=hashlib.sha256(canonical.encode()).hexdigest(), scenes=scenes)
    CACHE.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return document


def attach_routes(plan):
    """不求解路径。高度在机载发布时统一填入本机任务相对高度。"""
    if plan['search_area']['flight_profile'] in CLEARANCE_PROFILES:
        from .clearance_plans import apply_clearance_plan
        plan = apply_clearance_plan(plan)
    area = plan['search_area']
    key = area['flight_profile'] + '/' + area.get('departure_point', 'southeast')
    data = json.loads(CACHE.read_text(encoding='utf-8'))
    digest = hashlib.sha256(json.dumps(data['scenes'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if data.get('schema_version') != 1 or data.get('sha256') != digest:
        raise ValueError('固定进返场航线文件校验失败，请重新生成')
    saved = data['scenes'][key]
    if area['flight_profile'] in CLEARANCE_PROFILES and (
            saved.get('boundary_clearance_m') != BOUNDARY_CLEARANCE_M
            or saved.get('prepared_clearance_sha256') != plan.get('prepared_clearance_sha256')):
        raise ValueError('5米边界间距的进返场方案与侦察方案不一致，请更新固定规划文件')
    if area['flight_profile'] == 'xuchang_small' and (
            saved.get('boundary_clearance_m') != BOUNDARY_CLEARANCE_M
            or saved.get('excluded_polygons_m') != area.get('excluded_polygons_m', [])):
        raise ValueError('许昌扣除区与固定进返场方案不一致，请更新固定规划文件')
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
        if saved.get('boundary_clearance_m'):
            task['transit_routes'].update(boundary_clearance_m=saved['boundary_clearance_m'],
                endpoint_clearance_policy=saved['endpoint_clearance_policy'])
    # 静态方案六机共用选定出发点，可以直接复用已保存的每条几何路线。
    # GPS 实飞在分派阶段再按接收机自己的起降点重接全机队路线。
    fleet_points, fleet_returns = {}, []
    for uid, item in sorted(plan['planned_uavs'].items(), key=lambda item: int(item[0])):
        task = item['task']
        fleet_points[str(uid)] = ([[p[1], p[0]] for p in task['waypoints_wgs84']]
                                  if area['coordinate_mode'] == 'gps' else copy.deepcopy(task['waypoints_m']))
        for route in task['transit_routes']['return_paths']:
            fleet_returns.append(dict(copy.deepcopy(route), source_uav_id=int(uid)))
    for uid, item in plan['planned_uavs'].items():
        routes = item['task']['transit_routes']
        vehicle = saved['vehicles'][str(uid)]
        if area['flight_profile'] == 'xuchang_small' and vehicle.get('fleet_return_paths_m'):
            # 同一个来源航点，为不同接收机保存各自的返航接近通道。
            points = item['task']['waypoints_m']
            gps = item['task']['waypoints_wgs84']
            factors = [1 / area['coverage']['projection']['north_m_per_degree'],
                       -1 / area['coverage']['projection']['west_m_per_degree']]
            for axis in (0, 1):
                low = min(range(len(points)), key=lambda i: points[i][axis])
                high = max(range(len(points)), key=lambda i: points[i][axis])
                span = points[high][axis] - points[low][axis]
                if span > 1e-6:
                    factors[axis] = (gps[high][axis] - gps[low][axis]) / span
            def convert_recipient(point):
                return [gps[0][1] + (point[1] - points[0][1]) * factors[1],
                        gps[0][0] + (point[0] - points[0][0]) * factors[0]]
            returns = [dict(source_uav_id=int(part['source_uav_id']), waypoint_index=part['waypoint_index'],
                            path=[convert_recipient(point) for point in part['path']])
                       for part in vehicle['fleet_return_paths_m']]
            expected_sources = {(int(source_uid), i + 1) for source_uid, points in fleet_points.items()
                                for i in range(len(points))}
            if {(part['source_uav_id'], part['waypoint_index']) for part in returns} != expected_sources:
                raise ValueError('分离返航航线的全机队航点索引不完整')
        else:
            returns = copy.deepcopy(fleet_returns)
        for route in returns:
            route['path'][0] = list(fleet_points[str(route['source_uav_id'])][route['waypoint_index'] - 1])
            route['path'][-1] = list(routes['departure'])
        routes['entry_path'][-1] = list(fleet_points[str(uid)][0])
        routes.update(
            schema_version=2, recipient_uav_id=int(uid), route_scope='all_uav_waypoints',
            waypoints_by_uav=copy.deepcopy(fleet_points), return_paths=returns)
    area['landing_mode'] = 'onboard_home' if area['flight_profile'] in BOOT_HOME_PROFILES else 'selected_departure'
    if area['flight_profile'] in BOOT_HOME_PROFILES:
        area.update(home_reference_policy='first_valid_unarmed_gps_per_boot', other_home_clearance_m=2.5)
    area['departure_point_m'] = list(saved['departure_m'])
    plan['prepared_transit_sha256'] = data['sha256']
    return plan
