import copy
import json
import math
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon
from competition_backend.transit_routes import CACHE, PROFILES, attach_routes, scene_plan, shortest_routes, rebase_gps_routes_for_takeoff
from competition_backend.api import create_app

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/su17_competition_executor/src'))
from su17_competition_executor.transit_protocol import route_messages
from su17_competition_executor.task_protocol import validate_assignment, assignment_checksum, TaskValidationError


class TransitTest(unittest.TestCase):
    @staticmethod
    def _scene_cases():
        for profile in PROFILES:
            if profile == 'subject1_actual':
                yield profile, 'stadium_center', ('gps',)
            elif profile == 'dalian_nanshan':
                yield profile, 'fixed_dalian', ('gps',)
            elif profile == 'xuchang_small':
                yield profile, 'fixed_xuchang', ('gps',)
            else:
                for departure in ('southeast', 'stadium_center'):
                    yield profile, departure, ('gps', 'xyz')

    def test_all_saved_routes_stay_inside_after_entry_and_reverse_to_departure(self):
        saved = json.loads(CACHE.read_text(encoding='utf-8'))['scenes']
        self.assertEqual(len(saved), 15)
        for key, scene in saved.items():
            with self.subTest(scene=key):
                region = Polygon(scene['boundary_m']).buffer(2.1e-5)
                home = scene['departure_m']
                boundary = Polygon(scene['boundary_m'])
                for vehicle in scene['vehicles'].values():
                    self.assertEqual(vehicle['entry_path_m'], list(reversed(vehicle['return_paths_m'][0])))
                    self.assertEqual(len(vehicle['return_paths_m']), len(vehicle['waypoints_m']))
                    for scan, route in zip(vehicle['waypoints_m'], vehicle['return_paths_m']):
                        self.assertEqual(route[0], scan)
                        self.assertEqual(route[-1], home)
                        if scene.get('boundary_clearance_m'):
                            self.assertEqual(scene['boundary_clearance_m'], 5)
                            checked_route = (route if boundary.covers(Point(home)) and
                                             Point(home).distance(boundary.boundary) >= 5 else route[:-1])
                            geometry = LineString(checked_route) if len(checked_route)>1 else Point(checked_route[0])
                            self.assertGreaterEqual(geometry.distance(boundary.boundary),5)
                        for a, b in zip(route[:-2], route[1:-1]):
                            self.assertTrue(region.covers(LineString([a, b])))
                        final = LineString(route[-2:])
                        intersection = final.intersection(region)
                        self.assertIn(intersection.geom_type, ('LineString', 'Point'))

    def test_shortest_route_does_not_cut_across_concave_notch(self):
        polygon = [[0,0],[5,0],[5,5],[4,5],[4,1],[1,1],[1,5],[0,5]]
        route = shortest_routes(polygon, [.5,4], [[4.5,4]])[0]
        self.assertGreater(len(route), 2)
        self.assertTrue(Polygon(polygon).buffer(2.1e-5).covers(LineString(route)))

    def test_live_gps_takeoff_rebases_routes_without_moving_scan_points(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            try:
                endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
                cases = [(profile, departure) for profile, departure, _ in self._scene_cases()]
                for profile, departure in cases:
                    with self.subTest(profile=profile):
                        plan=endpoint(dict(subject='subject1',flight_profile=profile,departure_point=departure,
                                           coordinate_mode='gps',gps_origin={'latitude':39.1,'longitude':121.6}))
                        area=plan['search_area']
                        for uid, wrapper in plan['planned_uavs'].items():
                            task=wrapper['task']
                            before=copy.deepcopy(task)
                            origin=task['transit_routes']['departure']
                            home={'latitude':origin[1]+int(uid)*0.000001,'longitude':origin[0]+int(uid)*0.000001}
                            routed=rebase_gps_routes_for_takeoff(area,task,home)
                            self.assertEqual(routed['departure'],[home['longitude'],home['latitude']])
                            self.assertEqual(task,before)
                            checked=dict(task=dict(task,transit_routes=routed),uav_id=int(uid),mission_id='m',
                                         assignment_checksum='sum',coordinate_frame='WGS84',target_altitude_m=8.)
                            entry,returns=route_messages(checked)
                            self.assertEqual(entry['path'][0][:2],routed['departure'])
                            self.assertTrue(all(r['path'][-1][:2]==routed['departure'] for r in returns['routes']))
                            if routed.get('boundary_clearance_m'):
                                saved = json.loads(CACHE.read_text(encoding='utf-8'))['scenes'][profile+'/'+departure]
                                boundary = Polygon(saved['boundary_m'])
                                scans, geo = task['waypoints_m'],task['waypoints_wgs84']
                                projection = area['coverage']['projection']
                                factors=[1 / projection['north_m_per_degree'],
                                         -1 / projection['west_m_per_degree']]
                                for axis in (0,1):
                                    low=min(range(len(scans)),key=lambda i:scans[i][axis])
                                    high=max(range(len(scans)),key=lambda i:scans[i][axis])
                                    span=scans[high][axis]-scans[low][axis]
                                    if span>1e-6:
                                        factors[axis]=(geo[high][axis]-geo[low][axis])/span
                                def local(point):
                                    return [scans[0][0]+(point[1]-geo[0][0])/factors[0],
                                            scans[0][1]+(point[0]-geo[0][1])/factors[1]]
                                home_local=local(routed['departure'])
                                whole_safe = boundary.covers(Point(home_local)) and Point(home_local).distance(boundary.boundary)>=5
                                self.assertEqual(returns['boundary_clearance_m'],5)
                                self.assertEqual(entry['endpoint_clearance_policy'],'takeoff_landing_connector_only')
                                for route in returns['routes']:
                                    points=[local(p) for p in route['path']]
                                    points=points if whole_safe else points[:-1]
                                    geometry=LineString(points) if len(points)>1 else Point(points[0])
                                    self.assertTrue(boundary.covers(geometry))
                                    self.assertGreaterEqual(geometry.distance(boundary.boundary),5)
            finally:
                app.state.audit.close()

    def test_every_scene_coordinate_and_altitude_publishes_consistent_routes(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            cleanup.callback(app.state.audit.close)
            endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
            for profile, departure, modes in self._scene_cases():
                    for mode in modes:
                        with self.subTest(profile=profile, departure=departure, mode=mode):
                            plan = endpoint(dict(subject='subject1',flight_profile=profile,departure_point=departure,coordinate_mode=mode,
                                                 gps_origin={'latitude':39.1,'longitude':121.6}))
                            for uid, wrapper in plan['planned_uavs'].items():
                                for altitude in (.5, 1.5, 5., 8., 10., 12., 45.):
                                    assignment=dict(task=wrapper['task'],uav_id=int(uid),mission_id='m',assignment_checksum='sum',
                                                    coordinate_frame='WGS84' if mode=='gps' else 'ENU',target_altitude_m=altitude)
                                    entry, returns = route_messages(assignment)
                                    self.assertEqual(entry['schema_version'], 1)
                                    self.assertEqual(returns['schema_version'], 2)
                                    self.assertEqual(returns['uav_id'], int(uid))
                                    expected_keys = [(int(source), i + 1) for source, vehicle in
                                                     sorted(plan['planned_uavs'].items(), key=lambda pair:int(pair[0]))
                                                     for i in range(len(vehicle['task']['waypoints_m']))]
                                    self.assertEqual([(r['source_uav_id'],r['waypoint_index']) for r in returns['routes']], expected_keys)
                                    own_first = next(r for r in returns['routes'] if r['source_uav_id']==int(uid) and r['waypoint_index']==1)
                                    self.assertEqual(entry['path'][-1], own_first['path'][0])
                                    for route in returns['routes']:
                                        source_task = plan['planned_uavs'][str(route['source_uav_id'])]['task']
                                        p = source_task['waypoints_wgs84' if mode=='gps' else 'waypoints_m'][route['waypoint_index']-1]
                                        expected = [p[1],p[0]] if mode=='gps' else p
                                        self.assertEqual(route['path'][0][:2], expected)
                                        self.assertEqual(route['path'][-1], returns['landing_point'])
                                    for route in [entry['path']] + [r['path'] for r in returns['routes']]:
                                        self.assertTrue(all(p[2] == altitude for p in route))
                                    self.assertEqual(entry['path'][0], returns['routes'][-1]['path'][-1])

    def test_dispatched_landing_and_transit_start_are_same_for_every_scene(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            cleanup.callback(app.state.audit.close)
            endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
            for profile, departure, modes in self._scene_cases():
                    for mode in modes:
                        with self.subTest(profile=profile,departure=departure,mode=mode):
                            origin={'latitude':39.1,'longitude':121.6}
                            plan=endpoint(dict(subject='subject1',flight_profile=profile,departure_point=departure,
                                               coordinate_mode=mode,gps_origin=origin))
                            app.state.adapter.commands.clear()
                            app.state.orchestrator.plan('subject1',flight_profile=profile,prepared_plan=plan,
                                                        controller_mode='external',gps_origin=origin)
                            assignments=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                            self.assertEqual(len(assignments),6)
                            for command in assignments:
                                assignment=dict(command['payload'],type='assign_task',uav_id=command['uav_id'])
                                checked=validate_assignment(assignment,command['uav_id'])
                                entry,_=route_messages(checked)
                                landing=assignment['landing_point_wgs84'] if mode=='gps' else assignment['landing_point_m']
                                expected=[landing[1],landing[0]] if mode=='gps' else landing
                                self.assertEqual(entry['path'][0][:2],expected)
                                if profile=='dalian_nanshan':
                                    self.assertAlmostEqual(expected[0],121.65960216424625,places=8)
                                    self.assertAlmostEqual(expected[1],39.052820490783674,places=8)

    def test_single_connected_receiver_gets_all_six_sources_at_its_own_gps_home(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            try:
                endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
                plan = endpoint(dict(subject='subject1', flight_profile='lab', departure_point='stadium_center',
                                     coordinate_mode='gps',gps_origin={'latitude':39.1,'longitude':121.6}))
                before = copy.deepcopy(plan)
                point_count = sum(len(w['task']['waypoints_m']) for w in plan['planned_uavs'].values())
                # 六机各自分派只激活接收机，仍应包含其余五架的全部规划点。
                for uid in range(1,7):
                    home = {'latitude':39.1+uid*0.000001,'longitude':121.6-uid*0.000001}
                    receiver_plan = copy.deepcopy(plan)
                    receiver_plan['search_area']['takeoff_gps_by_uav'] = {str(uid): home}
                    app.state.adapter.commands.clear()
                    app.state.orchestrator.set_active_uav_ids([uid])
                    app.state.orchestrator.plan('subject1', flight_profile='lab', prepared_plan=receiver_plan,
                                                controller_mode='external',
                                                gps_origin={'latitude':39.1,'longitude':121.6})
                    commands = [c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                    self.assertEqual([c['uav_id'] for c in commands], [uid])
                    assignment = dict(commands[0]['payload'], type='assign_task', uav_id=uid)
                    checked = validate_assignment(assignment, uid)
                    entry, returns = route_messages(checked)
                    landing = [home['longitude'],home['latitude'],assignment['target_altitude_m']]
                    self.assertEqual(entry['path'][0], landing)
                    self.assertEqual(len(returns['routes']), point_count)
                    self.assertTrue(all(r['path'][-1]==landing for r in returns['routes']))
                    self.assertEqual(assignment['task']['waypoints_wgs84'], before['planned_uavs'][str(uid)]['task']['waypoints_wgs84'])
                    self.assertLess(len(json.dumps(assignment).encode()), 4*1024*1024)
                self.assertEqual(plan, before)
            finally:
                app.state.audit.close()

    def test_fleet_routes_reject_missing_sources_duplicates_wrong_receiver_and_home(self):
        plan = attach_routes(scene_plan('lab','southeast'))
        assignment = dict(task=plan['planned_uavs']['2']['task'], uav_id=2, mission_id='m',
                          assignment_checksum='sum', coordinate_frame='ENU', target_altitude_m=2.)
        changes = [lambda a: a['task']['transit_routes']['waypoints_by_uav'].pop('6'),
                   lambda a: a['task']['transit_routes']['return_paths'].pop(),
                   lambda a: a['task']['transit_routes']['return_paths'][1].update(waypoint_index=1),
                   lambda a: a['task']['transit_routes']['return_paths'][0].update(source_uav_id=2),
                   lambda a: a['task']['transit_routes'].update(recipient_uav_id=1),
                   lambda a: a['task']['transit_routes']['waypoints_by_uav']['2'][0].__setitem__(0,999),
                   lambda a: a.update(landing_point_m=[999.,999.]),
                   lambda a: a.update(landing_point_m=[float('nan'),0.])]
        for change in changes:
            altered = copy.deepcopy(assignment)
            change(altered)
            with self.assertRaises(ValueError):
                route_messages(altered)

    def test_legacy_single_vehicle_routes_remain_compatible(self):
        plan = attach_routes(scene_plan('lab','southeast'))
        task = plan['planned_uavs']['2']['task']
        routes = task['transit_routes']
        routes['schema_version'] = 1
        routes['return_paths'] = [dict(waypoint_index=r['waypoint_index'],path=r['path'])
                                  for r in routes['return_paths'] if r['source_uav_id']==2]
        for key in ('recipient_uav_id','route_scope','waypoints_by_uav'):
            routes.pop(key)
        entry, returns = route_messages(dict(task=task,uav_id=2,mission_id='legacy',assignment_checksum='sum',
                                             coordinate_frame='ENU',target_altitude_m=2.))
        self.assertEqual(returns['schema_version'],1)
        self.assertNotIn('source_uav_id',returns['routes'][0])
        self.assertEqual(entry['path'][-1],returns['routes'][0]['path'][0])

    def test_changed_waypoint_rejects_stale_transit_cache(self):
        plan = scene_plan('lab','southeast')
        next(iter(plan['planned_uavs'].values()))['task']['waypoints_m'][0][0] += .1
        with self.assertRaisesRegex(ValueError, '航点与固定'):
            attach_routes(plan)

    def test_invalid_route_index_and_endpoint_are_rejected(self):
        plan = attach_routes(scene_plan('lab','southeast'))
        task = next(iter(plan['planned_uavs'].values()))['task']
        assignment=dict(task=task,uav_id=1,mission_id='m',assignment_checksum='sum',coordinate_frame='ENU',target_altitude_m=1.5)
        for field, value in [('waypoint_index', 0), ('path', [[8,8],[0,0]])]:
            changed=copy.deepcopy(assignment)
            changed['task']['transit_routes']['return_paths'][0][field]=value
            with self.assertRaises(ValueError):
                route_messages(changed)
            changed.update(type='assign_task', controller_mode='external')
            changed['assignment_checksum']=assignment_checksum(changed)
            with self.assertRaises(TaskValidationError):
                validate_assignment(changed,1)
