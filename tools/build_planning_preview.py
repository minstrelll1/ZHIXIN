"""用当前固定运行数据生成时间轴式三场景预览，不重新规划或修改任务。"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path

from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/planning_results_v2/planning_results_v2/planning_result.json'
FIXED = ROOT / 'competition_backend/competition_backend/final_scenes_prepared.json'
TEMPLATE = ROOT / 'tools/templates/planning_preview.html'


def build_data():
    archive = json.loads(SOURCE.read_text(encoding='utf8'))
    fixed = json.loads(FIXED.read_text(encoding='utf8'))
    canonical = json.dumps(fixed['scenes'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if hashlib.sha256(canonical.encode()).hexdigest() != fixed['sha256']:
        raise ValueError('固定运行数据摘要不符')
    document = dict(schema_version=1, contract_type='competition_multilayer_planning_handoff',
        plan_id='runtime-'+fixed['sha256'][:16], source_inputs_sha256=fixed['sha256'],
        compatibility='current_ground_onboard_contract', scenes=[])
    catalog = dict(plan_id=document['plan_id'], scenes={})
    for original in archive['scenes']:
        sid = original['scene_id']
        runtime = fixed['scenes'][sid]
        plan, geometry = runtime['plan'], runtime['transit_geometry']
        area = plan['search_area']
        scene = {k: copy.deepcopy(original[k]) for k in ('scene_id', 'display_name', 'coordinate_frame',
            'local_frame', 'projection', 'boundary_geometry_wgs84', 'reference_home', 'exclusions',
            'effective_area_geometry_wgs84', 'pending_confirmations')}
        scene.update(status='runtime_fixed', mission_budget_seconds=area.get('mission_budget_seconds'),
                     execution_mode=plan['planned_uavs']['1']['task']['execution_mode'], uavs=[])
        p = scene['projection']
        def to_local(lon, lat, z=None):
            return ((lat-p['latitude'])*p['north_m_per_degree'], -(lon-p['longitude'])*p['west_m_per_degree'])
        def to_gps(north, west, z=None):
            return (p['longitude']-west/p['west_m_per_degree'], p['latitude']+north/p['north_m_per_degree'])
        required = transform(to_local, shape(scene['effective_area_geometry_wgs84']))
        scene_disks = {'upper': [], 'lower': []}
        catalog['scenes'][sid] = {}
        def coverage(disks, region):
            union = unary_union(disks).intersection(region)
            summed = sum(d.intersection(region).area for d in disks)
            return dict(effective_area_m2=region.area, covered_area_m2=union.area,
                uncovered_area_m2=region.area-union.area, coverage_ratio=union.area/region.area if region.area else 0.,
                independent_scan_count=len(disks), redundancy_ratio=(summed-union.area)/summed if summed else 0.,
                uncovered_geometry_wgs84=mapping(transform(to_gps, region.difference(union))))
        for uid, item in plan['planned_uavs'].items():
            task = item['task']; vehicle = geometry['vehicles'][uid]
            region = Polygon(task['polygon_m'], holes=task.get('polygon_holes_m', []))
            layers = copy.deepcopy(task.get('layers'))
            if not layers:
                layers = [dict(layer_id=task['physical_layer'], altitude_m=item['target_altitude_m'],
                    reconnaissance_radius_m=task['reconnaissance_radius_m'], speed_mps=task['speed_mps'],
                    hover_scan_seconds=task['hover_scan_seconds'], scan_passes=task['scan_passes'],
                    waypoints_m=task['waypoints_m'], waypoints_wgs84=task['waypoints_wgs84'],
                    waypoint_ids=['UAV%s_%s_%03d' % (uid, task['physical_layer'], i+1) for i in range(len(task['waypoints_m']))])]
            uav = dict(uav_id=int(uid), partition_group=task['partition_group'],
                region_geometry_wgs84=mapping(transform(to_gps, region)), layers=layers, segments=[])
            segments = uav['segments']; cursor = 0.
            def segment(phase, a, b, speed=None, seconds=None, layer=None, waypoint=None, pass_index=None):
                nonlocal cursor
                horizontal = math.dist(a[:2], b[:2]); vertical = abs(a[2]-b[2])
                duration = seconds if seconds is not None else math.dist(a, b)/speed
                if not duration:
                    return
                segments.append(dict(segment_id='UAV%s_S%04d' % (uid, len(segments)+1), phase=phase,
                    layer_id=layer, waypoint_id=waypoint, pass_index=pass_index,
                    from_nwz_m=list(a), to_nwz_m=list(b), start_s=cursor, end_s=cursor+duration,
                    duration_s=duration, horizontal_distance_m=horizontal, vertical_distance_m=vertical, speed_mps=speed))
                cursor += duration
            def path(phase, points, height, speed=5., layer=None):
                for a, b in zip(points, points[1:]):
                    segment(phase, [*a, height], [*b, height], speed=speed, layer=layer)
            upper = layers[0]; home = vehicle['entry_path_m'][0]; height = upper['altitude_m']
            segment('takeoff', [*home, 0.], [*home, height], speed=task.get('takeoff_speed_mps', 6.))
            path('entry', vehicle['entry_path_m'], height)
            position = [*upper['waypoints_m'][0], height]
            for li, layer in enumerate(layers):
                altitude = layer['altitude_m']; points = layer['waypoints_m']; label = layer['layer_id']
                if li:
                    down = [*position[:2], altitude]
                    segment('layer_transition', position, down, speed=task['layer_transition']['descent_speed_mps'])
                    position = down
                order = list(range(len(points)))
                if layer['scan_passes'] == 2:
                    order += list(reversed(range(len(points))))
                layer['execution_order'] = [layer['waypoint_ids'][i] for i in order]
                for visit, index in enumerate(order):
                    point = [*points[index], altitude]
                    segment('layer_transition' if li and visit == 0 else 'scan_transit', position, point,
                            speed=layer['speed_mps'], layer=label)
                    segment('scan', point, point, seconds=layer['hover_scan_seconds'], layer=label,
                            waypoint=layer['waypoint_ids'][index], pass_index=visit//len(points)+1)
                    position = point
                disks = [Point(v).buffer(layer['reconnaissance_radius_m']) for v in points]
                layer['coverage'] = coverage(disks, required.intersection(region))
                scene_disks[label].extend(disks)
            start_return = cursor
            climb_start = list(position); raised = [*position[:2], height]
            climb_speed = task.get('takeoff_speed_mps', 6.)
            segment('return', position, raised, speed=climb_speed)
            choice = task['normal_return_source']
            returning = next(r['path'] for r in vehicle['fleet_return_paths_m']
                if r['source_uav_id'] == choice['source_uav_id'] and r['waypoint_index'] == choice['waypoint_index'])
            connection = [raised[:2], returning[0]]
            path('return', connection, height)
            path('return', returning, height)
            if not math.isclose(cursor, task['mission_time_s'], abs_tol=.02):
                raise ValueError('%s UAV%s 时间轴与固定方案估时不一致：%s / %s' % (sid, uid, cursor, task['mission_time_s']))
            budget = scene['mission_budget_seconds']
            uav['timing'] = dict(mission_time_s=cursor, mission_limit_s=None, mission_budget_seconds=budget,
                remaining_s=budget-cursor if budget is not None else None, within_limit=budget is None or cursor<=budget)
            scene['uavs'].append(uav)
            catalog['scenes'][sid][uid] = dict(recipient_return_altitude_m=height,
                return_paths=task['transit_routes']['return_paths'], normal_return_connection=dict(
                    return_climb_s=abs(climb_start[2]-height)/climb_speed, return_climb_mps=climb_speed,
                    climb_start_nwz_m=climb_start, climb_end_nwz_m=raised,
                    source_uav_id=choice['source_uav_id'], waypoint_index=choice['waypoint_index'],
                    source_physical_layer=upper['layer_id'],
                    connection_path=dict(horizontal_distance_m=math.dist(*connection)),
                    return_route_distance_m=sum(math.dist(a,b) for a,b in zip(returning,returning[1:])),
                    total_return_time_s=cursor-start_return))
        unions = {k: unary_union(v).intersection(required) for k,v in scene_disks.items()}
        scene['scene_metrics'] = dict(
            **{k: coverage(v, required) for k,v in scene_disks.items()},
            both_layers_coverage_ratio=unions['upper'].intersection(unions['lower']).area/required.area,
            either_layer_coverage_ratio=unions['upper'].union(unions['lower']).area/required.area,
            maximum_completion_time_s=max(u['timing']['mission_time_s'] for u in scene['uavs']),
            total_aircraft_time_s=sum(u['timing']['mission_time_s'] for u in scene['uavs']),
            all_aircraft_within_limit=all(u['timing']['within_limit'] for u in scene['uavs']))
        document['scenes'].append(scene)
    return document, catalog


def main():
    data, catalog = build_data()
    template = TEMPLATE.read_text(encoding='utf8')
    html = template.replace('__PLANNING_DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    html = html.replace('__RETURN_DATA__', json.dumps(catalog, ensure_ascii=False, separators=(',', ':')))
    for path in (ROOT/'docs/competition_plans_3d.html', ROOT/'docs/planning_preview.html', SOURCE.with_name('planning_preview.html')):
        legacy = Path(os.path.relpath(ROOT/'docs/competition_plans_legacy_3d.html', path.parent)).as_posix()
        path.write_text(html.replace('__LEGACY_URL__', legacy), encoding='utf8')
        print('Updated', path)
    (ROOT/'docs/planning_preview_data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'))+'\n', encoding='utf8')


if __name__ == '__main__':
    main()
