"""离线精简双层固定点：每层正逆两趟、30秒/点、960秒名义预算。"""
import copy
import math

from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import transform, unary_union


PROFILES = ('final_scene_2', 'xuchang_small_2')
BUDGET = 960.


def length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def refine_scenes(scenes, source):
    for original in source['scenes']:
        sid = original['scene_id']
        if sid not in PROFILES:
            continue
        scene = scenes[sid]
        plan, geometry = scene['plan'], scene['transit_geometry']
        area = plan['search_area']
        projection = area['coverage']['projection']
        def local(lon, lat, z=None):
            return ((lat-projection['latitude'])*projection['north_m_per_degree'],
                    -(lon-projection['longitude'])*projection['west_m_per_degree'])
        def gps(point):
            return [projection['latitude']+point[0]/projection['north_m_per_degree'],
                    projection['longitude']-point[1]/projection['west_m_per_degree']]
        effective = transform(local, shape(original['effective_area_geometry_wgs84']))
        safe = Polygon(area['points_m'], holes=area['excluded_polygons_m']).buffer(-5.)
        def allowed(a, b):
            return safe.buffer(1e-6).covers(Point(a) if a == b else LineString([a, b]))
        old_tasks = {uid: copy.deepcopy(v['task']) for uid, v in plan['planned_uavs'].items()}
        retained, coverage_disks = {}, {'upper': [], 'lower': []}
        for uid, item in plan['planned_uavs'].items():
            task = item['task']
            upper, lower = task['layers']
            old_upper = copy.deepcopy(upper['waypoints_m'])
            start = old_upper[0]
            # 同一子区内上层首点也可用于半径更小的下层；原位换层避免长距离转场。
            paths = [[start], [start]]
            candidates = [copy.deepcopy(upper['waypoints_m']),
                          copy.deepcopy(lower['waypoints_m']) + copy.deepcopy(upper['waypoints_m'])]
            region = Polygon(task['polygon_m'], holes=task['polygon_holes_m'])
            required = effective.intersection(region)
            radii = [upper['reconnaissance_radius_m'], lower['reconnaissance_radius_m']]
            disks = [{tuple(p): Point(p).buffer(r).intersection(required) for p in [start]+pool}
                     for r, pool in zip(radii, candidates)]
            covered = [d[tuple(start)] for d in disks]
            home_route = geometry['vehicles'][uid]['return_paths_m'][0]
            vertical = upper['altitude_m']/6. + (upper['altitude_m']-lower['altitude_m'])/2. + (upper['altitude_m']-lower['altitude_m'])/6.
            transit = length(task['entry_path_m']) + length(home_route)
            def timing(routes):
                return vertical + transit/5. + sum(2*length(p)/5. + 60*len(p) for p in routes)
            # 从原固定点候选中按新增覆盖/额外用时选点；插入位置只允许合法直连。
            # 全部在导入工具中执行，运行时不会做覆盖优化。
            while True:
                options = []
                for li, pool in enumerate(candidates):
                    for ci, point in enumerate(pool):
                        if point in paths[li]:
                            continue
                        gain = disks[li][tuple(point)].difference(covered[li]).area
                        if gain < 1e-6:
                            continue
                        for index in range(1, len(paths[li])+1):
                            before = paths[li][index-1]
                            after = paths[li][index] if index < len(paths[li]) else None
                            if not allowed(before, point) or (after is not None and not allowed(point, after)):
                                continue
                            proposed = [list(p) for p in paths]
                            proposed[li].insert(index, point)
                            duration = timing(proposed)
                            if duration <= BUDGET:
                                cost = duration-timing(paths)
                                # 优先补足覆盖较弱的一层，避免大半径上层挤占全部扫描预算。
                                balance = 1. / (.2 + covered[li].area/max(required.area, 1.))
                                options.append((gain/cost*balance, gain, -duration, -li, -ci, -index, proposed))
                if not options:
                    break
                best = max(options, key=lambda row: row[:6])
                li = -best[3]
                paths = best[-1]
                covered[li] = unary_union([disks[li][tuple(p)] for p in paths[li]])
            for layer, points in zip((upper, lower), paths):
                layer.update(waypoints_m=points, waypoints_wgs84=[gps(p) for p in points],
                    waypoint_ids=['UAV%s_%s_%03d' % (uid, layer['layer_id'], i+1) for i in range(len(points))],
                    scan_passes=2, hover_scan_seconds=30., scan_pass_order='forward_then_reverse',
                    scan_execution_order=list(range(len(points)))+list(reversed(range(len(points)))))
                coverage_disks[layer['layer_id']].extend(Point(p).buffer(layer['reconnaissance_radius_m']) for p in points)
            retained[uid] = [old_upper.index(p)+1 for p in paths[0]]
            task.update(waypoints_m=upper['waypoints_m'], waypoints_wgs84=upper['waypoints_wgs84'],
                execution_mode='upper_roundtrip_then_lower_roundtrip', scan_pass_order='forward_then_reverse',
                scan_passes=2, hover_scan_seconds=30., mission_budget_seconds=BUDGET,
                scan_count=len(paths[0]), scan_visit_count=2*sum(map(len, paths)),
                hover_scan_time_s=60*sum(map(len, paths)), mission_time_s=timing(paths),
                route_distance_m=transit+2*sum(map(length, paths)),
                scan_waypoints_m=paths[0]+paths[1], normal_return_source=dict(source_uav_id=int(uid), waypoint_index=1),
                layer_transition=dict(from_waypoint_id=upper['waypoint_ids'][0], to_waypoint_id=lower['waypoint_ids'][0],
                    horizontal_distance_m=0., vertical_distance_m=upper['altitude_m']-lower['altitude_m'],
                    descent_speed_mps=2., descent_speed_status='accepted_provisional',
                    duration_s=(upper['altitude_m']-lower['altitude_m'])/2., preferred_horizontal_limit_m=20.,
                    preferred_limit_met=True, note='上层正逆两趟回到上层首点后原位下降；下层首点与上层首点重合。'),
                timing_components=dict(initial_climb_seconds=upper['altitude_m']/6.,
                    descent_seconds=(upper['altitude_m']-lower['altitude_m'])/2.,
                    return_climb_seconds=(upper['altitude_m']-lower['altitude_m'])/6.,
                    horizontal_flight_seconds=(transit+2*sum(map(length, paths)))/5.,
                    scan_seconds=60*sum(map(len, paths))),
                fixed_plan_revision='dual-roundtrip-30s-960s-v1')
            task['flight_path_m'] = task['entry_path_m'] + paths[0][1:] + list(reversed(paths[0])) + paths[1] + list(reversed(paths[1])) + home_route[1:]
            geometry['vehicles'][uid]['waypoints_m'] = upper['waypoints_m']
        # 上层删点/重排后重建全部接收机的返航索引；几何仍使用已校验的原固定返航线。
        fleet = {uid: [[p[1], p[0]] for p in u['task']['waypoints_wgs84']] for uid, u in plan['planned_uavs'].items()}
        def remap(routes):
            lookup = {(str(r['source_uav_id']), r['waypoint_index']): r for r in routes}
            return [dict(copy.deepcopy(lookup[(uid, old)]), waypoint_index=i+1)
                    for uid, indexes in retained.items() for i, old in enumerate(indexes)]
        for uid, item in plan['planned_uavs'].items():
            routes = item['task']['transit_routes']
            routes.update(waypoints_by_uav=fleet, return_paths=remap(old_tasks[uid]['transit_routes']['return_paths']))
            vehicle = geometry['vehicles'][uid]
            vehicle['fleet_return_paths_m'] = remap(vehicle['fleet_return_paths_m'])
            vehicle['return_paths_m'] = [r['path'] for r in vehicle['fleet_return_paths_m'] if r['source_uav_id'] == int(uid)]
        coverage = area['coverage']
        for label, disks in coverage_disks.items():
            union = unary_union(disks).intersection(effective)
            summed = sum(d.intersection(effective).area for d in disks)
            coverage['layer_metrics'][label] = dict(effective_area_m2=effective.area, covered_area_m2=union.area,
                uncovered_area_m2=effective.area-union.area, coverage_ratio=union.area/effective.area,
                independent_scan_count=len(disks), redundant_area_m2=summed-union.area,
                redundancy_ratio=(summed-union.area)/summed if summed else 0.)
        coverage.update(hover_scan_seconds=30., scan_passes=2, mission_budget_seconds=BUDGET,
            coverage_ratio=unary_union(coverage_disks['upper']+coverage_disks['lower']).intersection(effective).area/effective.area,
            total_scan_count=sum(len(u['task']['scan_waypoints_m']) for u in plan['planned_uavs'].values()),
            total_scan_visit_count=sum(u['task']['scan_visit_count'] for u in plan['planned_uavs'].values()),
            total_distance_m=sum(u['task']['route_distance_m'] for u in plan['planned_uavs'].values()),
            maximum_completion_time_s=max(u['task']['mission_time_s'] for u in plan['planned_uavs'].values()),
            total_mission_time_s=sum(u['task']['mission_time_s'] for u in plan['planned_uavs'].values()))
        area['mission_budget_seconds'] = BUDGET
        plan['prepared_plan']['revision'] = 'dual-roundtrip-30s-960s-v1'
