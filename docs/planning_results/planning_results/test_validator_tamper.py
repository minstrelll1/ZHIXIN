#!/usr/bin/env python3
"""Adversarial smoke checks: validator must reject corrupted complete plans.
Run after planner.py: python3 test_validator_tamper.py
Temporary files are isolated; the delivered plan is never modified.
"""
import copy
import json
import tempfile
from pathlib import Path
from validate_plan import Validator, read_json, nw

ROOT = Path(__file__).resolve().parent


def tamper_duration(scene):
    next(s for s in scene['uavs'][0]['segments'] if s['phase'] == 'scan')['duration_s'] += 7


def tamper_turnaround(scene):
    scans = [s for s in scene['uavs'][0]['segments'] if s['phase'] == 'scan']
    scans[len(scans)//2]['pass_index'] = 1


def tamper_strong_circle(scene):
    p = scene['projection']
    e = next(e for e in scene['exclusions'] if e['kind'] == 'scan_disk_forbidden')
    lon, lat = e['geometry_wgs84']['coordinates'][0][0]
    layer = scene['uavs'][0]['layers'][0]
    xy = nw(lat, lon, p).tolist()
    layer['waypoints_m'][0] = xy
    layer['waypoints_wgs84'][0] = [lat, lon]
    layer['waypoints_lon_lat_alt'][0] = [lon, lat, layer['altitude_m']]


def tamper_coverage(scene):
    c = scene['scene_metrics']['upper']
    c['coverage_ratio'] = c['coverage_ratio'] + .01 if c['coverage_ratio'] < .98 else c['coverage_ratio'] - .01


def tamper_partition(scene):
    # Swap already valid existing regions between aircraft: aggregate topology
    # remains unchanged, but input UAV ownership must not silently change.
    a, b = scene['uavs'][:2]
    for key in ('polygon_m', 'region_geometry_wgs84'):
        a[key], b[key] = b[key], a[key]


def tamper_no_fly_crossing(scene):
    nf = next(e for e in scene['exclusions'] if e['kind'] == 'no_fly')
    from validate_plan import geometry_m
    g = geometry_m(nf['geometry_wgs84'], scene['projection'])
    xmin, ymin, xmax, ymax = g.bounds
    x = (xmin+xmax)/2
    segment = next(s for s in scene['uavs'][0]['segments'] if s['phase'] == 'entry')
    segment['from_nwz_m'] = [x, ymin-10, 47]
    segment['to_nwz_m'] = [x, ymax+10, 47]


def tamper_budget(scene):
    uav = scene['uavs'][0]
    route = [copy.deepcopy(s) for s in uav['segments'] if s['phase'] == 'return']
    # Each extra out-and-back follows exactly the existing legal return route.
    # All segment distances, speeds and declared times remain internally valid.
    while sum(s['duration_s'] for s in uav['segments']) <= 1381:
        for old in reversed(route):
            new = copy.deepcopy(old)
            new['from_nwz_m'], new['to_nwz_m'] = new['to_nwz_m'], new['from_nwz_m']
            uav['segments'].append(new)
        uav['segments'].extend(copy.deepcopy(route))
    clock = 0
    for i, seg in enumerate(uav['segments']):
        seg['segment_id'] = 'tamper_segment_' + str(i)
        seg['start_s'] = clock
        clock += seg['duration_s']
        seg['end_s'] = clock
    t = uav['timing']
    t['return_s'] = sum(s['duration_s'] for s in uav['segments'] if s['phase']=='return')
    t['mission_time_s'] = clock
    t['remaining_s'] = 1380-clock
    t['budget_with_reserve_s'] = clock+30
    t['within_limit'] = False
    t['within_planning_target'] = False
    metrics = scene['scene_metrics']
    metrics['maximum_completion_time_s'] = max(u['timing']['mission_time_s'] for u in scene['uavs'])
    metrics['total_aircraft_time_s'] = sum(u['timing']['mission_time_s'] for u in scene['uavs'])
    metrics['all_aircraft_within_limit'] = False


def tamper_confirmed(scene):
    scene['status'] = 'validated'


def main():
    result_path = ROOT/'planning_result.json'
    source = read_json(result_path)
    cases = [
        ('scan_duration', 'final_scene_1', tamper_duration, 'time'),
        ('repeated_turn_scan_pass', 'final_scene_1', tamper_turnaround, 'execution'),
        ('strong_scan_circle', 'final_scene_1', tamper_strong_circle, 'strong_scan_exclusion'),
        ('false_coverage_ratio', 'final_scene_2', tamper_coverage, 'coverage'),
        ('swapped_fixed_xuchang_regions', 'xuchang_small_2', tamper_partition, 'partition'),
        ('segment_crosses_no_fly', 'xuchang_small_2', tamper_no_fly_crossing, 'flight_clearance'),
        ('internally_consistent_over_budget', 'final_scene_1', tamper_budget, 'time_budget'),
        ('unconfirmed_boundary_claimed_validated', 'final_scene_1', tamper_confirmed, 'provisional_status'),
    ]
    checks = []
    for name, sid, mutate, category in cases:
        scene = copy.deepcopy(next(s for s in source['scenes'] if s['scene_id'] == sid))
        mutate(scene)
        validator = Validator(result_path, ROOT/'inputs/02_规划输入.json', ROOT/'inputs/03_规划输出.schema.json')
        validator.validate_scene(scene)
        relevant = [f for f in validator.failures if f['category'] == category]
        caught = bool(relevant)
        checks.append({'name': name, 'expected_failure_category': category,
                       'caught': caught, 'matching_failure_count': len(relevant)})
        print(name + ': ' + ('rejected as expected' if caught else 'FAILED TO DETECT'))
    report = {'passed': all(c['caught'] for c in checks),
              'purpose': 'Adversarial changes must be detected; does not establish real-world flight safety.',
              'cases': checks}
    (ROOT/'validator_tamper_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
