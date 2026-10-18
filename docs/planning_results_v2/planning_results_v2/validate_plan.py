#!/usr/bin/env python3
"""Independent, fail-closed offline plan validator. Does not import planner.py.

Usage: python3 validate_plan.py [--result planning_result.json]
Dependencies: Python >=3.8, shapely >=2, numpy, jsonschema.
Exit 0 means the stored candidate satisfies these offline checks; it is NOT
flight permission, confirmation of exclusion boundaries, or a flight test.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import shapely
from jsonschema import Draft202012Validator
from shapely.geometry import Point, LineString, Polygon, shape
from shapely.ops import transform, unary_union

COORD_TOL_M = 1e-4
AREA_TOL_M2 = 0.01
TIME_TOL_S = 0.1
NUMERIC_EPS_M = 1e-7
RATIO_TOL = 1e-7
PHASES = ('takeoff', 'entry', 'scan_transit', 'scan', 'layer_transition', 'return')
SCENE_IDS = ('final_scene_1', 'final_scene_2', 'xuchang_small_2')
WAYPOINT_COLUMNS = 'scene_id,uav_id,layer_id,waypoint_id,waypoint_index_1based,latitude,longitude,north_m,west_m,relative_altitude_m,scan_radius_m,scan_seconds,scan_passes'.split(',')
VISIT_COLUMNS = 'scene_id,uav_id,visit_index_1based,layer_id,pass_index_1based,waypoint_id,start_s,end_s'.split(',')


def reject_constant(value):
    raise ValueError('Non-finite JSON constant: ' + value)


def strict_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite JSON numeric value: ' + value)
    return result


def unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate JSON object key: ' + key)
        out[key] = value
    return out


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),
                      parse_constant=reject_constant, parse_float=strict_float, object_pairs_hook=unique_object)


def read_json_snapshot(path):
    raw=Path(path).read_bytes()
    value=json.loads(raw.decode('utf-8-sig'),parse_constant=reject_constant,parse_float=strict_float,object_pairs_hook=unique_object)
    return value,hashlib.sha256(raw).hexdigest()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plain(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def nw(lat, lon, p):
    return np.array([(lat - p['latitude']) * p['north_m_per_degree'],
                     -(lon - p['longitude']) * p['west_m_per_degree']], dtype=float)


def geometry_m(g, p):
    if g is None:
        return Polygon()
    def project(lon, lat, z=None):
        x = (np.asarray(lat) - p['latitude']) * p['north_m_per_degree']
        y = -(np.asarray(lon) - p['longitude']) * p['west_m_per_degree']
        return (x, y) if z is None else (x, y, z)
    return transform(project, shape(g))


def polygonal(g):
    # Set differences of areal geometries may also contain zero-area line/point
    # residues. Preserve every polygon and hole; omit only lower dimensions.
    if g.geom_type in ('Polygon', 'MultiPolygon'):
        return g
    if hasattr(g, 'geoms'):
        return unary_union([polygonal(part) for part in g.geoms])
    return Polygon()


def distance(a, b):
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def polygon_rings(g):
    if g['type'] == 'Polygon':
        return g['coordinates']
    if g['type'] == 'MultiPolygon':
        return [ring for polygon in g['coordinates'] for ring in polygon]
    return []


class Validator:
    def __init__(self, result_path, input_path, schema_path, skip_exports=False):
        self.skip_exports = skip_exports
        self.result_path = Path(result_path)
        self.input_path = Path(input_path)
        self.schema_path = Path(schema_path)
        self.result, self.result_snapshot_sha256 = read_json_snapshot(result_path)
        self.input, self.input_snapshot_sha256 = read_json_snapshot(input_path)
        self.revision_path = self.input_path.parent/'revision'/'修订需求.json'
        self.revision, self.revision_snapshot_sha256 = read_json_snapshot(self.revision_path)
        self.direct_link_summaries = []
        self.redundancy_summaries = {}
        self.schema, self.schema_snapshot_sha256 = read_json_snapshot(schema_path)
        self.checks = Counter()
        self.failures = []
        self.warnings = []
        self.scenes = {}
        self.expected_waypoints = []
        self.expected_visits = []
        self.expected_features = []
        self.quad_segs = self.result.get('planner', {}).get('parameters', {}).get('circle_quad_segs', 128)

    def check(self, category, condition, location, detail='', actual=None, expected=None):
        self.checks[category] += 1
        if not bool(condition):
            item = {'category': category, 'location': location, 'detail': detail}
            if actual is not None:
                item['actual'] = plain(actual)
            if expected is not None:
                item['expected'] = plain(expected)
            self.failures.append(item)
        return bool(condition)

    def near(self, category, actual, expected, location, tolerance, detail=''):
        return self.check(category, math.isfinite(float(actual)) and abs(actual - expected) <= tolerance,
                          location, detail or 'Recomputed numeric value differs', actual, expected)

    def geom(self, obj, p, location):
        self.check('geometry', obj is not None, location, 'Required geometry cannot be null')
        if obj is None:
            return Polygon()
        for i, ring in enumerate(polygon_rings(obj)):
            self.check('geometry', len(ring) >= 4 and ring[0] == ring[-1],
                       location + '/ring' + str(i), 'GeoJSON rings must be closed')
        g = geometry_m(obj, p)
        self.check('geometry', g.is_valid, location, 'Invalid stored geometry: ' + shapely.is_valid_reason(g))
        return g

    def same_geom(self, actual, expected, location, category='geometry', tolerance=AREA_TOL_M2):
        if not actual.is_valid or not expected.is_valid:
            self.check(category, False, location, 'Cannot compare invalid geometry')
            return
        mismatch = actual.symmetric_difference(expected).area
        self.near(category, mismatch, 0, location, tolerance, 'Geometry symmetric-difference area (m²)')

    def feature(self, scene_id, uav_id, layer_id, kind, geometry, feature_id=None):
        self.expected_features.append({'scene_id': scene_id, 'uav_id': uav_id,
                                      'layer_id': layer_id, 'kind': kind,
                                      'id': feature_id, 'geometry_m': geometry})

    def home(self, home, source, p, location):
        self.near('fixed_home', home['latitude'], source['latitude'], location + '/latitude', 1e-12)
        self.near('fixed_home', home['longitude'], source['longitude'], location + '/longitude', 1e-12)
        expected = nw(source['latitude'], source['longitude'], p)
        self.near('coordinate_conversion', distance(home['home_nw_m'], expected), 0, location, COORD_TOL_M)
        self.check('fixed_home', home['is_nominal'] is True and home['source'] == 'nominal_reference_home',
                   location, 'Homes are nominal; measured homes are unavailable')
        return expected

    def coverage(self, reported, denominator, disks, p, location):
        covered = unary_union(disks).intersection(denominator) if disks else Polygon()
        uncovered = polygonal(denominator.difference(covered))
        summed = sum(disk.intersection(denominator).area for disk in disks)
        redundant = max(0.0, summed - covered.area)
        values = {'effective_area_m2': denominator.area, 'covered_area_m2': covered.area,
                  'uncovered_area_m2': uncovered.area,
                  'coverage_ratio': covered.area / denominator.area if denominator.area else 0,
                  'independent_scan_count': len(disks), 'redundant_area_m2': redundant,
                  'redundancy_ratio': redundant / summed if summed else 0}
        for key, expected in values.items():
            tol = RATIO_TOL if 'ratio' in key else (0 if key.endswith('count') else AREA_TOL_M2)
            self.near('coverage', reported[key], expected, location + '/' + key, tol)
        stored = reported['uncovered_geometry_wgs84']
        if stored is None:
            self.check('coverage', uncovered.area <= AREA_TOL_M2, location,
                       'Uncovered geometry is missing despite positive uncovered area', uncovered.area)
        else:
            self.same_geom(self.geom(stored, p, location + '/uncovered'), uncovered, location, 'coverage')
        return covered, uncovered, values

    def validate_revised_forest(self, scene, field, p, exclusions):
        loc = scene['scene_id'] + '/revised_southeast_forest'
        chain = self.revision['confirmed_overrides']['southeast_forest_user_chain_lat_lon']
        original = {v['point_id']: v for v in field['all_source_points']}
        vertices = {i: nw(original[i]['latitude'], original[i]['longitude'], p) for i in (8, 9, 10)}
        points = [nw(*ll, p) for ll in chain]
        south = LineString([vertices[8], vertices[9]])
        east = LineString([vertices[9], vertices[10]])
        qs = south.interpolate(south.project(Point(points[-1])))
        qe = east.interpolate(east.project(Point(points[0])))
        expected_vertices = points + [np.array(qs.coords[0]), vertices[9], np.array(qe.coords[0])]
        expected = Polygon(expected_vertices)
        eid = 'southeast_exclusion_anchor'
        self.check('forest_replacement', eid in exclusions, loc, 'The revised southeastern forest must replace the original exclusion')
        if eid not in exclusions:
            return
        geometry = geometry_m(exclusions[eid]['geometry_wgs84'], p)
        self.same_geom(geometry, expected, loc, 'forest_replacement')
        self.near('forest_replacement', geometry.hausdorff_distance(expected), 0, loc, COORD_TOL_M)
        self.near('forest_replacement', geometry.area, self.revision['proposed_not_user_measured_boundary_completion']['area_m2'], loc + '/area', AREA_TOL_M2)
        self.check('forest_replacement', geometry.geom_type == 'Polygon' and not geometry.interiors, loc, 'One complete P1…P8→Qsouth→original9→Qeast polygon required')
        if geometry.geom_type == 'Polygon':
            actual = list(geometry.exterior.coords)[:-1]
            self.check('forest_replacement', len(actual) == len(expected_vertices), loc, 'Retain all eight original user points and the three closing vertices')
            for index, point in enumerate(expected_vertices):
                self.check('forest_replacement', any(distance(point, vertex)<=COORD_TOL_M for vertex in actual), loc + '/vertex' + str(index+1), 'User point or independently projected closing vertex is missing')
        for q, key in [(qs, 'q_south_lat_lon'), (qe, 'q_east_lat_lon')]:
            self.near('forest_replacement', distance(q.coords[0], nw(*self.revision['proposed_not_user_measured_boundary_completion'][key], p)), 0, loc+'/'+key, COORD_TOL_M)
        self.check('forest_replacement', any(a['status'] != 'confirmed' and ('Q' in a['description'] or '补点' in a['description'] or '连接' in a['description']) for a in scene['assumptions']), loc, 'Derived Q connectors must be distinguished from user-supplied measurements')
        supplied = next((e for e in field['strict_scan_exclusions'] if e['id']==eid), None)
        self.check('forest_replacement', supplied is not None and supplied.get('geometry_wgs84') is not None, loc, 'Merged v2 input must contain the new forest geometry')
        if supplied and supplied.get('geometry_wgs84'):
            self.same_geom(geometry_m(supplied['geometry_wgs84'], p), expected, loc+'/v2_input', 'forest_replacement')
        previous = read_json(self.input_path.parent/'v1'/'02_规划输入.json')
        self.same_geom(geometry_m(field['terrain']['coverage_exempt_geometry_wgs84'], p), geometry_m(previous['final_field']['terrain']['coverage_exempt_geometry_wgs84'], p), loc+'/retained_terrain_mask', 'forest_replacement')
        old_config = read_json(self.input_path.parent/'v1'/'exclusions_draft_v1.json')
        old_forest = next(e for e in old_config['exclusions'] if e['id']==eid)
        old_geometry = geometry_m(old_forest['geometry_wgs84'], p)
        self.check('forest_replacement', geometry.symmetric_difference(old_geometry).area > 1000, loc, 'Old large southeastern block must not remain the active forest')
        old_pond = next(e for e in old_config['exclusions'] if e['id']=='pond_exclusion_anchor')
        self.same_geom(geometry_m(exclusions['pond_exclusion_anchor']['geometry_wgs84'], p), geometry_m(old_pond['geometry_wgs84'], p), loc+'/unchanged_provisional_pond', 'forest_replacement')

    def legal_direct_line(self, a, b, boundary, nofly):
        line = Point(a[:2]) if distance(a[:2], b[:2]) <= NUMERIC_EPS_M else LineString([a[:2], b[:2]])
        outer = line.distance(boundary.boundary)
        nofly_clearance = min((line.distance(g) for g in nofly), default=math.inf)
        legal = boundary.covers(line) and outer >= 5-NUMERIC_EPS_M and nofly_clearance >= 5-NUMERIC_EPS_M
        return legal, outer, nofly_clearance, line

    def validate_actual_arrays(self, scene, uav, region, boundary, nofly, p):
        # Independent of the pre-routed segments: reconstruct what B receives.
        loc = scene['scene_id'] + '/UAV' + str(uav['uav_id']) + '/actual_B_arrays'
        checks = 0
        minimum_outer = math.inf
        minimum_nofly = math.inf
        in_region = True
        arrays = {}
        for layer in uav['layers']:
            lid = layer['layer_id']
            payload = uav['external_program_b_proposal'][lid]
            if payload is None:
                continue
            arrays[lid] = [nw(v[1], v[0], p) for v in payload['waypoints_lon_lat_alt']]
            sequence = arrays[lid] + list(reversed(arrays[lid])) if layer['scan_passes'] == 2 else arrays[lid]
            for index, (a,b) in enumerate(zip(sequence, sequence[1:]), 1):
                legal, outer, nf, line = self.legal_direct_line(a,b,boundary,nofly)
                self.check('actual_array_direct_links', legal, loc+'/'+lid+'/'+str(index), 'Actual transmitted consecutive scan points must connect directly with full 5 m outer/no-fly clearance', {'outer_clearance_m':outer,'no_fly_clearance_m':None if math.isinf(nf) else nf})
                minimum_outer = min(minimum_outer,outer)
                minimum_nofly = min(minimum_nofly,nf)
                in_region = in_region and region.buffer(NUMERIC_EPS_M).covers(line)
                checks += 1
        if set(arrays) == {'upper','lower'}:
            legal, outer, nf, line = self.legal_direct_line(arrays['upper'][-1],arrays['lower'][0],boundary,nofly)
            self.check('actual_array_direct_links', legal, loc+'/upper_to_lower', 'After stationary descent, upper-last to lower-first must be one legal direct horizontal connection', {'outer_clearance_m':outer,'no_fly_clearance_m':None if math.isinf(nf) else nf})
            minimum_outer=min(minimum_outer,outer)
            minimum_nofly=min(minimum_nofly,nf)
            checks+=1
        return {'direct_links_checked': checks, 'minimum_outer_clearance_m': None if math.isinf(minimum_outer) else minimum_outer,
                'minimum_no_fly_clearance_m': None if math.isinf(minimum_nofly) else minimum_nofly,
                'all_scan_links_in_own_region':in_region, 'source':'external_program_b_proposal arrays, independent of segments'}

    def validate_scan_segment_realization(self, scene, uav):
        loc = scene['scene_id'] + '/UAV' + str(uav['uav_id'])
        segments=uav['segments']
        scans=[i for i,s in enumerate(segments) if s['phase']=='scan']
        for left,right in zip(scans,scans[1:]):
            before,after=segments[left],segments[right]
            between=segments[left+1:right]
            a,b=before['to_nwz_m'],after['from_nwz_m']
            horizontal=distance(a[:2],b[:2])
            if before['layer_id']==after['layer_id']:
                expected_count=1 if horizontal>NUMERIC_EPS_M else 0
                self.check('scan_array_realization', len(between)==expected_count and all(s['phase']=='scan_transit' for s in between), loc+'/'+before['waypoint_id']+'→'+after['waypoint_id'], 'B sends only scans: each scan-to-scan move must be a single direct segment, with no untransmitted auxiliary turn')
                if expected_count==1 and len(between)==1:
                    self.near('scan_array_realization', distance(between[0]['from_nwz_m'],a)+distance(between[0]['to_nwz_m'],b), 0, loc, COORD_TOL_M)
            else:
                expected_count=2 if horizontal>NUMERIC_EPS_M else 1
                self.check('scan_array_realization', len(between)==expected_count and all(s['phase']=='layer_transition' for s in between), loc+'/upper_to_lower', 'Layer change contains only stationary descent and optional one direct horizontal move; no hidden turn vertices')

    def audit_final_redundant_points(self, scene, effective, region_groups, boundary, nofly, p):
        # This is an independent audit of the final set, not a recreation of
        # the planner's sequential deletion optimization. Any interior point
        # whose removal preserves local+field coverage and a legal straight
        # neighbor link is demonstrably removable and must not remain.
        sid=scene['scene_id']
        all_disks=defaultdict(list)
        records=[]
        for uav in scene['uavs']:
            region=geometry_m(uav['region_geometry_wgs84'],p)
            for layer in uav['layers']:
                disks=[Point(q).buffer(layer['reconnaissance_radius_m'],quad_segs=self.quad_segs) for q in layer['waypoints_m']]
                record={'uav':uav,'layer':layer,'region':region,'disks':disks}
                records.append(record)
                for i,disk in enumerate(disks):all_disks[layer['layer_id']].append((uav['uav_id'],i,disk))
        audits=[]
        audited_points=0
        zero_local_count=0
        for record in records:
            uav,layer,region,disks=record['uav'],record['layer'],record['region'],record['disks']
            target=region.intersection(effective)
            own_full=unary_union(disks).intersection(target).area
            field_disks=all_disks[layer['layer_id']]
            field_full=unary_union([d for _,_,d in field_disks]).intersection(effective).area
            for i,(wid,disk) in enumerate(zip(layer['waypoint_ids'],disks)):
                local_gain=max(0.,own_full-unary_union(disks[:i]+disks[i+1:]).intersection(target).area)
                audited_points+=1
                self.check('redundant_scan_points', math.isfinite(local_gain), sid+'/UAV'+str(uav['uav_id'])+'/'+wid, 'Recompute this independent point exclusive contribution by deleting it from its final layer union')
                if local_gain>=AREA_TOL_M2:continue
                zero_local_count+=1
                global_gain=max(0.,field_full-unary_union([d for uid,j,d in field_disks if not (uid==uav['uav_id'] and j==i)]).intersection(effective).area)
                if global_gain>=AREA_TOL_M2:continue
                interior=0<i<len(disks)-1
                removable=False
                if interior:
                    legal,_,_,line=self.legal_direct_line(layer['waypoints_m'][i-1],layer['waypoints_m'][i+1],boundary,nofly)
                    removable=legal and region.buffer(NUMERIC_EPS_M).covers(line)
                self.check('redundant_scan_points', not removable, sid+'/UAV'+str(uav['uav_id'])+'/'+wid, 'Zero local and field coverage contribution plus a legal direct neighbor shortcut makes this interior scan point removable', {'local_unique_area_m2':local_gain,'field_unique_area_m2':global_gain})
                audits.append({'uav_id':uav['uav_id'],'layer_id':layer['layer_id'],'waypoint_id':wid,'local_unique_area_m2':local_gain,'field_unique_area_m2':global_gain,'interior_point':interior,'direct_neighbor_shortcut_removable':removable,'interpretation':'Route-critical interior point' if interior and not removable else 'Endpoint requires full entry/transition/return reoptimization before removal'})
        self.redundancy_summaries[sid]={'audited_independent_points':audited_points,'local_unique_area_below_0_01_m2_count':zero_local_count,'local_and_field_unique_area_below_0_01_m2_count':len(audits),'zero_contribution_points':audits,'method':'Remove one point at a time from the FINAL set and recompute exact polygon unions; if local contribution is >=0.01m2 it cannot satisfy the both-zero criterion.'}
        if audits:self.warnings.append({'location':sid,'kind':'zero_marginal_area_point_audit','points':audits,'note':'All final zero-area-contribution points listed; removable interior points are hard failures. Endpoint removal and documented iterative deletion are reported by the planner.'})

    def validate_scene(self, scene):
        sid = scene['scene_id']
        loc = sid
        spec = next(item for item in self.input['scenes'] if item['scene_id'] == sid)
        field = self.input[spec['site']]
        params = self.input['common_parameters']
        p = scene['projection']
        self.check('projection', p == field['projection'], loc, 'Fixed input projection must not change', p, field['projection'])
        boundary = self.geom(scene['boundary_geometry_wgs84'], p, loc + '/boundary')
        source_boundary = Polygon(field['boundary_m'])
        self.same_geom(boundary, source_boundary, loc + '/boundary_from_input')
        if sid.startswith('final'):
            expected_boundary = Polygon([nw(*v, p) for v in field['boundary_wgs84_lat_lon']])
            self.same_geom(boundary, expected_boundary, loc + '/original_first_ten_vertices')
        home = self.home(scene['reference_home'], field['reference_home'], p, loc + '/home')
        self.feature(sid, None, None, 'boundary', boundary)
        self.check('semantics', scene['execution_mode'] == spec['execution_mode'], loc, 'Execution mode matches input')
        self.check('semantics', sorted(u['uav_id'] for u in scene['uavs']) == list(range(1, 7)), loc, 'Exactly UAV1…UAV6 required')
        nofly, exempt, forbidden = [], [], []
        exclusion_by_id = {}
        for exclusion in scene['exclusions']:
            eid = exclusion['id']
            self.check('exclusion', eid not in exclusion_by_id, loc + '/' + eid, 'Duplicate exclusion ID')
            exclusion_by_id[eid] = exclusion
            g = self.geom(exclusion['geometry_wgs84'], p, loc + '/exclusions/' + eid)
            kind = exclusion['kind']
            self.feature(sid, None, None, kind, g, eid)
            if kind == 'no_fly':
                nofly.append(g)
                self.check('exclusion', exclusion['flight_prohibited'] and exclusion['clearance_m'] >= 5,
                           loc + '/' + eid, 'No-fly area must prohibit flight and retain 5 m clearance')
            elif kind == 'coverage_exempt':
                exempt.append(g)
                self.check('exclusion', not exclusion['flight_prohibited'] and not exclusion['scan_disk_intersection_forbidden'],
                           loc + '/' + eid, 'Terrain exemption is not a no-fly or scan-disc ban')
            elif kind == 'scan_disk_forbidden':
                forbidden.append(g)
                self.check('exclusion', exclusion['scan_disk_intersection_forbidden'] and not exclusion['flight_prohibited'],
                           loc + '/' + eid, 'New scan exclusion forbids discs; it does not forbid transit')
                self.check('provisional_status', exclusion['geometry_status'] == 'provisional', loc + '/' + eid,
                           'User supplied anchors only; candidate polygon cannot be marked confirmed')
                self.check('provisional_status', bool(exclusion['source']) and bool(exclusion['source_date']),
                           loc + '/' + eid, 'Provisional geometry needs source and source date')
        if sid.startswith('final'):
            expected_ids = {e['id'] for e in field['strict_scan_exclusions']}
            self.check('exclusion', {e['id'] for e in scene['exclusions'] if e['kind'] == 'scan_disk_forbidden'} == expected_ids,
                       loc, 'Both new strong scan exclusions required')
            for anchor in field['strict_scan_exclusions']:
                if anchor['id'] in exclusion_by_id:
                    g = geometry_m(exclusion_by_id[anchor['id']]['geometry_wgs84'], p)
                    self.check('exclusion', g.buffer(COORD_TOL_M).covers(Point(nw(anchor['latitude'], anchor['longitude'], p))),
                               loc + '/' + anchor['id'], 'Candidate boundary must include the supplied location anchor')
            self.validate_revised_forest(scene, field, p, exclusion_by_id)
            terrain = geometry_m(field['terrain']['coverage_exempt_geometry_wgs84'], p)
            self.same_geom(unary_union(exempt), terrain.intersection(boundary), loc + '/inherited_terrain_exemptions', 'exclusion')
            self.check('exclusion', not nofly, loc, 'No invented final-field no-fly polygon')
            self.check('provisional_status', scene['status'] == 'draft', loc,
                       'Unconfirmed real exclusion boundaries require draft status')
        else:
            self.same_geom(unary_union(nofly), geometry_m(field['no_fly_geometry_wgs84'], p), loc + '/inherited_no_fly', 'exclusion')
            self.check('exclusion', not forbidden and not exempt, loc, 'Do not invent additional Xuchang exclusions')
        self.check('provisional_status', len(scene['pending_confirmations']) > 0, loc,
                   'Actual homes, provisional descent and planner-estimated return ascent are not flight-tested')
        if sid != 'final_scene_1':
            self.check('provisional_status', any(a['status'] == 'accepted_provisional' for a in scene['assumptions']),
                       loc, 'Accepted provisional altitude/descent must remain labelled')
            self.check('return_climb_assumption', any(a['status'] == 'unconfirmed' and ('return_climb' in a['id'] or '回升' in a['description']) for a in scene['assumptions']), loc, 'Return climb 2 m/s is a planner assumption, not user-confirmed or measured')
        nf = unary_union(nofly)
        covered_exempt = unary_union(exempt + forbidden)
        partition_domain = boundary.difference(nf)
        effective = partition_domain.difference(covered_exempt)
        stored_effective = self.geom(scene['effective_area_geometry_wgs84'], p, loc + '/effective')
        self.same_geom(stored_effective, effective, loc + '/effective_recomputed', 'coverage')
        discs_per_layer = defaultdict(list)
        region_groups = defaultdict(list)
        uav_summaries = []
        times = []
        for uav in scene['uavs']:
            uid = uav['uav_id']
            uloc = loc + '/UAV' + str(uid)
            region = self.geom(uav['region_geometry_wgs84'], p, uloc + '/region')
            self.check('geometry', not region.is_empty and region.area > 0, uloc, 'Nonempty positive-area task region')
            poly = uav['polygon_m']
            simple = region.geom_type == 'Polygon' and len(region.interiors) == 0
            if simple:
                self.check('coordinate_conversion', poly is not None, uloc, 'Simple polygon must retain polygon_m')
                if poly:
                    self.check('geometry', poly[0] != poly[-1], uloc, 'polygon_m must not repeat its first vertex')
                    self.same_geom(Polygon(poly), region, uloc + '/polygon_m', 'coordinate_conversion')
            else:
                self.check('geometry', poly is None and bool(uav['compatibility_notes']), uloc,
                           'Holes/MultiPolygon require polygon_m:null and compatibility note')
            if sid == 'xuchang_small_2':
                original = next(v for v in field['baseline_uavs'] if v['uav_id'] == uid)
                self.same_geom(region, Polygon(original['polygon_m']), uloc + '/unchanged_baseline_region', 'partition')
            self.home(uav['reference_home'], field['reference_home'], p, uloc + '/home')
            if sid == 'final_scene_1':
                lids = ['upper'] if uid <= 3 else ['lower']
                group = lids[0]
            else:
                lids, group = ['upper', 'lower'], 'all'
            self.check('semantics', uav['partition_group'] == group, uloc, 'Partition group mapping')
            self.check('semantics', uav['layer_order'] == lids and [l['layer_id'] for l in uav['layers']] == lids,
                       uloc, 'Layer count and execution order')
            region_groups[group].append(region)
            self.feature(sid, uid, group if group != 'all' else None, 'subregion', region)
            by_id = {}
            expected_scans = []
            layer_summaries = {}
            for layer in uav['layers']:
                lid = layer['layer_id']
                lloc = uloc + '/' + lid
                if sid == 'final_scene_1':
                    alt = spec[lid + '_uavs'][str(uid)]['altitude_m']
                else:
                    alt = spec['heights_by_uav'][str(uid)][lid + '_height_m']
                rad = params[lid + '_scan_radius_m']
                passes = spec['scan_passes_per_layer']
                for key, expected in [('altitude_m', alt), ('reconnaissance_radius_m', rad),
                                      ('speed_mps', params['horizontal_speed_mps']),
                                      ('hover_scan_seconds', params['scan_seconds']), ('scan_passes', passes)]:
                    self.near('semantics', layer[key], expected, lloc + '/' + key, 0)
                ids = layer['waypoint_ids']
                points = layer['waypoints_m']
                self.check('waypoints', len(set(ids)) == len(ids), lloc, 'Independent waypoint IDs must be unique')
                lengths = [len(layer[k]) for k in ['waypoint_ids', 'waypoints_m', 'waypoints_wgs84', 'waypoints_lon_lat_alt']]
                self.check('waypoints', len(set(lengths)) == 1, lloc, 'Waypoint arrays have identical length', lengths)
                order = ids + list(reversed(ids)) if passes == 2 else ids
                self.check('execution', layer['execution_order'] == order, lloc, 'Complete required scan order including repeated turn')
                disks = []
                for index, (wid, xy, ll, lla) in enumerate(zip(ids, points, layer['waypoints_wgs84'], layer['waypoints_lon_lat_alt']), 1):
                    wloc = lloc + '/' + wid
                    self.check('waypoints', wid not in by_id, wloc, 'Waypoint ID must be unique within aircraft')
                    by_id[wid] = {'point': np.array([*xy, alt]), 'layer': lid}
                    self.near('coordinate_conversion', distance(xy, nw(ll[0], ll[1], p)), 0, wloc, COORD_TOL_M)
                    self.near('coordinate_conversion', distance(xy, nw(lla[1], lla[0], p)), 0, wloc + '/lon_lat_alt', COORD_TOL_M)
                    self.near('coordinate_conversion', lla[2], alt, wloc + '/altitude', 1e-9)
                    pt = Point(xy)
                    self.check('waypoint_legality', region.buffer(NUMERIC_EPS_M).covers(pt), wloc, 'Center must belong to own region')
                    self.check('waypoint_legality', effective.buffer(NUMERIC_EPS_M).covers(pt), wloc, 'Center must be in effective reconnaissance area')
                    for forbidden_id, forbidden_geom in enumerate(forbidden):
                        clearance = pt.distance(forbidden_geom)
                        self.check('strong_scan_exclusion', clearance >= rad + 1e-6, wloc,
                                   'Exact point-to-polygon distance must exceed radius + numerical margin', clearance, rad + 1e-6)
                    for previous in points[:index-1]:
                        self.check('waypoints', distance(xy, previous) > COORD_TOL_M, wloc, 'Unique layer points cannot be duplicate coordinates')
                    disk = pt.buffer(rad, quad_segs=self.quad_segs)
                    disks.append(disk)
                    self.feature(sid, uid, lid, 'waypoint', Point([*xy, alt]), wid)
                    self.expected_waypoints.append(dict(zip(WAYPOINT_COLUMNS,
                        [sid, uid, lid, wid, index, ll[0], ll[1], xy[0], xy[1], alt, rad, params['scan_seconds'], passes])))
                for index, wid in enumerate(order):
                    expected_scans.append((lid, wid, 1 if index < len(ids) else 2))
                _, uncovered, values = self.coverage(layer['coverage'], region.intersection(effective), disks, p, lloc + '/coverage')
                self.feature(sid, uid, lid, 'uncovered', uncovered)
                layer_summaries[lid] = values
                discs_per_layer[lid].extend(disks)
                proposal = uav['external_program_b_proposal'][lid]
                self.check('b_proposal', proposal is not None, lloc, 'Required logical layer payload missing')
                if proposal:
                    for key in ['waypoint_ids', 'waypoints_lon_lat_alt', 'scan_passes', 'execution_order']:
                        self.check('b_proposal', proposal[key] == layer[key], lloc + '/' + key, 'B proposal equals canonical layer')
                    self.check('b_proposal', proposal['scan_seconds'] == params['scan_seconds'], lloc, 'B scan duration')
                    self.same_geom(geometry_m(proposal['region_geometry_wgs84'], p), region, lloc + '/B_region', 'b_proposal')
            for absent in set(['upper', 'lower']) - set(lids):
                self.check('b_proposal', uav['external_program_b_proposal'][absent] is None, uloc, 'Absent layer payload must be null')
            direct_summary = self.validate_actual_arrays(scene, uav, region, boundary, nofly, p)
            summary = self.segments(scene, uav, by_id, expected_scans, region, boundary, nofly, home, params, field)
            summary['actual_transmitted_array_direct_links'] = direct_summary
            summary['layers'] = layer_summaries
            uav_summaries.append(summary)
            times.append(summary['mission_time_s'])
        self.audit_final_redundant_points(scene, effective, region_groups, boundary, nofly, p)
        metrics = scene['scene_metrics']
        partition_reports = {v['group']: v for v in metrics['partition_checks']}
        self.check('partition', set(partition_reports) == set(region_groups), loc, 'Every partition group has a topology report')
        topology = {}
        for group, regions in region_groups.items():
            combined = unary_union(regions)
            overlap = sum(a.intersection(b).area for i, a in enumerate(regions) for b in regions[i+1:])
            gap = partition_domain.difference(combined).area
            outside = combined.difference(partition_domain).area
            vals = {'overlap_area_m2': overlap, 'gap_area_m2': gap, 'outside_area_m2': outside}
            for key, actual in vals.items():
                self.check('partition', actual <= AREA_TOL_M2, loc + '/' + group + '/' + key,
                           'Disjoint complete partition required; tolerance is rounding-only area', actual, 0)
                if group in partition_reports:
                    self.near('partition', partition_reports[group][key], actual, loc + '/' + group + '/' + key, AREA_TOL_M2)
            if group in partition_reports:
                self.check('partition', partition_reports[group]['all_regions_valid'] == all(g.is_valid for g in regions),
                           loc + '/' + group, 'Validity flag must reflect actual regions')
            topology[group] = vals
        layer_coverage = {}
        layer_values = {}
        for lid in ('upper', 'lower'):
            cov, uncov, values = self.coverage(metrics[lid], effective, discs_per_layer[lid], p, loc + '/metrics/' + lid)
            layer_coverage[lid] = cov
            layer_values[lid] = values
            self.feature(sid, None, lid, 'uncovered', uncov)
        both = layer_coverage['upper'].intersection(layer_coverage['lower']).area / effective.area
        either = layer_coverage['upper'].union(layer_coverage['lower']).area / effective.area
        self.near('coverage', metrics['both_layers_coverage_ratio'], both, loc + '/both', RATIO_TOL)
        self.near('coverage', metrics['either_layer_coverage_ratio'], either, loc + '/either', RATIO_TOL)
        self.near('time', metrics['maximum_completion_time_s'], max(times), loc + '/max_time', TIME_TOL_S)
        self.near('time', metrics['total_aircraft_time_s'], sum(times), loc + '/total_time', TIME_TOL_S)
        self.check('time', metrics['all_aircraft_within_limit'] == all(t <= params['mission_limit_s'] for t in times), loc, 'Budget status flag')
        self.scenes[sid] = {'status': scene['status'], 'scope': 'offline_nominal_parameters_only',
                            'boundary_area_m2': boundary.area, 'effective_area_m2': effective.area,
                            'layers': layer_values, 'both_layers_coverage_ratio': both,
                            'either_layer_coverage_ratio': either, 'partition_checks': topology,
                            'maximum_completion_time_s': max(times), 'total_aircraft_time_s': sum(times),
                            'uavs': uav_summaries,
                            'pending_confirmations': scene['pending_confirmations']}

    def segments(self, scene, uav, waypoints, expected_scans, region, boundary, nofly, home, params, field):
        sid, uid = scene['scene_id'], uav['uav_id']
        loc = sid + '/UAV' + str(uid)
        layers = {l['layer_id']: l for l in uav['layers']}
        segments = uav['segments']
        actual_scans = []
        times = defaultdict(float)
        distances = defaultdict(float)
        phase_paths = defaultdict(list)
        previous = np.array([*home, 0.0])
        clock = 0.0
        last_phase = None
        takeoff_count = 0
        seen_scan = False
        seen_return = False
        transition_segments = []
        min_outer_clearance = math.inf
        min_nofly_clearance = math.inf
        outside_region_scan_transit_m = 0.0
        ids = [s['segment_id'] for s in segments]
        self.check('execution', len(ids) == len(set(ids)), loc, 'Unique segment identifiers')
        for index, seg in enumerate(segments):
            sloc = loc + '/' + seg['segment_id']
            phase = seg['phase']
            a, b = np.array(seg['from_nwz_m']), np.array(seg['to_nwz_m'])
            horiz = distance(a[:2], b[:2])
            vert = abs(float(a[2] - b[2]))
            self.near('continuity', distance(a, previous), 0, sloc + '/position', COORD_TOL_M)
            self.near('continuity', seg['start_s'], clock, sloc + '/start', TIME_TOL_S)
            self.near('time', seg['end_s'] - seg['start_s'], seg['duration_s'], sloc, TIME_TOL_S)
            self.near('distance', seg['horizontal_distance_m'], horiz, sloc + '/horizontal', COORD_TOL_M)
            self.near('distance', seg['vertical_distance_m'], vert, sloc + '/vertical', COORD_TOL_M)
            self.check('execution', not seen_return or phase == 'return', sloc, 'Return must be terminal phase')
            line = Point(a[:2]) if horiz < 1e-12 else LineString([a[:2], b[:2]])
            outer_distance = line.distance(boundary.boundary)
            min_outer_clearance = min(min_outer_clearance, outer_distance)
            self.check('flight_clearance', boundary.covers(line) and outer_distance >= 5 - NUMERIC_EPS_M,
                       sloc, 'Whole centerline must stay inside boundary and at least 5 m away', outer_distance, 5)
            for nf in nofly:
                clearance = line.distance(nf)
                min_nofly_clearance = min(min_nofly_clearance, clearance)
                self.check('flight_clearance', clearance >= 5 - NUMERIC_EPS_M, sloc,
                           'Whole segment must clear no-fly geometry by 5 m', clearance, 5)
            if phase == 'scan':
                seen_scan = True
                wid, lid, pass_index = seg['waypoint_id'], seg['layer_id'], seg['pass_index']
                self.check('execution', wid in waypoints, sloc, 'Scan references a canonical independent waypoint')
                if wid in waypoints:
                    self.near('execution', distance(a, waypoints[wid]['point']), 0, sloc, COORD_TOL_M)
                    self.check('execution', lid == waypoints[wid]['layer'], sloc, 'Scan layer matches waypoint')
                self.near('execution', distance(a, b), 0, sloc, COORD_TOL_M, 'Scan position does not move')
                self.check('execution', seg['speed_mps'] is None, sloc, 'Hover scan has no travel speed')
                actual_scans.append((lid, wid, pass_index))
                expected_duration = params['scan_seconds']
                self.expected_visits.append(dict(zip(VISIT_COLUMNS,
                    [sid, uid, len(actual_scans), lid, pass_index, wid, seg['start_s'], seg['end_s']])))
            else:
                self.check('execution', seg['waypoint_id'] is None and seg['pass_index'] is None,
                           sloc, 'Transit/auxiliary points must not trigger scans')
                if phase == 'takeoff':
                    takeoff_count += 1
                    self.check('execution', index == 0 and horiz <= NUMERIC_EPS_M and a[2] == 0 and b[2] > 0,
                               sloc, 'Single vertical takeoff must begin at t=0')
                    self.near('semantics', b[2], uav['layers'][0]['altitude_m'], sloc, 1e-9)
                    expected_speed = params['takeoff_vertical_speed_mps']
                    expected_duration = vert / expected_speed
                elif phase == 'return':
                    seen_return = True
                    self.check('execution', len(actual_scans) == len(expected_scans), sloc, 'Return begins only after every scan')
                    if vert > NUMERIC_EPS_M:
                        self.check('return_climb', sid != 'final_scene_1' and last_phase == 'scan' and horiz <= NUMERIC_EPS_M and b[2] > a[2], sloc, 'Dual-layer return begins with one in-place upward climb')
                        self.near('return_climb', a[2], uav['layers'][-1]['altitude_m'], sloc, NUMERIC_EPS_M)
                        self.near('return_climb', b[2], uav['layers'][0]['altitude_m'], sloc, NUMERIC_EPS_M)
                        expected_speed = params['return_climb_mps']
                        expected_duration = vert / expected_speed
                    else:
                        expected_speed = params['horizontal_speed_mps']
                        expected_duration = horiz / expected_speed
                        self.near('return_climb', a[2], uav['layers'][0]['altitude_m'], sloc, NUMERIC_EPS_M)
                        self.near('return_climb', b[2], uav['layers'][0]['altitude_m'], sloc, NUMERIC_EPS_M)
                elif phase == 'layer_transition':
                    transition_segments.append(seg)
                    self.check('execution', sid != 'final_scene_1', sloc, 'Fixed-layer scene cannot change layer')
                    self.check('execution', not (vert > NUMERIC_EPS_M and horiz > NUMERIC_EPS_M), sloc,
                               'Transition uses vertical descent then horizontal travel, never simultaneous motion')
                    self.check('execution', b[2] <= a[2], sloc, 'Layer change must descend')
                    expected_speed = params['interlayer_descent_mps'] if vert > NUMERIC_EPS_M else params['horizontal_speed_mps']
                    expected_duration = vert / params['interlayer_descent_mps'] + horiz / params['horizontal_speed_mps']
                else:
                    self.near('execution', vert, 0, sloc, NUMERIC_EPS_M, 'Ordinary transit stays at fixed layer altitude')
                    expected_speed = params['horizontal_speed_mps']
                    expected_duration = horiz / expected_speed
                    if phase == 'entry':
                        self.check('execution', not seen_scan, sloc, 'Entry must precede all scans')
                        self.near('execution', b[2], uav['layers'][0]['altitude_m'], sloc, NUMERIC_EPS_M)
                    elif phase == 'scan_transit':
                        self.check('execution', seen_scan, sloc, 'Scan transit follows a scan')
                        if seg['layer_id'] in layers:
                            self.near('execution', b[2], layers[seg['layer_id']]['altitude_m'], sloc, NUMERIC_EPS_M)
                        else:
                            self.check('execution', False, sloc, 'Scan transit requires a valid layer')
                        outside_region_scan_transit_m += line.difference(region).length
                    elif phase == 'return':
                        seen_return = True
                        self.check('execution', len(actual_scans) == len(expected_scans), sloc, 'Return begins only after every scan')
                self.near('semantics', seg['speed_mps'] if seg['speed_mps'] is not None else -1,
                          expected_speed, sloc + '/speed', 1e-9)
                if phase in ('entry', 'scan_transit', 'layer_transition', 'return'):
                    self.feature(sid, uid, seg['layer_id'], phase, LineString([a, b]), seg['segment_id'])
            self.near('time', seg['duration_s'], expected_duration, sloc + '/duration', TIME_TOL_S)
            times[phase] += expected_duration
            distances[phase] += horiz
            phase_paths[phase].append((a[:2], b[:2]))
            previous, clock, last_phase = b, seg['end_s'], phase
        self.check('execution', takeoff_count == 1 and segments[0]['phase'] == 'takeoff', loc, 'Exactly one initial takeoff')
        self.check('execution', actual_scans == expected_scans, loc, 'Actual scan sequence must exactly equal expanded required sequence', actual_scans, expected_scans)
        self.check('execution', last_phase == 'return', loc, 'Plan must end with final return')
        self.near('execution', distance(previous[:2], home), 0, loc + '/return_home', COORD_TOL_M)
        self.near('return_climb', previous[2], uav['layers'][0]['altitude_m'], loc + '/return_height', 1e-9)
        return_actions = [s for s in segments if s['phase'] == 'return']
        vertical_returns = [s for s in return_actions if abs(s['to_nwz_m'][2]-s['from_nwz_m'][2]) > NUMERIC_EPS_M]
        return_climb_s = sum(abs(s['to_nwz_m'][2]-s['from_nwz_m'][2])/params['return_climb_mps'] for s in vertical_returns)
        if sid != 'final_scene_1':
            self.check('return_climb', len(vertical_returns) == 1 and return_actions and vertical_returns[0] is return_actions[0], loc, 'Exactly one initial return ascent is required')
            if return_actions:
                self.near('return_climb', distance(return_actions[0]['from_nwz_m'], waypoints[uav['layers'][-1]['waypoint_ids'][-1]]['point']), 0, loc, COORD_TOL_M)
            upper_points = [q for other in scene['uavs'] for layer in other['layers'] if layer['layer_id']=='upper' for q in layer['waypoints_m']]
            return_vertices = [s['to_nwz_m'][:2] for s in return_actions]
            self.check('return_route_connection', any(distance(v,q)<=COORD_TOL_M for v in return_vertices for q in upper_points), loc, 'Upper-altitude return must explicitly join a canonical upper waypoint')
        else:
            self.check('return_climb', not vertical_returns, loc, 'Fixed-layer scene returns at its own assigned height without a climb')
        self.validate_scan_segment_realization(scene, uav)
        if sid == 'final_scene_1':
            self.check('execution', uav['layer_transition'] is None and not transition_segments, loc, 'No layer transition in scene A')
        else:
            upper, lower = layers['upper'], layers['lower']
            transition = uav['layer_transition']
            self.check('layer_transition', transition is not None and bool(transition_segments), loc, 'Dual-layer mission needs an explicit transition')
            if transition is not None and transition_segments:
                from_id, to_id = upper['waypoint_ids'][-1], lower['waypoint_ids'][0]
                a, b = waypoints[from_id]['point'], waypoints[to_id]['point']
                actual_horizontal = sum(s['horizontal_distance_m'] for s in transition_segments)
                actual_vertical = sum(s['vertical_distance_m'] for s in transition_segments)
                self.check('layer_transition', transition['from_waypoint_id'] == from_id and transition['to_waypoint_id'] == to_id,
                           loc, 'Upper last point connects to lower first point')
                self.near('layer_transition', distance(transition_segments[0]['from_nwz_m'], a), 0, loc, COORD_TOL_M)
                self.near('layer_transition', distance(transition_segments[-1]['to_nwz_m'], b), 0, loc, COORD_TOL_M)
                first = transition_segments[0]
                self.check('layer_transition', first['horizontal_distance_m'] <= NUMERIC_EPS_M and first['vertical_distance_m'] > 0,
                           loc, 'First transition action must be stationary descent')
                self.near('layer_transition', first['to_nwz_m'][2], lower['altitude_m'], loc, NUMERIC_EPS_M)
                transition_positions = [i for i, s in enumerate(segments) if s['phase'] == 'layer_transition']
                self.check('layer_transition', transition_positions == list(range(min(transition_positions), max(transition_positions)+1)),
                           loc, 'Transition actions form one contiguous block')
                for key, expected in [('horizontal_distance_m', actual_horizontal), ('vertical_distance_m', upper['altitude_m']-lower['altitude_m']),
                                      ('descent_speed_mps', params['interlayer_descent_mps']), ('duration_s', times['layer_transition'])]:
                    self.near('layer_transition', transition[key], expected, loc + '/' + key, COORD_TOL_M if 'distance' in key else TIME_TOL_S)
                self.near('layer_transition', actual_vertical, upper['altitude_m']-lower['altitude_m'], loc, COORD_TOL_M)
                self.check('layer_transition', transition['preferred_limit_met'] == (actual_horizontal <= 20), loc, '20 m target flag uses actual route distance')
                if actual_horizontal > 20:
                    self.check('layer_transition', bool(transition['note'].strip()), loc, 'Missed soft target requires an explanation')
        if sid == 'xuchang_small_2':
            original = next(v for v in field['baseline_uavs'] if v['uav_id'] == uid)
            anchors = original['old_entry_path_m'][1:-1]
            for phase, required in [('entry', anchors), ('return', original['old_return_path_from_last_waypoint_m'][1:-1])]:
                path = phase_paths[phase]
                sequence = ([path[0][0]] + [b for _, b in path]) if path else []
                cursor = 0
                for anchor in required:
                    found = next((i for i in range(cursor, len(sequence)) if distance(sequence[i], anchor) <= COORD_TOL_M), None)
                    self.check('legacy_corridors', found is not None, loc + '/' + phase,
                               'Retain old intermediate corridor anchor in order', expected=anchor)
                    if found is not None:
                        cursor = found + 1
        duration = sum(times.values())
        timing = uav['timing']
        self.near('time', clock, duration, loc + '/timeline_total', TIME_TOL_S)
        for phase in PHASES:
            self.near('time', timing[phase + '_s'], times[phase], loc + '/timing/' + phase, TIME_TOL_S)
        self.near('time', timing['mission_time_s'], duration, loc + '/mission_time', TIME_TOL_S)
        self.near('time', timing['remaining_s'], params['mission_limit_s'] - duration, loc + '/remaining', TIME_TOL_S)
        self.near('time', timing['planning_reserve_s'], params['preferred_reserve_s'], loc + '/reserve', 0)
        self.near('time', timing['budget_with_reserve_s'], duration + params['preferred_reserve_s'], loc + '/with_reserve', TIME_TOL_S)
        self.check('time_budget', duration <= params['mission_limit_s'], loc, 'Hard budget must not be relaxed by tolerance', duration, params['mission_limit_s'])
        self.check('time', timing['within_limit'] == (duration <= params['mission_limit_s']), loc, 'Hard-budget status flag')
        self.check('time', timing['within_planning_target'] == (duration <= params['mission_limit_s']-params['preferred_reserve_s']), loc, 'Planning-margin flag')
        if outside_region_scan_transit_m > 0.01:
            self.warnings.append({'location': loc, 'kind': 'scan_transit_outside_own_region',
                                  'distance_m': outside_region_scan_transit_m,
                                  'note': 'Own-region scan transit is a soft preference; global flight legality was checked separately.'})
        return {'uav_id': uid, 'mission_time_s': duration, 'remaining_s': params['mission_limit_s']-duration,
                'within_limit': duration <= params['mission_limit_s'], 'phase_times_s': dict(times),
                'horizontal_distances_m': dict(distances), 'scan_visits': len(actual_scans),
                'minimum_outer_clearance_m': min_outer_clearance,
                'minimum_no_fly_clearance_m': None if math.isinf(min_nofly_clearance) else min_nofly_clearance,
                'scan_transit_outside_own_region_m': outside_region_scan_transit_m, 'return_climb_s': return_climb_s, 'return_climb_sensitivity': {str(speed): duration-return_climb_s+(uav['layers'][0]['altitude_m']-uav['layers'][-1]['altitude_m'])/speed for speed in self.revision['return_climb_rate']['sensitivity_mps']}}

    def validate_csv(self, filename, expected, columns):
        path = self.result_path.parent / filename
        self.check('artifacts', path.exists(), filename, 'Required export is missing')
        if not path.exists():
            return
        with path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.check('csv_consistency', reader.fieldnames == columns, filename, 'Exact prescribed CSV column order', reader.fieldnames, columns)
        self.check('csv_consistency', len(rows) == len(expected), filename, 'CSV row count equals canonical data', len(rows), len(expected))
        keys = ['scene_id', 'uav_id', 'layer_id', 'waypoint_id']
        if filename == 'scan_visits.csv':
            keys = ['scene_id', 'uav_id', 'visit_index_1based']
        def rowkey(row):
            return tuple(str(row.get(k)) for k in keys)
        lookup = {rowkey(r): r for r in rows}
        self.check('csv_consistency', len(lookup) == len(rows), filename, 'No duplicate CSV row keys')
        for e in expected:
            key = rowkey(e)
            self.check('csv_consistency', key in lookup, filename + '/' + repr(key), 'Required canonical row exists')
            if key not in lookup:
                continue
            actual = lookup[key]
            for column in columns:
                value = e[column]
                if isinstance(value, str):
                    self.check('csv_consistency', actual.get(column) == value, filename + '/' + repr(key) + '/' + column, 'Canonical string equality')
                else:
                    try:
                        numeric = float(actual[column])
                        tol = 1e-10 if column in ('latitude', 'longitude') else 1e-5
                        self.near('csv_consistency', numeric, value, filename + '/' + repr(key) + '/' + column, tol)
                    except (KeyError, TypeError, ValueError):
                        self.check('csv_consistency', False, filename + '/' + repr(key) + '/' + column, 'Invalid numeric CSV value')

    def validate_geojson(self):
        path = self.result_path.parent / 'planning_result.geojson'
        self.check('artifacts', path.exists(), path.name, 'Required GeoJSON export is missing')
        if not path.exists():
            return
        collection = read_json(path)
        self.check('geojson_consistency', collection.get('type') == 'FeatureCollection', path.name, 'GeoJSON FeatureCollection required')
        scene_lookup = {s['scene_id']: s for s in self.result['scenes']}
        actual = []
        for index, feature in enumerate(collection.get('features', [])):
            loc = path.name + '/feature' + str(index)
            prop = feature.get('properties', {})
            self.check('geojson_consistency', set(['scene_id','uav_id','layer_id','kind','id']).issubset(prop), loc, 'Required Feature metadata')
            sid = prop.get('scene_id')
            self.check('geojson_consistency', sid in scene_lookup, loc, 'Feature scene ID exists')
            if sid not in scene_lookup:
                continue
            geom = feature.get('geometry')
            self.check('geojson_consistency', geom is not None, loc, 'Feature geometry cannot be null')
            if geom is None:
                continue
            for ring in polygon_rings(geom):
                self.check('geojson_consistency', ring[0] == ring[-1], loc, 'Export polygon rings must be closed')
            mg = geometry_m(geom, scene_lookup[sid]['projection'])
            # GEOS ignores Z and calls a purely vertical 3D LineString invalid;
            # GeoJSON legitimately represents descent with two different Z values.
            vertical_3d = (mg.geom_type == 'LineString' and mg.has_z and len(mg.coords) >= 2
                           and any(distance(a, b) > NUMERIC_EPS_M for a, b in zip(mg.coords, list(mg.coords)[1:])))
            self.check('geojson_consistency', mg.is_valid or vertical_3d, loc, 'Export geometry must be valid, including explicit vertical 3D lines')
            actual.append((prop, mg))
        used = set()
        for e in self.expected_features:
            key = (e['scene_id'], e['uav_id'], e['layer_id'], e['kind'])
            if e['geometry_m'].is_empty:
                continue
            candidates = [(i, prop, g) for i, (prop, g) in enumerate(actual)
                          if i not in used and (prop.get('scene_id'), prop.get('uav_id'), prop.get('layer_id'), prop.get('kind')) == key
                          and (e['id'] is None or prop.get('id') == e['id'])]
            loc = 'planning_result.geojson/' + repr(key) + '/' + str(e['id'])
            self.check('geojson_consistency', bool(candidates), loc, 'Required feature matching canonical JSON exists')
            if not candidates:
                continue
            # Geometry selection allows several exclusions with identical metadata except IDs.
            idx, prop, g = (candidates[0] if len(candidates) == 1 else min(candidates, key=lambda row: row[2].hausdorff_distance(e['geometry_m'])))
            used.add(idx)
            expected = e['geometry_m']
            if expected.geom_type in ('Polygon', 'MultiPolygon'):
                self.same_geom(g, expected, loc, 'geojson_consistency')
            else:
                self.check('geojson_consistency', g.geom_type == expected.geom_type, loc, 'Point/route geometry type must match canonical object')
                self.near('geojson_consistency', g.length, expected.length, loc + '/length', COORD_TOL_M)
                self.check('geojson_consistency', g.has_z == expected.has_z, loc, 'Point/route relative altitude must be preserved')
                if g.geom_type == expected.geom_type:
                    ac, ec = list(g.coords), list(expected.coords)
                    self.check('geojson_consistency', len(ac) == len(ec), loc, 'Canonical route vertices preserved')
                    if len(ac) == len(ec):
                        for a, b in zip(ac, ec):
                            self.near('geojson_consistency', distance(a, b), 0, loc + '/nwz', COORD_TOL_M)
        extras = [(prop.get('kind'), prop.get('id')) for i, (prop, _) in enumerate(actual) if i not in used]
        self.check('geojson_consistency', not extras, path.name, 'No unmatched or duplicate export features', extras[:30], [])

    def run(self):
        errors = sorted(Draft202012Validator(self.schema).iter_errors(self.result), key=lambda e: str(list(e.path)))
        self.check('schema', not errors, 'planning_result.json', 'Draft 2020-12 schema compliance')
        for error in errors[:100]:
            self.failures.append({'category': 'schema', 'location': '/'.join(map(str,error.path)), 'detail': error.message})
        self.check('provenance', self.result.get('source_inputs_sha256') == self.input_snapshot_sha256, 'source_inputs_sha256', 'Exact source input SHA256')
        old_input=read_json(self.input_path.parent/'v1'/'02_规划输入.json')
        for field_key in ('final_field','xuchang_field'):
            for key in ('projection','boundary_m','reference_home'):
                self.check('revision_input', self.input[field_key][key] == old_input[field_key][key], field_key+'/'+key, 'Revision must preserve the original fixed coordinate projection, boundary and nominal home')
        self.check('revision_input', [u['polygon_m'] for u in self.input['xuchang_field']['baseline_uavs']] == [u['polygon_m'] for u in old_input['xuchang_field']['baseline_uavs']], 'xuchang_field/baseline_uavs', 'Revision must not alter the inherited six aircraft partitions')
        confirmed = self.revision['confirmed_overrides']
        common = self.input['common_parameters']
        for key in ('upper_scan_radius_m','lower_scan_radius_m','scan_seconds','horizontal_speed_mps','takeoff_vertical_speed_mps','mission_limit_s','preferred_reserve_s'):
            self.near('revision_input', common[key], confirmed[key], 'common_parameters/'+key, 0)
        for spec in self.input['scenes']:
            if spec['scene_id']=='final_scene_1':
                for lid in ('upper','lower'):
                    for uid,values in spec[lid+'_uavs'].items():
                        self.near('revision_input',values['radius_m'],common[lid+'_scan_radius_m'],spec['scene_id']+'/'+uid+'/'+lid,0)
        self.near('return_climb_assumption', common['return_climb_mps'], self.revision['return_climb_rate']['proposed_nominal_mps'], 'return_climb_mps', 0)
        self.check('return_climb_assumption', common.get('return_climb_status') == 'planner_provisional_unconfirmed', 'return_climb_status', 'Return ascent speed must remain an explicitly unconfirmed planner assumption')
        self.check('coverage', isinstance(self.quad_segs, int) and self.quad_segs >= 128, 'planner.parameters.circle_quad_segs', 'Recorded circle approximation must use at least 128 segments per quadrant')
        serialized = json.dumps(self.result, ensure_ascii=False)
        self.check('provisional_status', '"mission_id"' not in serialized and '"assignment_checksum"' not in serialized,
                   'planning_result.json', 'Offline plan must not invent real dispatched mission identifiers')
        if errors:
            return
        self.check('semantics', sorted(s['scene_id'] for s in self.result['scenes']) == sorted(SCENE_IDS), 'scenes', 'Exactly the three prescribed scenes')
        for scene in self.result['scenes']:
            try:
                self.validate_scene(scene)
            except Exception as exc:
                self.check('validator_exception', False, scene['scene_id'], type(exc).__name__ + ': ' + str(exc))
                self.warnings.append({'location': scene['scene_id'], 'traceback': traceback.format_exc()})
        final_scenes = {s['scene_id']: s for s in self.result['scenes'] if s['scene_id'].startswith('final')}
        if len(final_scenes) == 2:
            a, b = final_scenes['final_scene_1'], final_scenes['final_scene_2']
            self.check('semantics', a['boundary_geometry_wgs84'] == b['boundary_geometry_wgs84'], 'final_scenes', 'A/B use identical outer boundary')
            self.check('exclusion', a['exclusions'] == b['exclusions'], 'final_scenes', 'A/B use identical terrain and strong scan exclusion geometry')
        if not self.skip_exports:
            self.validate_csv('waypoints.csv', self.expected_waypoints, WAYPOINT_COLUMNS)
            self.validate_csv('scan_visits.csv', self.expected_visits, VISIT_COLUMNS)
            self.validate_geojson()
        else:
            self.warnings.append({'kind': 'candidate_json_only', 'note': 'CSV/GeoJSON export consistency was explicitly skipped for this standalone candidate; JSON geometry, semantics and nominal timing were checked.'})
        self.warnings.append({'kind': 'offline_scope_only', 'note': 'Checks apply only to stored candidate boundaries and nominal parameters. Actual exclusion boundaries, real aircraft homes, wind/acceleration/tracking delays, descent/return-ascent performance, simultaneous multi-aircraft separation and executable program adapters are not flight-validated.'})

    def report(self):
        for path,expected in [(self.result_path,self.result_snapshot_sha256),(self.input_path,self.input_snapshot_sha256),(self.schema_path,self.schema_snapshot_sha256),(self.revision_path,self.revision_snapshot_sha256)]:
            self.check('provenance',sha256(path)==expected,str(path),'File changed during validation; rerun against stable files')
        return {'validator_version': '2.0.0', 'independent_of_planner': True, 'csv_geojson_exports_validated': not self.skip_exports,
                'result_sha256': self.result_snapshot_sha256, 'source_inputs_sha256': self.input_snapshot_sha256,
                'schema_sha256': self.schema_snapshot_sha256, 'revision_requirements_sha256': self.revision_snapshot_sha256,
                'compatibility_scope': 'Actual scan-array direct segments and nominal return climb verified; catalog and topic mapping require their separate offline validator; no runtime integration or flight test.',
                'passed': not self.failures, 'flight_ready': False,
                'meaning_of_passed': 'Offline stored-candidate geometry, semantics, exports and nominal timing only; never flight approval.',
                'tolerances': {'coordinate_m': COORD_TOL_M, 'area_m2': AREA_TOL_M2,
                               'time_s': TIME_TOL_S, 'ratio': RATIO_TOL,
                               'flight_clearance_numeric_epsilon_m': NUMERIC_EPS_M,
                               'circle_quad_segs': self.quad_segs,
                               'hard_time_budget_tolerance_s': 0,
                               'strong_scan_check': 'Exact point-to-polygon distance >= radius + 0.000001 m'},
                'checks_by_category': dict(self.checks), 'total_checks': sum(self.checks.values()),
                'failure_count': len(self.failures), 'failures': self.failures,
                'warnings': self.warnings, 'scenes': self.scenes, 'independent_redundancy_audit': self.redundancy_summaries,
                'dependencies': {'python': sys.version.split()[0], 'shapely': shapely.__version__, 'numpy': np.__version__}}


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, default=base/'planning_result.json')
    parser.add_argument('--input', type=Path, default=base/'inputs'/'02_规划输入_v2.json')
    parser.add_argument('--schema', type=Path, default=base/'inputs'/'03_规划输出.schema.json')
    parser.add_argument('--output', type=Path, default=base/'validation_report.json')
    parser.add_argument('--skip-exports', action='store_true', help='Validate a standalone candidate JSON; explicitly omit CSV/GeoJSON cross-checks.')
    args = parser.parse_args()
    try:
        validator = Validator(args.result, args.input, args.schema, skip_exports=args.skip_exports)
        validator.run()
        report = validator.report()
    except Exception as exc:
        report = {'validator_version': '2.0.0', 'passed': False, 'flight_ready': False,
                  'failure_count': 1, 'failures': [{'category': 'load_or_runtime_error', 'detail': str(exc)}],
                  'traceback': traceback.format_exc()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plain(report), ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'flight_ready': False, 'failure_count': report['failure_count'],
                      'total_checks': report.get('total_checks', 0), 'report': str(args.output)}, ensure_ascii=False))
    if not report['passed']:
        for failure in report['failures'][:15]:
            print(json.dumps(failure, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
