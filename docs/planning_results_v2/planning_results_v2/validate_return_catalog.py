#!/usr/bin/env python3
"""Independent serialized return-catalog, topic-array and normal-return validator.

No import from planner.py or build_return_catalog.py. Dependencies: Shapely,
jsonschema. Run after build_return_catalog.py. This verifies offline geometry
and mappings, not actual ROS delivery or behavior of a deployed program B.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator
from shapely.geometry import shape, LineString, Point
from shapely.ops import transform

ROOT = Path(__file__).resolve().parent


def read(path):
    def invalid(value):
        raise ValueError('Non-finite JSON constant: ' + value)
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def planar(g, p):
    def fn(lon, lat, z=None):
        try:
            return ([(v - p['latitude']) * p['north_m_per_degree'] for v in lat],
                [-(v - p['longitude']) * p['west_m_per_degree'] for v in lon])
        except TypeError:
            return ((lat - p['latitude']) * p['north_m_per_degree'],
                -(lon - p['longitude']) * p['west_m_per_degree'])
    return transform(fn, shape(g))


def separation(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def compact(path):
    out = []
    for p in path:
        if not out or separation(p, out[-1]) > 1e-9:
            out.append(p)
    return out


class Audit:
    def __init__(self):
        self.count = 0
        self.failures = []
        self.scene_rows = []
        self.min_outer = float('inf')
        self.min_nofly = float('inf')

    def check(self, passed, label, detail=None):
        self.count += 1
        if not passed:
            self.failures.append(dict(check=label, detail=detail))

    def close(self, a, b, label, tol=1e-6):
        self.check(abs(a - b) <= tol, label, dict(actual=a, expected=b, tolerance=tol))

    def same_path(self, a, b, label, tol=1e-7):
        self.check(len(a) == len(b), label + ' point count', dict(actual=len(a), expected=len(b)))
        if len(a) == len(b):
            self.check(all(len(x) == len(y) and separation(x, y) <= tol for x, y in zip(a, b)), label + ' coordinates')

    def legal(self, path, boundary, nofly, label):
        self.check(len(path) >= 1, label + ' nonempty point array')
        if len(path) == 1:
            path = path * 2
        for index, (a, b) in enumerate(zip(path, path[1:]), 1):
            ln = Point(a[:2]) if separation(a[:2], b[:2]) < 1e-12 else LineString([a[:2], b[:2]])
            outer = ln.distance(boundary.boundary)
            self.min_outer = min(self.min_outer, outer)
            self.check(boundary.covers(ln), label + '/segment%d inside boundary' % index)
            self.check(outer >= 5 - 1e-7, label + '/segment%d outer clearance >= 5m' % index, outer)
            for nf in nofly:
                distance = ln.distance(nf)
                self.min_nofly = min(self.min_nofly, distance)
                self.check(distance >= 5 - 1e-7, label + '/segment%d no-fly clearance >= 5m' % index, distance)

    def geometry(self, g, altitude, p, boundary, nofly, label):
        nw = g['path_nwz_m']; gps = g['path_lon_lat_alt']
        self.check(len(nw) == len(gps), label + ' GPS/local length')
        for i, (local, ll) in enumerate(zip(nw, gps)):
            self.close(local[2], altitude, label + ' recipient altitude %d' % i, 1e-9)
            recovered = [(ll[1] - p['latitude']) * p['north_m_per_degree'], -(ll[0] - p['longitude']) * p['west_m_per_degree'], ll[2]]
            self.check(separation(local, recovered) <= 1e-4, label + ' coordinate roundtrip %d' % i)
        length = sum(separation(a[:2], b[:2]) for a, b in zip(nw, nw[1:]))
        self.close(g['horizontal_distance_m'], length, label + ' horizontal length')
        self.legal(nw, boundary, nofly, label)
        return length


def validate(args):
    audit = Audit()
    plan = read(args.plan); catalog = read(args.catalog); schema = read(args.schema)
    inp = read(args.input); selections = read(args.selections)['scenes']; mapping = read(args.mapping)
    errors = sorted(Draft202012Validator(schema).iter_errors(catalog), key=lambda error: str(error.path))
    audit.check(not errors, 'return catalog schema', [str(error) for error in errors[:10]])
    if errors:
        return audit, plan, catalog
    audit.check(catalog['planning_result_sha256'] == sha(args.plan), 'final planning JSON SHA256')
    audit.check(catalog['return_selections_sha256'] == sha(args.selections), 'normal return selections SHA256')
    audit.check(catalog['source_inputs_sha256'] == plan['source_inputs_sha256'] == sha(args.input), 'new input SHA256')
    audit.check(catalog['plan_id'] == plan['plan_id'], 'plan ID')
    audit.check(mapping['planning_result_sha256'] == sha(args.plan), 'topic mapping final JSON SHA256')
    audit.check(set(catalog['scenes']) == set(s['scene_id'] for s in plan['scenes']), 'three required scenes')
    audit.check(set(mapping['scenes']) == set(catalog['scenes']), 'topic mapping scenes')

    def forbidden_keys(value, prefix='root'):
        if isinstance(value, dict):
            for key, child in value.items():
                audit.check(key not in ['mission_id', 'assignment_checksum'], prefix + ' no invented runtime identifier ' + key)
                forbidden_keys(child, prefix + '/' + key)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                forbidden_keys(child, prefix + '/%d' % i)
    forbidden_keys(catalog, 'catalog'); forbidden_keys(mapping, 'mapping')

    for scene in plan['scenes']:
        sid = scene['scene_id']; fixed = scene['execution_mode'] == 'fixed_layer_forward_reverse'; p = scene['projection']
        boundary = planar(scene['boundary_geometry_wgs84'], p)
        nofly = [planar(e['geometry_wgs84'], p) for e in scene['exclusions'] if e['flight_prohibited'] and e['geometry_wgs84']]
        sources = {u['uav_id']: (u['layers'][0] if fixed else next(l for l in u['layers'] if l['layer_id'] == 'upper')) for u in scene['uavs']}
        expected = [(uid, index + 1) for uid in sorted(sources) for index in range(len(sources[uid]['waypoints_m']))]
        recs = catalog['scenes'][sid]
        audit.check(set(recs) == set(map(str, range(1, 7))), sid + ' exact six recipients')
        row = dict(scene_id=sid, source_points=len(expected), routes_per_recipient=len(expected), total_routes=0, recipients=[])
        for uav in scene['uavs']:
            uid = uav['uav_id']; key = sid + '/UAV%d' % uid; rec = recs[str(uid)]
            altitude = uav['layers'][0]['altitude_m']; home = uav['reference_home']['home_nw_m'] + [altitude]
            audit.check(rec['recipient_uav_id'] == uid, key + ' recipient ID')
            audit.close(rec['recipient_return_altitude_m'], altitude, key + ' return altitude')
            audit.check(rec['reference_home'] == uav['reference_home'], key + ' reference home')
            audit.check(rec['waypoint_layer'] == ('assigned_physical_layer' if fixed else 'upper'), key + ' source-layer scope')
            audit.check(rec['return_policy'] == ('at_task_altitude' if fixed else 'climb_to_upper_then_return'), key + ' return policy')
            audit.check(rec['waypoint_counts_by_uav'] == {str(u): len(l['waypoints_m']) for u, l in sources.items()}, key + ' source counts')
            actual = [(r['source_uav_id'], r['waypoint_index']) for r in rec['return_paths']]
            audit.check(actual == expected, key + ' exact all-source coverage, indices, ordering and no duplicates')
            gates = [] if sid != 'xuchang_small_2' else inp['xuchang_field']['baseline_uavs'][uid - 1]['old_return_path_from_last_waypoint_m'][1:-1]
            audit.same_path(rec['retained_return_gate_points_m'], gates, key + ' inherited return gates')
            by_key = {}
            for route in rec['return_paths']:
                route_key = (route['source_uav_id'], route['waypoint_index']); by_key[route_key] = route
                label = key + '/source%d-point%d' % route_key
                source = sources[route['source_uav_id']]; index = route['waypoint_index'] - 1
                audit.check(route['waypoint_id'] == source['waypoint_ids'][index], label + ' waypoint ID')
                audit.check(route['source_physical_layer'] == source['layer_id'], label + ' source physical layer')
                start = source['waypoints_m'][index] + [altitude]
                audit.check(separation(route['path_nwz_m'][0], start) <= 1e-7, label + ' source endpoint at recipient altitude')
                audit.check(separation(route['path_nwz_m'][-1], home) <= 1e-7, label + ' recipient home endpoint')
                audit.geometry(route, altitude, p, boundary, nofly, label)
                pos = 0
                for gate in gates:
                    while pos < len(route['path_nwz_m']) and separation(route['path_nwz_m'][pos][:2], gate) > 1e-7:
                        pos += 1
                    audit.check(pos < len(route['path_nwz_m']), label + ' retained gate in prescribed order', gate)
                    pos += 1

            entry = [s for s in uav['segments'] if s['phase'] == 'entry']
            expected_entry = [entry[0]['from_nwz_m']] + [s['to_nwz_m'] for s in entry] if entry else [home, home]
            audit.same_path(rec['entry_path']['path_nwz_m'], expected_entry, key + ' entry from actual segments')
            audit.geometry(rec['entry_path'], altitude, p, boundary, nofly, key + '/entry')
            normal = rec['normal_return_connection']; choice = selections[sid][str(uid)]
            for field in ['source_uav_id', 'waypoint_index', 'waypoint_id', 'return_climb_mps', 'return_climb_s']:
                audit.check(normal[field] == choice[field], key + ' exact normal selection ' + field)
            selected = by_key[(normal['source_uav_id'], normal['waypoint_index'])]
            audit.check([v[:2] for v in selected['path_nwz_m']] == choice['return_path_m'], key + ' exact selected route geometry from planner')
            audit.check([v[:2] for v in normal['connection_path']['path_nwz_m']] == choice['connection_path_m'], key + ' exact connection geometry from planner')
            scans = [s for s in uav['segments'] if s['phase'] == 'scan']; last = scans[-1]
            audit.check(normal['last_scan_waypoint_id'] == last['waypoint_id'], key + ' final scan ID')
            audit.same_path([normal['climb_start_nwz_m'], normal['climb_end_nwz_m']], [last['to_nwz_m'], last['to_nwz_m'][:2] + [altitude]], key + ' in-place return climb')
            conn = normal['connection_path']['path_nwz_m']
            audit.check(separation(conn[0], normal['climb_end_nwz_m']) <= 1e-7, key + ' connection starts at upper point above last scan')
            audit.check(separation(conn[-1], selected['path_nwz_m'][0]) <= 1e-7, key + ' connection reaches selected catalog route')
            conn_length = audit.geometry(normal['connection_path'], altitude, p, boundary, nofly, key + '/normal_connection')
            audit.close(conn_length, choice['return_connection_distance_m'], key + ' selected connection length')
            audit.close(selected['horizontal_distance_m'], choice['return_route_distance_m'], key + ' selected route length')
            climb = abs(normal['climb_end_nwz_m'][2] - normal['climb_start_nwz_m'][2])
            if fixed:
                audit.close(climb, 0, key + ' no scene1 height change')
                audit.check(normal['return_climb_mps'] is None and normal['return_climb_status'] == 'not_required', key + ' no scene1 climb assumption')
            else:
                audit.close(climb, 13, key + ' 13m return climb')
                audit.check(normal['return_climb_mps'] == 2 and normal['return_climb_status'] == 'planner_provisional_unconfirmed', key + ' unconfirmed planner return climb at 2m/s')
            climb_time = 0 if fixed else climb / normal['return_climb_mps']
            audit.close(normal['return_climb_s'], climb_time, key + ' climb time')
            horizontal = conn_length + selected['horizontal_distance_m']; return_time = climb_time + horizontal / 5
            audit.close(normal['total_return_horizontal_distance_m'], horizontal, key + ' all return horizontal distance')
            audit.close(normal['return_route_distance_m'], selected['horizontal_distance_m'], key + ' route length metadata')
            audit.close(normal['total_return_time_s'], return_time, key + ' complete return time')
            audit.close(uav['timing']['return_s'], return_time, key + ' final time ledger includes climb, connection and return')
            actual_return = [s for s in uav['segments'] if s['phase'] == 'return']
            expected_path = compact([normal['climb_start_nwz_m'], normal['climb_end_nwz_m']] + conn + selected['path_nwz_m'])
            actual_path = compact([actual_return[0]['from_nwz_m']] + [s['to_nwz_m'] for s in actual_return])
            audit.same_path(actual_path, expected_path, key + ' exact normal return full segment geometry')
            for s in actual_return:
                dh = abs(s['to_nwz_m'][2] - s['from_nwz_m'][2]); dx = separation(s['to_nwz_m'][:2], s['from_nwz_m'][:2])
                expected_duration = dx / 5 + (dh / normal['return_climb_mps'] if dh > 1e-9 else 0)
                audit.close(s['duration_s'], expected_duration, key + '/' + s['segment_id'] + ' independently recomputed duration')
                audit.check(not (dh > 1e-9 and dx > 1e-9), key + '/' + s['segment_id'] + ' sequential vertical/horizontal model')
            audit.close(sum(s['duration_s'] for s in actual_return), return_time, key + ' return segment time sum')
            for speed in [1, 2, 3]:
                sensitivity = normal['sensitivity_climb_speed_1_2_3_mps'][str(speed)]
                alt_time = climb / speed; total = uav['timing']['mission_time_s'] - climb_time + alt_time
                audit.close(sensitivity['mission_time_s'], total, key + '/speed%d mission sensitivity' % speed)
                audit.close(sensitivity['return_time_s'], horizontal / 5 + alt_time, key + '/speed%d return sensitivity' % speed)
                audit.check(sensitivity['within_1380_s'] == (total <= 1380), key + '/speed%d budget sensitivity' % speed)

            # Reconstruct actual stage arrays from the separate mapping, not segments.
            mapped = mapping['scenes'][sid][str(uid)]; stages = [uav['layers'][0], None] if fixed else [sources[uid], next(l for l in uav['layers'] if l['layer_id'] == 'lower')]
            for stage, layer in zip(['path_stage_1', 'path_stage_2'], stages):
                supplied = mapped[stage]; expected_data = [v for q in layer['waypoints_lon_lat_alt'] for v in q] if layer else []
                audit.check(supplied['topic'] == '/ground_mission_planner/vehicle_%d/%s' % (uid, stage), key + ' documented topic ' + stage)
                audit.check(supplied['data'] == expected_data, key + ' actual flattened GPS array ' + stage)
                audit.check(supplied['physical_layer'] == (layer['layer_id'] if layer else None), key + ' physical/interface layer mapping ' + stage)
                audit.check(supplied['scan_passes'] == (layer['scan_passes'] if layer else 0), key + ' array scan passes ' + stage)
                audit.check(supplied['hover_scan_seconds'] == (50 if layer else 0), key + ' array scan seconds ' + stage)
                if layer:
                    audit.legal([q + [layer['altitude_m']] for q in layer['waypoints_m']], boundary, nofly, key + '/' + stage + ' actual direct transmitted edges')
                    audit.close(layer['reconnaissance_radius_m'], 50 if layer['layer_id'] == 'upper' else 30, key + ' revised physical radius ' + stage)
            if fixed:
                audit.check('layers' not in mapped['task_proposal'], key + ' legacy task omits task.layers')
                audit.check(mapped['path_stage_2']['data'] == [], key + ' legacy stage2 explicitly empty')
            else:
                audit.check([l['layer_id'] for l in mapped['task_proposal']['layers']] == ['upper', 'lower'], key + ' true two-layer task')
                for layer, source in zip(mapped['task_proposal']['layers'], stages):
                    audit.check(layer['waypoints_m'] == source['waypoints_m'] and layer['waypoints_wgs84'] == source['waypoints_wgs84'], key + ' task and transmitted layer consistency')
                # Actual layer transition is a direct array endpoint edge after descent.
                audit.legal([stages[0]['waypoints_m'][-1] + [stages[1]['altitude_m']], stages[1]['waypoints_m'][0] + [stages[1]['altitude_m']]], boundary, nofly, key + ' actual direct inter-layer edge')
            audit.check(mapped['jiangluodian']['data'] == [uav['reference_home']['longitude'], uav['reference_home']['latitude'], altitude], key + ' landing-point message altitude')
            payload = mapped['return_paths']['geometry_payload_only']
            audit.check(len(payload['routes']) == len(rec['return_paths']), key + ' topic return payload count')
            for rt, cr in zip(payload['routes'], rec['return_paths']):
                audit.check(rt['source_uav_id'] == cr['source_uav_id'] and rt['waypoint_index'] == cr['waypoint_index'] and rt['path'] == cr['path_lon_lat_alt'], key + ' topic return payload geometry')
            audit.check(uav['external_program_b_proposal']['ros_topics'] is None, key + ' unchanged main schema ros_topics null')
            row['total_routes'] += len(rec['return_paths'])
            row['recipients'].append(dict(uav_id=uid, altitude_m=altitude, routes=len(rec['return_paths']), return_climb_s=climb_time,
                normal_connection_distance_m=conn_length, selected_source_uav_id=normal['source_uav_id'], selected_waypoint_index=normal['waypoint_index'], return_time_s=return_time))
        audit.scene_rows.append(row)
    return audit, plan, catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [('input', 'inputs/02_规划输入_v2.json'), ('plan', 'planning_result.json'),
        ('catalog', 'return_catalog.json'), ('schema', 'return_catalog.schema.json'), ('selections', 'return_selections.json'),
        ('mapping', 'topic_mapping.json'), ('output', 'return_catalog_validation_report.json')]:
        parser.add_argument('--' + name, type=Path, default=ROOT / default)
    args = parser.parse_args()
    try:
        audit, plan, catalog = validate(args)
        report = dict(passed=not audit.failures, total_checks=audit.count, failures=audit.failures,
            planning_result_sha256=sha(args.plan), return_catalog_sha256=sha(args.catalog), topic_mapping_sha256=sha(args.mapping),
            independent_of_planner=True, coordinate_tolerance_m=1e-4, line_clearance_required_m=5,
            minimum_outer_clearance_m=audit.min_outer if math.isfinite(audit.min_outer) else None,
            minimum_no_fly_clearance_m=audit.min_nofly if math.isfinite(audit.min_nofly) else None,
            scenes=audit.scene_rows, scope='Offline catalog geometry, direct transmitted scan edges, normal-return consistency and documented topic mapping only.',
            runtime_compatibility='Geometry and explicit mapping verified only; no current ground/onboard/B import or runtime test performed. Scene 1 requires legacy single-layer mapping and assigned_physical_layer catalog adapter.',
            pending=['Nominal versus actual per-aircraft home GPS', 'Provisional heights/descent', 'Planner-estimated return climb speed', 'Unconfirmed pond boundary', 'Runtime B integration and collision avoidance'])
    except Exception as error:
        report = dict(passed=False, fatal_error=type(error).__name__ + ': ' + str(error), independent_of_planner=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ['scenes', 'pending']}, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
