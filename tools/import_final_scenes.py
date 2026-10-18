"""赛前将 v2 交接 ZIP 转为固定运行数据；绝不在规划/分派请求中执行。"""
import argparse
import copy
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'competition_backend'))
from competition_backend.final_scenes import PATH, NAMES, altitude_profile, departure_for, digest


def compile_bundle(archive):
    with zipfile.ZipFile(archive) as z:
        def read(name):
            return z.read('planning_results_v2/' + name)
        manifest = json.loads(read('SHA256SUMS.json'))['files']
        for name in ('planning_result.json', 'return_catalog.json', 'return_selections.json',
                     'validation_report.json', 'return_catalog_validation_report.json'):
            if hashlib.sha256(read(name)).hexdigest() != manifest[name]:
                raise ValueError('交接包文件摘要不匹配：' + name)
        raw = read('planning_result.json')
        source = json.loads(raw)
        catalog = json.loads(read('return_catalog.json'))
        selections = json.loads(read('return_selections.json'))['scenes']
        for name in ('validation_report.json', 'return_catalog_validation_report.json'):
            report = json.loads(read(name))
            if not report.get('passed') or report.get('failures'):
                raise ValueError('交接包核验未通过：' + name)
        sha = hashlib.sha256(raw).hexdigest()
        if catalog['planning_result_sha256'] != sha:
            raise ValueError('航点与返航目录来源不一致')
        scenes = {}
        for scene in source['scenes']:
            sid, projection = scene['scene_id'], scene['projection']
            def local(p):
                return [(p[1] - projection['latitude']) * projection['north_m_per_degree'],
                        -(p[0] - projection['longitude']) * projection['west_m_per_degree']]
            def polygons(geometry):
                return [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
            def rings(geometry):
                return [[local(p) for p in poly[0][:-1]] for poly in polygons(geometry)]
            boundary = rings(scene['boundary_geometry_wgs84'])[0]
            home = scene['reference_home']
            holes, forbidden, terrain = [], [], []
            for exclusion in scene['exclusions']:
                target = holes if exclusion['flight_prohibited'] else forbidden if exclusion['scan_disk_intersection_forbidden'] else terrain
                target.extend(rings(exclusion['geometry_wgs84']))
            metrics = {label: {k: v for k, v in scene['scene_metrics'][label].items()
                               if not k.endswith('geometry_wgs84')} for label in ('upper', 'lower')}
            coverage = dict(projection=projection, speed_mps=5., hover_scan_seconds=50.,
                            scan_passes=2 if sid == 'final_scene_1' else 1,
                            coverage_ratio=scene['scene_metrics']['either_layer_coverage_ratio'],
                            layer_metrics=metrics, mission_time_limit_s=None)
            area = dict(flight_profile=sid, coordinate_mode='gps', coordinate_frame='LOCAL_NORTH_WEST',
                        points_m=boundary, points=[[p[1], p[0]] for p in scene['boundary_geometry_wgs84']['coordinates'][0][:-1]],
                        departure_point=departure_for(sid), departure_point_m=home['home_nw_m'],
                        gps_origin=dict(latitude=projection['latitude'], longitude=projection['longitude']),
                        landing_mode='onboard_home', boundary_clearance_m=5.,
                        home_reference_policy='first_valid_unarmed_gps_per_boot',
                        other_home_clearance_m=1.5, transit_constraints='soft_preferences',
                        excluded_polygons_m=holes, scan_exclusion_polygons_m=forbidden,
                        terrain_layers_m={'免侦察区': terrain}, coverage=coverage,
                        imported_plan_id=source['plan_id'], source_plan_sha256=sha,
                        source_status=scene['status'], source_pending_confirmations=scene['pending_confirmations'])
            area.update(departure_point_wgs84=dict(latitude=home['latitude'], longitude=home['longitude']),
                        origin_x_m=min(p[0] for p in boundary), origin_y_m=min(p[1] for p in boundary),
                        height_m=max(p[0] for p in boundary)-min(p[0] for p in boundary),
                        width_m=max(p[1] for p in boundary)-min(p[1] for p in boundary))
            area['terrain_layers_m'] = {'excluded': {'type': 'MultiPolygon',
                'coordinates': [[ring + [ring[0]]] for ring in terrain]}}
            planned, vehicles = {}, {}
            fleet = {str(u['uav_id']): [[p[1], p[0]] for p in u['layers'][0]['waypoints_wgs84']] for u in scene['uavs']}
            for uav in scene['uavs']:
                uid = uav['uav_id']; first = uav['layers'][0]; routes = catalog['scenes'][sid][str(uid)]
                selection = selections[sid][str(uid)]
                region = uav['region_geometry_wgs84']
                if region['type'] != 'Polygon':
                    raise ValueError('运行分区适配要求 Polygon')
                layers = [{k: copy.deepcopy(layer[k]) for k in ('layer_id', 'altitude_m', 'reconnaissance_radius_m',
                           'speed_mps', 'hover_scan_seconds', 'scan_passes', 'waypoint_ids', 'waypoints_m', 'waypoints_wgs84')}
                          for layer in uav['layers']]
                entry = [p[:2] for p in routes['entry_path']['path_lon_lat_alt']]
                returning = [dict(source_uav_id=r['source_uav_id'], waypoint_index=r['waypoint_index'],
                                  path=[p[:2] for p in r['path_lon_lat_alt']]) for r in routes['return_paths']]
                task = dict(type='lawnmower_search', sector=uid, coordinate_frame='LOCAL_NORTH_WEST',
                            polygon_m=uav['polygon_m'], polygon_holes_m=[[local(p) for p in ring[:-1]] for ring in region['coordinates'][1:]],
                            waypoints_m=first['waypoints_m'], waypoints_wgs84=first['waypoints_wgs84'],
                            reconnaissance_radius_m=first['reconnaissance_radius_m'], reconnaissance_mode='hover_scan',
                            speed_mps=5., external_speed_mps=5., hover_scan_seconds=50., scan_count=len(first['waypoints_m']),
                            scan_passes=first['scan_passes'], physical_layer=first['layer_id'],
                            execution_mode=scene['execution_mode'], partition_group=uav['partition_group'],
                            takeoff_speed_mps=6., timing_altitude_m=first['altitude_m'],
                            mission_time_s=uav['timing']['mission_time_s'], mission_time_limit_s=None,
                            entry_path_m=[p[:2] for p in routes['entry_path']['path_nwz_m']],
                            flight_path_m=[uav['segments'][0]['from_nwz_m'][:2]] + [s['to_nwz_m'][:2] for s in uav['segments']],
                            source_plan_sha256=sha,
                            normal_return_source=dict(source_uav_id=selection['source_uav_id'], waypoint_index=selection['waypoint_index']),
                            route_distance_m=sum(s['horizontal_distance_m'] for s in uav['segments']),
                            timing_reference='offline_nominal_home',
                            transit_routes=dict(schema_version=2, recipient_uav_id=uid, route_scope='all_uav_waypoints',
                                coordinate_frame='WGS84', coordinate_order='longitude_latitude', plan_sha256=sha,
                                boundary_clearance_m=5., endpoint_clearance_policy='takeoff_landing_connector_only',
                                waypoint_layer='upper' if len(layers) == 2 else 'assigned_physical_layer',
                                departure=[home['longitude'], home['latitude']], entry_path=entry,
                                waypoints_by_uav=fleet, return_paths=returning))
                if len(layers) == 2:
                    task.update(layers=layers, layer_order=['upper', 'lower'], layer_transition=uav['layer_transition'])
                else:
                    task.update(scan_pass_order='forward_then_reverse',
                                scan_execution_order=list(range(len(first['waypoints_m']))) + list(reversed(range(len(first['waypoints_m'])))))
                # Offline estimate is informational; no imported 23-minute budget is dispatched.
                task['scan_visit_count'] = sum(len(l['waypoints_m']) * l['scan_passes'] for l in layers)
                task['hover_scan_time_s'] = task['scan_visit_count'] * 50.
                task['scan_waypoints_m'] = [p for layer in layers for p in layer['waypoints_m']]
                task['bounds_m'] = dict(x_min=min(p[0] for p in task['polygon_m']), x_max=max(p[0] for p in task['polygon_m']),
                                        y_min=min(p[1] for p in task['polygon_m']), y_max=max(p[1] for p in task['polygon_m']))
                planned[str(uid)] = dict(uav_id=uid, target_altitude_m=first['altitude_m'], task=task)
                returns_m = [dict(source_uav_id=r['source_uav_id'], waypoint_index=r['waypoint_index'],
                                 path=[p[:2] for p in r['path_nwz_m']]) for r in routes['return_paths']]
                vehicles[str(uid)] = dict(waypoints_m=first['waypoints_m'], entry_path_m=task['entry_path_m'],
                                         fleet_return_paths_m=returns_m,
                                         return_paths_m=[r['path'] for r in returns_m if r['source_uav_id'] == uid])
            coverage.update(total_scan_count=sum(len(p['task']['scan_waypoints_m']) for p in planned.values()),
                            total_distance_m=sum(p['task']['route_distance_m'] for p in planned.values()),
                            total_scan_visit_count=sum(p['task']['scan_visit_count'] for p in planned.values()),
                            maximum_completion_time_s=max(p['task']['mission_time_s'] for p in planned.values()),
                            total_mission_time_s=sum(p['task']['mission_time_s'] for p in planned.values()))
            scenes[sid] = dict(plan=dict(subject='subject2', flight_altitude_plan=altitude_profile(sid),
                prepared_plan=dict(name=NAMES[sid], source='planning_results_v2', sha256=sha),
                prepared_transit_sha256=sha, search_area=area, planned_uavs=planned),
                transit_geometry=dict(boundary_m=boundary, departure_m=home['home_nw_m'], excluded_polygons_m=holes,
                                      boundary_clearance_m=5., vehicles=vehicles))
    return dict(schema_version=1, source_archive_sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
                source_plan_sha256=sha, sha256=digest(scenes), scenes=scenes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', nargs='?', default=str(ROOT / 'docs/planning_results_v2.zip'))
    args = parser.parse_args()
    data = compile_bundle(args.archive)
    PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print('Saved', PATH, 'sha256', data['sha256'])
