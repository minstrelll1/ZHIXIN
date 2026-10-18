"""进返场软偏好：有界尝试，失败即使用基本合法路线，不拒绝分派。"""
import copy
import heapq
import math

OTHER_HOME_CLEARANCE_M = 1.5


def _paths(region, home, targets):
    import numpy as np
    from shapely.geometry import Point
    from shapely import prepare
    from .landing_avoidance import _visible_pairs
    parts = [region] if region.geom_type == 'Polygon' else list(getattr(region, 'geoms', []))
    region = next((p for p in parts if p.geom_type == 'Polygon' and p.covers(Point(home))), None)
    if region is None or any(not region.buffer(1e-8).covers(Point(p)) for p in targets):
        raise ValueError('软避让切断航路')
    allowed = region.buffer(1e-8); prepare(allowed)
    vertices = [list(p) for ring in (region.exterior, *region.interiors) for p in list(ring.coords)[:-1]]
    nodes = [list(home), *vertices]
    coordinates = np.asarray(nodes, dtype=float)
    a, b = np.triu_indices(len(nodes), 1)
    visible = _visible_pairs(allowed, coordinates[a], coordinates[b])
    graph = [[] for _ in nodes]
    for i, j in zip(a[visible], b[visible]):
        length = math.dist(nodes[i], nodes[j])
        graph[i].append((j, length)); graph[j].append((i, length))
    distances, previous, queue = {0: 0.}, {}, [(0., 0)]
    while queue:
        distance, i = heapq.heappop(queue)
        if distance != distances[i]:
            continue
        for j, length in graph[i]:
            value = distance + length
            if value < distances.get(j, math.inf) - 1e-10:
                distances[j], previous[j] = value, i
                heapq.heappush(queue, (value, j))
    result = []
    for target in targets:
        mask = _visible_pairs(allowed, coordinates, np.broadcast_to(target, coordinates.shape))
        choices = [(distances[i] + math.dist(p, target), i) for i, p in enumerate(nodes) if i in distances and mask[i]]
        if not choices:
            raise ValueError('软避让切断航路')
        _, i = min(choices); path = [list(target)]
        while True:
            if math.dist(path[-1], nodes[i]) > 1e-9:
                path.append(nodes[i])
            if i == 0:
                break
            i = previous[i]
        path.reverse()
        result.append(path if len(path) > 1 else path * 2)
    return result


def routes_from_home(boundary, home, targets, others, *, holes=(), clearance=5.):
    """所有目标共用一张小可见图，不在全部扫描点之间建立平方数量的边。"""
    from shapely.geometry import Polygon, Point
    from shapely.ops import unary_union
    from .transit_routes import clearance_routes, shortest_routes
    safe = Polygon(boundary, holes=holes).buffer(-clearance - .02, join_style=2) if clearance else Polygon(boundary, holes=holes).buffer(2e-5)
    if not safe.covers(Point(home)):
        # 保留原有场界/禁飞区连接规则；其他起降点间距不阻塞连接。
        paths = (clearance_routes(boundary, home, targets, clearance, holes=holes) if clearance
                 else shortest_routes(boundary, home, targets))
        return paths, {'home_avoidance_relaxed': bool(others)}
    if others:
        radius = (OTHER_HOME_CLEARANCE_M + .03) / math.cos(math.pi / 12)
        obstacles = unary_union([Point(p).buffer(radius, quad_segs=3) for p in others])
        try:
            return _paths(safe.difference(obstacles), home, targets), {'home_avoidance_relaxed': False}
        except ValueError:
            pass
    return _paths(safe, home, targets), {'home_avoidance_relaxed': bool(others)}


def prefer_entries(boundary, entries, homes, *, holes=(), clearance=5.):
    """最多两种顺序、每段六个偏移候选；交叉/共线是评分，无间距硬门槛。"""
    from shapely.geometry import Polygon, LineString, Point
    safe = Polygon(boundary, holes=holes).buffer(-clearance - .02, join_style=2) if clearance else Polygon(boundary, holes=holes)
    allowed = safe.buffer(1e-7)
    uids = sorted(entries)
    candidates = {}
    for uid in uids:
        base = entries[uid]; values = [base]
        for index, (a, b) in enumerate(zip(base, base[1:])):
            length = math.dist(a, b)
            if length < 1e-6:
                continue
            for offset in (-20., -8., -2., 2., 8., 20.):
                bend = [(a[0]+b[0])/2 - (b[1]-a[1])*offset/length,
                        (a[1]+b[1])/2 + (b[0]-a[0])*offset/length]
                candidate = base[:index+1] + [bend] + base[index+1:]
                if allowed.covers(LineString(candidate)):
                    values.append(candidate)
        candidates[uid] = values
    def score(uid, path, selected):
        line = LineString(path)
        crossings = sum(line.crosses(LineString(p)) for p in selected.values())
        shared = sum(line.intersection(LineString(p)).length for p in selected.values())
        close = sum(line.distance(Point(p)) < OTHER_HOME_CLEARANCE_M - 1e-6 for other, p in homes.items() if other != uid)
        return (close * 1000 + crossings * 1000 + shared * 100, line.length)
    options = []
    for order in (uids, list(reversed(uids))):
        selected = {}
        for uid in order:
            selected[uid] = min(candidates[uid], key=lambda p: score(uid, p, selected))
        total = sum(score(uid, path, {i:p for i,p in selected.items() if i != uid})[0] for uid,path in selected.items())
        options.append((total, sum(LineString(p).length for p in selected.values()), selected))
    chosen = min(options, key=lambda v: v[:2])
    return chosen[2], dict(policy='soft_preferences', no_shared_segments=True, no_planar_crossings=True,
                          required_inter_route_distance_m=None, other_home_clearance_m=OTHER_HOME_CLEARANCE_M,
                          preferences_relaxed=chosen[0] > 0, candidate_orders=2)


def finalize_entries(area, runtimes, actual_routes):
    """联合改善真实或名义进场路线；未满足偏好仍保留可下发路线。"""
    from .transit_routes import geometry_for, SAFE_TRANSIT_PROFILES, ALL_PROFILES
    profile = area.get('flight_profile')
    if profile not in ALL_PROFILES:
        return  # 手工/外部 prepared_plan 沿用其已验证的路线，不查固定场景目录。
    routes = {uid: copy.deepcopy(actual_routes.get(uid) or runtime.task.get('transit_routes')) for uid,runtime in runtimes.items()}
    routes = {uid:r for uid,r in routes.items() if r}
    if not routes:
        return
    saved = geometry_for(area)
    # 以航点对应的变换为准，兼容旧 GPS 缩放场景的整体平移。
    sample = next(iter(runtimes.values())).task
    if next(iter(routes.values()))['coordinate_frame'] == 'WGS84':
        projection = area['coverage']['projection']
        p0, g0 = sample['waypoints_m'][0], sample['waypoints_wgs84'][0]
        factors = [1/projection['north_m_per_degree'], -1/projection['west_m_per_degree']]
        for axis in (0,1):
            lo = min(range(len(sample['waypoints_m'])), key=lambda i: sample['waypoints_m'][i][axis])
            hi = max(range(len(sample['waypoints_m'])), key=lambda i: sample['waypoints_m'][i][axis])
            span = sample['waypoints_m'][hi][axis] - sample['waypoints_m'][lo][axis]
            if span > 1e-6:
                factors[axis] = (sample['waypoints_wgs84'][hi][axis]-sample['waypoints_wgs84'][lo][axis])/span
        def to_local(p):
            return [p0[0]+(p[1]-g0[0])/factors[0], p0[1]+(p[0]-g0[1])/factors[1]]
        def convert(p):
            return [g0[1]+(p[1]-p0[1])*factors[1], g0[0]+(p[0]-p0[0])*factors[0]]
    else:
        to_local = convert = lambda p: list(p)
    entries = {uid:[to_local(p) for p in r['entry_path']] for uid,r in routes.items()}
    homes = {uid:to_local(r['departure']) for uid,r in routes.items()}
    improved, metadata = prefer_entries(saved['boundary_m'], entries, homes,
        holes=saved.get('excluded_polygons_m', []), clearance=5. if profile in SAFE_TRANSIT_PROFILES else 0.)
    area['entry_separation'] = metadata
    area['mission_time_limit_s'] = None
    for uid,r in routes.items():
        path = [convert(p) for p in improved[uid]]
        path[0], path[-1] = r['entry_path'][0], r['entry_path'][-1]
        r.update(entry_path=path, other_home_clearance_m=1.5, transit_constraints='soft_preferences')
        from shapely.geometry import LineString, Point
        if any(LineString(improved[uid]).distance(Point(p)) < OTHER_HOME_CLEARANCE_M - 1e-6
               for other, p in homes.items() if other != uid):
            r.update(home_avoidance_relaxed=True, avoided_home_count=0)
        runtimes[uid].task['transit_routes'] = r
        task = runtimes[uid].task
        if task.get('normal_return_source'):
            selected = task['normal_return_source']
            returning = next(p['path'] for p in r['return_paths']
                             if (p['source_uav_id'],p['waypoint_index']) == (selected['source_uav_id'],selected['waypoint_index']))
            scans = ([p for layer in task['layers'] for p in layer['waypoints_m']] if task.get('layers')
                     else task['waypoints_m'] + task['waypoints_m'][::-1])
            task['entry_path_m'] = improved[uid]
            task['flight_path_m'] = improved[uid] + scans + [to_local(p) for p in returning]
            # 显示参考路径，不代替 B 在任务中自主选择返航入口和执行回升。
        if uid in actual_routes:
            actual_routes[uid] = r


def update_subject2_timing(task, entry, returning, altitude):
    """保留科目二正逆两趟的估时展示，删除 14 分钟门槛。"""
    scans = task['waypoints_m']
    flight = [*entry, *scans[1:], *scans[-2::-1], *returning[1:]]
    distance = sum(math.dist(a,b) for a,b in zip(flight,flight[1:]))
    task.update(scan_passes=2, scan_visit_count=2*len(scans),
        scan_execution_order=list(range(len(scans)))+list(reversed(range(len(scans)))),
        scan_pass_order='forward_then_reverse', flight_path_m=flight, entry_path_m=entry,
        final_return_path_m=returning, route_distance_m=distance, travel_time_s=distance/5.,
        hover_scan_time_s=60.*len(scans), takeoff_time_s=float(altitude)/6., takeoff_speed_mps=6.,
        timing_altitude_m=float(altitude), mission_time_s=distance/5.+60.*len(scans)+float(altitude)/6.,
        mission_time_limit_s=None, remaining_time_s=None)
