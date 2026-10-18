#!/usr/bin/env python3
"""Negative regressions against the independent return catalog validator."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import validate_return_catalog as validator

ROOT = Path(__file__).resolve().parent


def main():
    original = validator.read(ROOT / 'return_catalog.json')
    original_mapping = validator.read(ROOT / 'topic_mapping.json')
    def missing(c, m):
        c['scenes']['final_scene_2']['1']['return_paths'].pop()
    def wrong_height(c, m):
        route = c['scenes']['final_scene_2']['1']['return_paths'][0]
        for key in ['path_nwz_m', 'path_lon_lat_alt']:
            for point in route[key]:
                point[2] += 3
    def wrong_index(c, m):
        c['scenes']['final_scene_2']['1']['return_paths'][0]['waypoint_index'] = 2
    def missing_gate(c, m):
        for rec in c['scenes']['xuchang_small_2'].values():
            if rec['retained_return_gate_points_m']:
                route = rec['return_paths'][0]
                for field in ['path_nwz_m', 'path_lon_lat_alt']:
                    route[field] = [route[field][0], route[field][-1]]
                route['horizontal_distance_m'] = validator.separation(route['path_nwz_m'][0][:2], route['path_nwz_m'][-1][:2])
                return
        raise RuntimeError('No inherited gate available for negative test')
    def teleport(c, m):
        n = c['scenes']['final_scene_2']['1']['normal_return_connection']
        for field in ['path_nwz_m', 'path_lon_lat_alt']:
            n['connection_path'][field] = [n['connection_path'][field][-1]] * 2
        n['connection_path']['horizontal_distance_m'] = 0
    def missing_scan(m_catalog, m):
        rec = m['scenes']['final_scene_2']['1']['path_stage_1']
        rec['data'] = rec['data'][3:]
    cases = [('missing_source_route', missing), ('source_altitude_used_instead_of_recipient', wrong_height),
        ('wrong_source_waypoint_index', wrong_index), ('inherited_return_gate_omitted', missing_gate),
        ('normal_return_connection_teleport', teleport), ('actual_stage_array_missing_scan_point', missing_scan)]
    rows = []
    with tempfile.TemporaryDirectory(prefix='return-catalog-negative-') as tmp:
        tmp = Path(tmp)
        args = SimpleNamespace(plan=ROOT / 'planning_result.json', input=ROOT / 'inputs/02_规划输入_v2.json',
            selections=ROOT / 'return_selections.json', schema=ROOT / 'return_catalog.schema.json',
            catalog=tmp / 'return_catalog.json', mapping=tmp / 'topic_mapping.json')
        args.catalog.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
        args.mapping.write_text(json.dumps(original_mapping, ensure_ascii=False), encoding='utf-8')
        baseline, _, _ = validator.validate(args)
        if baseline.failures:
            raise RuntimeError('Negative tests require a passing final baseline first: ' + str(baseline.failures[:3]))
        for name, mutate in cases:
            catalog = copy.deepcopy(original); mapping = copy.deepcopy(original_mapping); mutate(catalog, mapping)
            args.catalog.write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
            args.mapping.write_text(json.dumps(mapping, ensure_ascii=False), encoding='utf-8')
            try:
                audit, _, _ = validator.validate(args)
                failures = audit.failures
                rows.append(dict(case=name, rejected=bool(failures), failure_count=len(failures), examples=failures[:3]))
            except Exception as error:
                rows.append(dict(case=name, rejected=True, exception=type(error).__name__ + ': ' + str(error)))
    report = dict(passed=all(row['rejected'] for row in rows), baseline_passed=True,
        planning_result_sha256=validator.sha(ROOT / 'planning_result.json'),
        return_catalog_sha256=validator.sha(ROOT / 'return_catalog.json'),
        original_files_unmodified=True, cases=rows)
    (ROOT / 'return_catalog_negative_tests.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
