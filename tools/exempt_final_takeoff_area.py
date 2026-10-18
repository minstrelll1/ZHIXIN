"""离线删除决赛起降区内的扫描点；起降区可通行，不作为禁飞/扫描圆禁入区。"""
import copy
import math

from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union

PROFILES = ('final_scene_1', 'final_scene_2')
REVISION = 'takeoff-area-exempt-20261018-v1'
# 用户给定顺序：纬度、经度（度分秒转 WGS84 十进制度）。
VERTICES_WGS84 = [
    [33 + 51/60 + 51.19/3600, 113 + 42/60 + 21.23/3600],
    [33 + 51/60 + 53.30/3600, 113 + 42/60 + 20.95/3600],
    [33 + 51/60 + 53.17/3600, 113 + 42/60 + 19.16/3600],
    [33 + 51/60 + 51.00/3600, 113 + 42/60 + 19.44/3600],
]


def length(path):
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _orders(points, start, allowed):
    """保留既有点，只尝试原序/逆序的循环移位或将共用首点前置。"""
    options = []
    for values in (points, list(reversed(points))):
        index = values.index(start)
        for candidate in (values[index:] + values[:index],
                          [start] + [p for p in values if p != start]):
            line = Point(candidate[0]) if len(candidate) == 1 else LineString(candidate)
            if allowed.covers(line) and candidate not in options:
                options.append(candidate)
    return options


def exclude_takeoff_area(scenes, source):
    originals = {s['scene_id']: s for s in source['scenes']}
    for sid in PROFILES:
        scene = scenes[sid]
        plan, geometry = scene['plan'], scene['transit_geometry']
        area = plan['search_area']; projection = area['coverage']['projection']

        def local(lon, lat, z=None):
            return ((lat-projection['latitude'])*projection['north_m_per_degree'],
                    -(lon-projection['longitude'])*projection['west_m_per_degree'])

        ring = [list(local(lon, lat)) for lat, lon in VERTICES_WGS84]
        exempt = Polygon(ring)
        if not exempt.is_valid or exempt.is_empty:
            raise ValueError('竞赛起降区四点无效')
        descriptor = dict(id='competition_takeoff_area', name='竞赛起降区（免扫描，可通行）',
            kind='coverage_exempt', geometry_wgs84=mapping(Polygon([[lon, lat] for lat, lon in VERTICES_WGS84])),
            geometry_status='user_confirmed', source='用户四个度分秒坐标，2026-10-18',
            clearance_m=0., scan_disk_intersection_forbidden=False, flight_prohibited=False,
            scan_waypoint_forbidden=True)
        area['coverage_exempt_areas'] = [descriptor]
        terrain = area['terrain_layers_m']
        terrain['excluded'] = mapping(unary_union([shape(terrain['excluded']), exempt]))
        required = transform(local, shape(originals[sid]['effective_area_geometry_wgs84'])).difference(exempt)
        safe = Polygon(area['points_m'], holes=area['excluded_polygons_m']).buffer(-5.).buffer(1e-6)
        old_tasks = {uid: copy.deepcopy(u['task']) for uid, u in plan['planned_uavs'].items()}
        old_vehicles = copy.deepcopy(geometry['vehicles'])
        retained, changes, disks = {}, {}, {'upper': [], 'lower': []}

        for uid, item in plan['planned_uavs'].items():
            task = item['task']; original = old_tasks[uid]
            layers = task.get('layers') or [dict(layer_id=task['physical_layer'],
                altitude_m=item['target_altitude_m'], reconnaissance_radius_m=task['reconnaissance_radius_m'],
                hover_scan_seconds=task['hover_scan_seconds'], scan_passes=task['scan_passes'],
                waypoints_m=task['waypoints_m'], waypoints_wgs84=task['waypoints_wgs84'])]
            before = [copy.deepcopy(layer['waypoints_m']) for layer in layers]
            paths = [[p for p in points if not exempt.covers(Point(p))] for points in before]
            if any(not p for p in paths):
                raise ValueError('%s UAV%s 起降区删点后无扫描点' % (sid, uid))
            changed = paths != before
            if changed and len(layers) == 2:
                # 从两层已有区外公共点选择新首点，保留所有区外点，不添加扫描点。
                allowed = safe.intersection(Polygon(task['polygon_m'], holes=task['polygon_holes_m']).buffer(1e-6))
                options = []
                for start in paths[0]:
                    if start not in paths[1]:
                        continue
                    upper_orders = _orders(paths[0], start, allowed)
                    lower_orders = _orders(paths[1], start, allowed)
                    index = original['waypoints_m'].index(start)
                    home_route = old_vehicles[uid]['return_paths_m'][index]
                    for upper in upper_orders:
                        for lower in lower_orders:
                            cost = 2 * (length(upper) + length(lower) + length(home_route))
                            options.append((cost, upper, lower))
                if not options:
                    raise ValueError('%s UAV%s 缺少合法的两层共用区外首点' % (sid, uid))
                _, upper, lower = min(options)
                paths = [upper, lower]
            retained[uid] = [original['waypoints_m'].index(p)+1 for p in paths[0]]
            if changed:
                changes[uid] = dict(removed_by_layer={layer['layer_id']: len(old)-len(new)
                    for layer, old, new in zip(layers, before, paths)},
                    previous_first_wgs84=original['waypoints_wgs84'][0])
                for layer, points in zip(layers, paths):
                    # 使用原 GPS 数值，不重新投影/舍入其他扫描点。
                    lookup = {tuple(p): g for p, g in zip(layer['waypoints_m'], layer['waypoints_wgs84'])}
                    layer.update(waypoints_m=points, waypoints_wgs84=[lookup[tuple(p)] for p in points])
                    if task.get('layers'):
                        layer.update(waypoint_ids=['UAV%s_%s_%03d' % (uid, layer['layer_id'], i+1) for i in range(len(points))],
                            scan_execution_order=list(range(len(points)))+list(reversed(range(len(points)))))
                task.update(waypoints_m=layers[0]['waypoints_m'], waypoints_wgs84=layers[0]['waypoints_wgs84'],
                    scan_waypoints_m=[p for points in paths for p in points], scan_count=len(paths[0]),
                    scan_visit_count=sum(layer['scan_passes']*len(p) for layer, p in zip(layers, paths)),
                    normal_return_source=dict(source_uav_id=int(uid), waypoint_index=1), fixed_plan_revision=REVISION)
                if 'scan_execution_order' in task:
                    task['scan_execution_order'] = list(range(len(paths[0])))+list(reversed(range(len(paths[0]))))
                task['hover_scan_time_s'] = task['scan_visit_count'] * task['hover_scan_seconds']
                if task.get('layers'):
                    task['layer_transition'].update(from_waypoint_id=layers[0]['waypoint_ids'][0],
                                                    to_waypoint_id=layers[1]['waypoint_ids'][0])
                changes[uid]['new_first_wgs84'] = task['waypoints_wgs84'][0]
            for layer in layers:
                disks[layer['layer_id']].extend(Point(p).buffer(layer['reconnaissance_radius_m']) for p in layer['waypoints_m'])
            geometry['vehicles'][uid]['waypoints_m'] = task['waypoints_m']

        # 每台接收机都持有六机航点返航目录，必须一起删除旧点并重标来源索引。
        fleet = {uid: [[lon, lat] for lat, lon in item['task']['waypoints_wgs84']]
                 for uid, item in plan['planned_uavs'].items()}
        def remap(routes):
            lookup = {(str(r['source_uav_id']), r['waypoint_index']): r for r in routes}
            return [dict(copy.deepcopy(lookup[(uid, old)]), waypoint_index=index+1)
                    for uid, indexes in retained.items() for index, old in enumerate(indexes)]

        for uid, item in plan['planned_uavs'].items():
            task = item['task']; vehicle = geometry['vehicles'][uid]
            task['transit_routes'].update(waypoints_by_uav=fleet,
                return_paths=remap(old_tasks[uid]['transit_routes']['return_paths']))
            vehicle['fleet_return_paths_m'] = remap(old_vehicles[uid]['fleet_return_paths_m'])
            vehicle['return_paths_m'] = [r['path'] for r in vehicle['fleet_return_paths_m'] if r['source_uav_id'] == int(uid)]
            if uid not in changes:
                continue
            returning = vehicle['return_paths_m'][0]
            entry = list(reversed(returning))
            return_gps = next(r['path'] for r in task['transit_routes']['return_paths']
                              if r['source_uav_id'] == int(uid) and r['waypoint_index'] == 1)
            task['transit_routes']['entry_path'] = list(reversed(return_gps))
            task['entry_path_m'] = entry; vehicle['entry_path_m'] = entry
            paths = [l['waypoints_m'] for l in task['layers']] if task.get('layers') else [task['waypoints_m']]
            scans = [p for points in paths for p in points + list(reversed(points))]
            task['flight_path_m'] = entry + scans + returning[1:]
            task['route_distance_m'] = length(entry) + 2*sum(map(length, paths)) + length(returning)
            vertical = item['target_altitude_m'] / task['takeoff_speed_mps']
            if task.get('layers'):
                height_delta = task['layers'][0]['altitude_m'] - task['layers'][1]['altitude_m']
                vertical += height_delta/task['layer_transition']['descent_speed_mps'] + height_delta/task['takeoff_speed_mps']
                task['timing_components'].update(horizontal_flight_seconds=task['route_distance_m']/task['speed_mps'],
                                                 scan_seconds=task['hover_scan_time_s'])
            task['mission_time_s'] = vertical + task['route_distance_m']/task['speed_mps'] + task['hover_scan_time_s']

        coverage = area['coverage']
        for label, values in disks.items():
            union = unary_union(values).intersection(required)
            summed = sum(d.intersection(required).area for d in values)
            coverage['layer_metrics'][label] = dict(effective_area_m2=required.area, covered_area_m2=union.area,
                uncovered_area_m2=required.area-union.area, coverage_ratio=union.area/required.area,
                independent_scan_count=len(values), redundant_area_m2=summed-union.area,
                redundancy_ratio=(summed-union.area)/summed if summed else 0.)
        coverage.update(coverage_ratio=unary_union(disks['upper']+disks['lower']).intersection(required).area/required.area,
            total_scan_count=sum(len(u['task']['scan_waypoints_m']) for u in plan['planned_uavs'].values()),
            total_scan_visit_count=sum(u['task']['scan_visit_count'] for u in plan['planned_uavs'].values()),
            total_distance_m=sum(u['task']['route_distance_m'] for u in plan['planned_uavs'].values()),
            maximum_completion_time_s=max(u['task']['mission_time_s'] for u in plan['planned_uavs'].values()),
            total_mission_time_s=sum(u['task']['mission_time_s'] for u in plan['planned_uavs'].values()))
        plan['prepared_plan']['takeoff_area_exemption'] = dict(revision=REVISION, affected_uavs=changes,
            vertices_wgs84=VERTICES_WGS84, area_m2=exempt.area,
            policy='omit_scan_centers_allow_transit_and_incidental_scan_overlap')
