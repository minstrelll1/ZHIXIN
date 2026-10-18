#!/usr/bin/env python3
"""Build offline all-source return routes and an explicit existing-topic mapping.

No ROS connection, publishing, mission ID, or assignment checksum is created.
Route generation shares the planner visibility graph; validate_return_catalog.py
recomputes all geometry independently from the serialized files.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def length(points):
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(points, points[1:]))


def dedup(points):
    out = []
    for point in points:
        point = list(point)
        if not out or point != out[-1]:
            out.append(point)
    return out if len(out) > 1 else out * 2


def geometry(path_m, altitude, projection):
    nwz = [list(point[:2]) + [altitude] for point in path_m]
    return dict(path_nwz_m=nwz, path_lon_lat_alt=[[
        projection['longitude'] - q[1] / projection['west_m_per_degree'],
        projection['latitude'] + q[0] / projection['north_m_per_degree'], q[2]
    ] for q in nwz], horizontal_distance_m=length(nwz))


def route_via(router, start, gates):
    out = [list(start)]
    for gate in gates:
        out.extend(list(point) for point in router.route(out[-1], gate)[1:])
    return dedup(out)


def layer_sources(scene):
    fixed = scene['execution_mode'] == 'fixed_layer_forward_reverse'
    return [(u, u['layers'][0] if fixed else next(l for l in u['layers'] if l['layer_id'] == 'upper')) for u in scene['uavs']]


def topic_mapping(plan, catalog):
    out = dict(mapping_schema_version=1, artifact_type='offline_existing_topic_mapping',
        proposed_only=True, publishes_messages=False, planning_result_sha256=catalog['planning_result_sha256'],
        interface_source='inputs/revision/当前程序B双层接口说明.txt',
        status='offline_geometry_and_mapping_only_runtime_import_and_execution_not_tested',
        main_schema_policy='Main schema remains version 1; external_program_b_proposal.ros_topics remains null.',
        runtime_identifiers='Actual mission_id, assignment_checksum and runtime task hash must be produced by the original assignment program; none are invented here.',
        scenes={})
    for scene in plan['scenes']:
        sid = scene['scene_id']; fixed = scene['execution_mode'] == 'fixed_layer_forward_reverse'
        recs = {}
        for uav in scene['uavs']:
            uid = uav['uav_id']; base = '/ground_mission_planner/vehicle_%d/' % uid
            layers = {l['layer_id']: l for l in uav['layers']}
            first = uav['layers'][0] if fixed else layers['upper']
            second = None if fixed else layers['lower']
            rec = catalog['scenes'][sid][str(uid)]
            stages = {}
            for stage, layer in [('path_stage_1', first), ('path_stage_2', second)]:
                stages[stage] = dict(topic=base + stage, message_type='std_msgs/Float64MultiArray',
                    layout=dict(dim=[], data_offset=0), physical_layer=layer['layer_id'] if layer else None,
                    waypoint_ids=layer['waypoint_ids'] if layer else [],
                    data=[value for point in layer['waypoints_lon_lat_alt'] for value in point] if layer else [],
                    scan_passes=layer['scan_passes'] if layer else 0,
                    hover_scan_seconds=layer['hover_scan_seconds'] if layer else 0,
                    point_order='Unique scan points in transmitted order; do not send expanded execution_order and then repeat again.')
            task = dict(polygon_m=uav['polygon_m'], waypoints_m=first['waypoints_m'],
                waypoints_wgs84=first['waypoints_wgs84'], reconnaissance_radius_m=first['reconnaissance_radius_m'],
                hover_scan_seconds=first['hover_scan_seconds'], scan_passes=first['scan_passes'])
            if not fixed:
                task.update(execution_mode='upper_once_then_lower_once', layer_order=['upper', 'lower'],
                    layers=[{key: layer[key] for key in ['layer_id', 'altitude_m', 'waypoints_m', 'waypoints_wgs84', 'scan_passes', 'hover_scan_seconds']} for layer in [first, second]])
            payload = dict(schema_version=2, uav_id=uid, coordinate_frame='WGS84',
                coordinate_order='longitude_latitude_relative_altitude', altitude_frame='RELATIVE_TO_TAKEOFF',
                route_scope='all_uav_waypoints', waypoint_layer=rec['waypoint_layer'],
                return_altitude_m=rec['recipient_return_altitude_m'], return_policy=rec['return_policy'],
                landing_point=[uav['reference_home']['longitude'], uav['reference_home']['latitude'], rec['recipient_return_altitude_m']],
                waypoint_counts_by_uav=rec['waypoint_counts_by_uav'],
                routes=[dict(source_uav_id=r['source_uav_id'], source_physical_layer=r['source_physical_layer'],
                    waypoint_index=r['waypoint_index'], waypoint_id=r['waypoint_id'], path=r['path_lon_lat_alt']) for r in rec['return_paths']])
            recs[str(uid)] = dict(recipient_uav_id=uid, offline_execution_mode=scene['execution_mode'],
                runtime_execution_mode='legacy_single_layer' if fixed else 'upper_once_then_lower_once',
                physical_layers=list(layers), path_stage_1=stages['path_stage_1'], path_stage_2=stages['path_stage_2'],
                task_proposal=task, task_layers_policy='OMIT: existing task.layers validator accepts only a true upper+lower pair.' if fixed else 'Use exactly upper then lower, never an incomplete pair.',
                entry_path=dict(topic=base + 'entry_path', message_type='std_msgs/String', catalog_reference='scenes/%s/%d/entry_path' % (sid, uid)),
                return_paths=dict(topic=base + 'return_paths', message_type='std_msgs/String', geometry_payload_only=payload,
                    compatibility='Scene 1 assigned_physical_layer catalog needs explicit legacy adapter; not a directly importable runtime payload.' if fixed else 'Existing documented schema 2 geometry; runtime task identifiers and tested B integration are still required.'),
                jiangluodian=dict(topic=base + 'jiangluodian', message_type='std_msgs/Float64MultiArray', data=payload['landing_point']),
                external_mission=dict(topic='/uav%d/competition/external_mission' % uid, message_type='std_msgs/String',
                    note='The original program must create the actual runtime mission envelope. Scene 1 physical lower may map to the legacy first-stage interface named upper; this does not change its physical altitude or radius.' if fixed else 'Use subject2 and upper/lower layer metadata consistent with both stage arrays; runtime identifiers originate from assignment.'),
                return_catalog_reference='scenes/%s/%d' % (sid, uid),
                compatibility_limitations=['No runtime import, publishing or deployed B behavior has been tested.',
                    'Actual takeoff GPS, provisional heights and return climb speed still require confirmation.'] +
                    (['A single physical lower layer must occupy path_stage_1; path_stage_2 is explicitly empty and task.layers is absent.'] if fixed else []))
        out['scenes'][sid] = recs
    return out


def build_schema():
    def obj(properties, required=None):
        return dict(type='object', properties=properties, required=list(properties) if required is None else required, additionalProperties=False)
    num = {'type': 'number'}; positive = {'type': 'number', 'exclusiveMinimum': 0}; nonnegative = {'type': 'number', 'minimum': 0}
    text = {'type': 'string'}; integer = {'type': 'integer', 'minimum': 1, 'maximum': 6}
    point = dict(type='array', items=num, minItems=3, maxItems=3)
    point2 = dict(type='array', items=num, minItems=2, maxItems=2)
    path = dict(type='array', items=point, minItems=2)
    geomprops = dict(path_nwz_m=path, path_lon_lat_alt=path, horizontal_distance_m=nonnegative)
    home = obj(dict(latitude=num, longitude=num, home_nw_m=point2,
        source={'const': 'nominal_reference_home'}, is_nominal={'const': True}))
    route = obj(dict(source_uav_id=integer, source_physical_layer={'enum': ['upper', 'lower']},
        waypoint_index={'type': 'integer', 'minimum': 1}, waypoint_id=text, **geomprops))
    normal = obj(dict(source_uav_id=integer, source_physical_layer={'enum': ['upper', 'lower']},
        waypoint_index={'type': 'integer', 'minimum': 1}, waypoint_id=text,
        last_scan_waypoint_id=text, climb_start_nwz_m=point, climb_end_nwz_m=point,
        return_climb_mps={'anyOf': [positive, {'type': 'null'}]},
        return_climb_status={'enum': ['planner_provisional_unconfirmed', 'not_required']},
        return_climb_s=nonnegative, connection_path=obj(geomprops), return_route_distance_m=nonnegative,
        total_return_horizontal_distance_m=nonnegative, total_return_time_s=nonnegative,
        sensitivity_climb_speed_1_2_3_mps=obj({str(v): obj(dict(return_time_s=nonnegative, mission_time_s=nonnegative, within_1380_s={'type': 'boolean'})) for v in [1, 2, 3]})))
    rec = obj(dict(recipient_uav_id=integer, coordinate_frame={'const': 'WGS84'},
        local_frame={'const': 'LOCAL_NORTH_WEST'}, coordinate_order={'const': 'longitude_latitude_relative_altitude'},
        altitude_frame={'const': 'RELATIVE_TO_RECIPIENT_TAKEOFF'},
        runtime_schema_version={'const': 2}, route_scope={'const': 'all_uav_waypoints'},
        waypoint_layer={'enum': ['upper', 'assigned_physical_layer']},
        return_policy={'enum': ['climb_to_upper_then_return', 'at_task_altitude']},
        recipient_return_altitude_m=positive, reference_home=home, entry_path=obj(geomprops),
        retained_return_gate_points_m={'type': 'array', 'items': point2},
        waypoint_counts_by_uav=obj({str(i): {'type': 'integer', 'minimum': 1} for i in range(1, 7)}),
        return_paths={'type': 'array', 'items': route, 'minItems': 6}, normal_return_connection=normal,
        status={'const': 'offline_geometry_requires_runtime_adapter'}, limitations={'type': 'array', 'items': text} ))
    recipients = obj({str(i): {'$ref': '#/$defs/recipient'} for i in range(1, 7)})
    schema = obj(dict(catalog_schema_version={'const': 1}, artifact_type={'const': 'offline_return_catalog'},
        plan_id=text, source_inputs_sha256={'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        planning_result_sha256={'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        return_selections_sha256={'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        proposed_only={'const': True}, publishes_messages={'const': False},
        scenes=obj({sid: recipients for sid in ['final_scene_1', 'final_scene_2', 'xuchang_small_2']})))
    schema.update({'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'urn:planning:offline-return-catalog:1',
        'title': 'Offline all-source recipient-height return catalog; not a runtime mission', '$defs': {'recipient': rec}})
    return schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'inputs/02_规划输入_v2.json')
    parser.add_argument('--plan', type=Path, default=ROOT / 'planning_result.json')
    parser.add_argument('--exclusions', type=Path, default=ROOT / 'exclusions_draft.json')
    parser.add_argument('--selections', type=Path, default=ROOT / 'return_selections.json')
    parser.add_argument('--output-dir', type=Path, default=ROOT)
    parser.add_argument('--schema-only', action='store_true')
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    dump(args.output_dir / 'return_catalog.schema.json', build_schema())
    if args.schema_only:
        return
    import planner
    inp = json.loads(args.input.read_text(encoding='utf-8'))
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    exc = json.loads(args.exclusions.read_text(encoding='utf-8'))
    selections = json.loads(args.selections.read_text(encoding='utf-8'))['scenes']
    sites = {key: planner.make_site(inp, key == 'final_field', exc) for key in ['final_field', 'xuchang_field']}
    catalog = dict(catalog_schema_version=1, artifact_type='offline_return_catalog', plan_id=plan['plan_id'],
        source_inputs_sha256=plan['source_inputs_sha256'], planning_result_sha256=digest(args.plan),
        return_selections_sha256=digest(args.selections), proposed_only=True, publishes_messages=False, scenes={})
    for scene in plan['scenes']:
        sid = scene['scene_id']; fixed = scene['execution_mode'] == 'fixed_layer_forward_reverse'
        site = sites['xuchang_field' if sid == 'xuchang_small_2' else 'final_field']
        projection = scene['projection']; router = site['router']; sources = layer_sources(scene)
        counts = {str(u['uav_id']): len(l['waypoints_m']) for u, l in sources}; recs = {}
        for uav in scene['uavs']:
            uid = uav['uav_id']; altitude = uav['layers'][0]['altitude_m']
            home = uav['reference_home']['home_nw_m']; selection = selections[sid][str(uid)]
            gates = [home] if sid != 'xuchang_small_2' else inp['xuchang_field']['baseline_uavs'][uid - 1]['old_return_path_from_last_waypoint_m'][1:]
            routes = []
            for source, layer in sources:
                for index, (point, waypoint_id) in enumerate(zip(layer['waypoints_m'], layer['waypoint_ids']), 1):
                    route = route_via(router, point, gates)
                    if source['uav_id'] == selection['source_uav_id'] and index == selection['waypoint_index']:
                        if waypoint_id != selection['waypoint_id']:
                            raise ValueError('Selected waypoint ID does not match final array')
                        route = selection['return_path_m']
                    routes.append(dict(source_uav_id=source['uav_id'], source_physical_layer=layer['layer_id'],
                        waypoint_index=index, waypoint_id=waypoint_id, **geometry(route, altitude, projection)))
            entry = [s for s in uav['segments'] if s['phase'] == 'entry']
            entry_path = [entry[0]['from_nwz_m'][:2]] + [s['to_nwz_m'][:2] for s in entry] if entry else [home, home]
            scans = [s for s in uav['segments'] if s['phase'] == 'scan']; last = scans[-1]
            climb_start = last['to_nwz_m']; climb_end = climb_start[:2] + [altitude]
            selected_route = next(r for r in routes if r['source_uav_id'] == selection['source_uav_id'] and r['waypoint_index'] == selection['waypoint_index'])
            connection = geometry(selection['connection_path_m'], altitude, projection)
            climb_time = selection['return_climb_s']; horizontal = connection['horizontal_distance_m'] + selected_route['horizontal_distance_m']
            return_time = climb_time + horizontal / 5
            if abs(return_time - uav['timing']['return_s']) > 1e-5:
                raise ValueError('Normal return catalog time differs from final segments for %s UAV%d' % (sid, uid))
            sensitivity = {}
            for speed in [1, 2, 3]:
                alt_time = abs(climb_end[2] - climb_start[2]) / speed
                total = uav['timing']['mission_time_s'] - climb_time + alt_time
                sensitivity[str(speed)] = dict(return_time_s=return_time - climb_time + alt_time,
                    mission_time_s=total, within_1380_s=total <= 1380)
            normal = dict(source_uav_id=selection['source_uav_id'], source_physical_layer=selected_route['source_physical_layer'],
                waypoint_index=selection['waypoint_index'], waypoint_id=selection['waypoint_id'], last_scan_waypoint_id=last['waypoint_id'],
                climb_start_nwz_m=climb_start, climb_end_nwz_m=climb_end,
                return_climb_mps=selection['return_climb_mps'], return_climb_status='not_required' if fixed else 'planner_provisional_unconfirmed',
                return_climb_s=climb_time, connection_path=connection, return_route_distance_m=selected_route['horizontal_distance_m'],
                total_return_horizontal_distance_m=horizontal, total_return_time_s=return_time,
                sensitivity_climb_speed_1_2_3_mps=sensitivity)
            recs[str(uid)] = dict(recipient_uav_id=uid, coordinate_frame='WGS84', local_frame='LOCAL_NORTH_WEST',
                coordinate_order='longitude_latitude_relative_altitude', altitude_frame='RELATIVE_TO_RECIPIENT_TAKEOFF',
                runtime_schema_version=2, route_scope='all_uav_waypoints', waypoint_layer='assigned_physical_layer' if fixed else 'upper',
                return_policy='at_task_altitude' if fixed else 'climb_to_upper_then_return', recipient_return_altitude_m=altitude,
                reference_home=uav['reference_home'], entry_path=geometry(entry_path, altitude, projection),
                retained_return_gate_points_m=gates[:-1], waypoint_counts_by_uav=counts, return_paths=routes,
                normal_return_connection=normal, status='offline_geometry_requires_runtime_adapter',
                limitations=['Routes use nominal reference homes, not measured aircraft home GPS.',
                    'All source points use the recipient altitude; no dynamic separation or actual B execution has been verified.',
                    'Scene 1 assigned_physical_layer catalog and legacy first-stage mapping require explicit integration.' if fixed else
                    'Return climb speed is a planner estimate, not user-confirmed; 1/2/3 m/s sensitivity is provided.'])
        catalog['scenes'][sid] = recs
        print(sid, 'routes_per_recipient', sum(counts.values()), 'total_routes', sum(counts.values()) * 6, flush=True)
    dump(args.output_dir / 'return_catalog.json', catalog)
    dump(args.output_dir / 'topic_mapping.json', topic_mapping(plan, catalog))
    print('Wrote return_catalog.json, return_catalog.schema.json and topic_mapping.json', flush=True)


if __name__ == '__main__':
    main()
