import copy
import json
import math
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon
from competition_backend.transit_routes import CACHE, PROFILES, attach_routes, scene_plan, shortest_routes
from competition_backend.api import create_app

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/su17_competition_executor/src'))
from su17_competition_executor.transit_protocol import route_messages
from su17_competition_executor.task_protocol import validate_assignment, assignment_checksum, TaskValidationError


class TransitTest(unittest.TestCase):
    def test_all_saved_routes_stay_inside_after_entry_and_reverse_to_departure(self):
        saved = json.loads(CACHE.read_text(encoding='utf-8'))['scenes']
        self.assertEqual(len(saved), 13)
        for key, scene in saved.items():
            with self.subTest(scene=key):
                region = Polygon(scene['boundary_m']).buffer(2.1e-5)
                home = scene['departure_m']
                for vehicle in scene['vehicles'].values():
                    self.assertEqual(vehicle['entry_path_m'], list(reversed(vehicle['return_paths_m'][0])))
                    self.assertEqual(len(vehicle['return_paths_m']), len(vehicle['waypoints_m']))
                    for scan, route in zip(vehicle['waypoints_m'], vehicle['return_paths_m']):
                        self.assertEqual(route[0], scan)
                        self.assertEqual(route[-1], home)
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

    def test_every_scene_coordinate_and_altitude_publishes_consistent_routes(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            cleanup.callback(app.state.audit.close)
            endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
            for profile in PROFILES:
                for departure in (('fixed_dalian',) if profile == 'dalian_nanshan' else ('southeast','stadium_center')):
                    for mode in (('gps',) if profile == 'dalian_nanshan' else ('gps','xyz')):
                        with self.subTest(profile=profile, departure=departure, mode=mode):
                            plan = endpoint(dict(subject='subject1',flight_profile=profile,departure_point=departure,coordinate_mode=mode,
                                                 gps_origin={'latitude':39.1,'longitude':121.6}))
                            for uid, wrapper in plan['planned_uavs'].items():
                                for altitude in (.5, 1.5, 5., 8., 10., 12., 45.):
                                    assignment=dict(task=wrapper['task'],uav_id=int(uid),mission_id='m',assignment_checksum='sum',
                                                    coordinate_frame='WGS84' if mode=='gps' else 'ENU',target_altitude_m=altitude)
                                    entry, returns = route_messages(assignment)
                                    self.assertEqual(entry['path'][-1], returns['routes'][0]['path'][0])
                                    for route in [entry['path']] + [r['path'] for r in returns['routes']]:
                                        self.assertTrue(all(p[2] == altitude for p in route))
                                    self.assertEqual(entry['path'][0], returns['routes'][-1]['path'][-1])

    def test_dispatched_landing_and_transit_start_are_same_for_every_scene(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            app = create_app({'COMPETITION_ADAPTER':'sim', 'COMPETITION_DATA_DIR':root,
                              'COMPETITION_DIAGNOSTICS_DIR':root})
            cleanup.callback(app.state.audit.close)
            endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/planning/competition-coverage')
            for profile in PROFILES:
                for departure in (('fixed_dalian',) if profile == 'dalian_nanshan' else ('southeast','stadium_center')):
                    for mode in (('gps',) if profile == 'dalian_nanshan' else ('gps','xyz')):
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
