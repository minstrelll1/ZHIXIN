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


def reflow(scene, uav):
    clock=0.0
    for i,s in enumerate(uav['segments']):
        s['segment_id']='tamper_segment_'+str(i)
        s['start_s']=clock
        clock+=s['duration_s']
        s['end_s']=clock
    t=uav['timing']
    for phase in ('takeoff','entry','scan_transit','scan','layer_transition','return'):
        t[phase+'_s']=sum(s['duration_s'] for s in uav['segments'] if s['phase']==phase)
    t.update(mission_time_s=clock,remaining_s=1380-clock,budget_with_reserve_s=clock+30,within_limit=clock<=1380,within_planning_target=clock<=1350)
    scene['scene_metrics']['maximum_completion_time_s']=max(u['timing']['mission_time_s'] for u in scene['uavs'])
    scene['scene_metrics']['total_aircraft_time_s']=sum(u['timing']['mission_time_s'] for u in scene['uavs'])
    scene['scene_metrics']['all_aircraft_within_limit']=all(u['timing']['within_limit'] for u in scene['uavs'])


def tamper_old_radius(scene):
    scene['uavs'][0]['layers'][0]['reconnaissance_radius_m']=30


def tamper_old_forest(scene):
    old=read_json(ROOT/'inputs/v1/exclusions_draft_v1.json')
    previous=next(e for e in old['exclusions'] if e['id']=='southeast_exclusion_anchor')
    current=next(e for e in scene['exclusions'] if e['id']=='southeast_exclusion_anchor')
    current['geometry_wgs84']=copy.deepcopy(previous['geometry_wgs84'])


def tamper_missing_return_climb(scene):
    uav=scene['uavs'][0]
    lower=uav['layers'][-1]['altitude_m']
    uav['segments']=[s for s in uav['segments'] if not(s['phase']=='return' and abs(s['to_nwz_m'][2]-s['from_nwz_m'][2])>0)]
    for s in uav['segments']:
        if s['phase']=='return':
            s['from_nwz_m'][2]=lower
            s['to_nwz_m'][2]=lower
    reflow(scene,uav)


def tamper_hidden_auxiliary_turn(scene):
    # Segments contain a bend which is absent from the transmitted scan array.
    # Restore all travel distances/times so a timing-only check cannot mask it.
    import math
    uav=scene['uavs'][0]
    index=next(i for i,s in enumerate(uav['segments']) if s['phase']=='scan_transit' and s['horizontal_distance_m']>5)
    seg=uav['segments'][index]
    a,b=seg['from_nwz_m'],seg['to_nwz_m']
    length=math.dist(a[:2],b[:2])
    mid=[(a[0]+b[0])/2-(b[1]-a[1])/length,(a[1]+b[1])/2+(b[0]-a[0])/length,a[2]]
    first,second=copy.deepcopy(seg),copy.deepcopy(seg)
    first['to_nwz_m']=mid
    second['from_nwz_m']=mid
    for s in (first,second):
        s['horizontal_distance_m']=math.dist(s['from_nwz_m'][:2],s['to_nwz_m'][:2])
        s['duration_s']=s['horizontal_distance_m']/5
    uav['segments'][index:index+1]=[first,second]
    reflow(scene,uav)


def tamper_unsafe_actual_array(scene):
    # Known original interface gap: retain the final complete segments while
    # corrupting only the actual arrays to imply a 0.9589 m outer-clearance link.
    previous=read_json(ROOT/'inputs/v1/planning_result_v1.json')
    original=next(s for s in previous['scenes'] if s['scene_id']=='final_scene_1')['uavs'][0]['layers'][0]
    uav=scene['uavs'][0]
    layer=uav['layers'][0]
    for new_index,old_index in enumerate((1,2)):
        for key in ('waypoints_m','waypoints_wgs84','waypoints_lon_lat_alt'):
            layer[key][new_index]=copy.deepcopy(original[key][old_index])
    uav['external_program_b_proposal'][layer['layer_id']]['waypoints_lon_lat_alt']=copy.deepcopy(layer['waypoints_lon_lat_alt'])


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
        ('old_upper30_radius_restored', 'final_scene_1', tamper_old_radius, 'semantics'),
        ('old_southeast_block_restored', 'final_scene_2', tamper_old_forest, 'forest_replacement'),
        ('return_climb_omitted_with_consistent_timing', 'final_scene_2', tamper_missing_return_climb, 'return_climb'),
        ('untransmitted_auxiliary_turn_in_segments', 'final_scene_1', tamper_hidden_auxiliary_turn, 'scan_array_realization'),
        ('unsafe_actual_array_despite_routed_segments', 'final_scene_1', tamper_unsafe_actual_array, 'actual_array_direct_links'),
    ]
    checks = []
    for name, sid, mutate, category in cases:
        scene = copy.deepcopy(next(s for s in source['scenes'] if s['scene_id'] == sid))
        mutate(scene)
        validator = Validator(result_path, ROOT/'inputs/02_规划输入_v2.json', ROOT/'inputs/03_规划输出.schema.json')
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
