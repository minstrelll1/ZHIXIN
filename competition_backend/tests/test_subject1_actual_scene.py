import copy
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from competition_backend.models import Telemetry

from fastapi import HTTPException
from shapely.geometry import Polygon, Point, LineString, shape
from shapely.ops import unary_union
from competition_backend.api import create_app
from competition_backend.subject1_actual_scene import load_plan, PROFILE, OTHER_HOME_CLEARANCE_M
from competition_backend.transit_routes import attach_routes, rebase_gps_routes_for_takeoff
from competition_backend.landing_avoidance import routes_from_home
from su17_competition_executor.transit_protocol import route_messages
from su17_competition_executor.task_protocol import validate_assignment
from su17_competition_executor.boot_gps_home import BootGpsHome


def homes_for(plan):
    a = plan['search_area']; p = a['coverage']['projection']; d = a['departure_point_m']
    return {str(uid): dict(latitude=p['latitude'] + (d[0] + (uid-3.5)*8)/p['north_m_per_degree'],
                           longitude=p['longitude'] - d[1]/p['west_m_per_degree'],
                           boot_id='boot-'+str(uid), source='first_valid_unarmed_gps_per_boot') for uid in range(1,7)}


class ActualSceneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = attach_routes(load_plan())

    def test_fixed_boundary_full_coverage_and_five_metre_clearance(self):
        p = self.plan; a = p['search_area']; boundary = Polygon(a['points_m'])
        self.assertEqual(a['coordinate_mode'], 'gps')
        self.assertEqual(a['departure_point'], 'stadium_center')
        self.assertEqual(len(a['points']), 25)
        self.assertTrue(a['terrain']['enabled'])
        required = shape(a['terrain_layers_m']['required']); excluded = shape(a['terrain_layers_m']['excluded'])
        self.assertGreater(excluded.area, 70000)
        self.assertLess(boundary.symmetric_difference(required.union(excluded)).area, 1e-5)
        regions, disks = [], []
        for uid, wrapper in p['planned_uavs'].items():
            task = wrapper['task']; region = Polygon(task['polygon_m']); regions.append(region)
            route = LineString(task['flight_path_m'])
            self.assertTrue(boundary.covers(route)); self.assertGreaterEqual(route.distance(boundary.boundary), 5.)
            self.assertEqual(task['reconnaissance_radius_m'],45. if uid=='6' else 50.)
            scans = task['waypoints_m']
            self.assertGreater(len(scans), 5)
            for point in scans:
                self.assertFalse(excluded.buffer(-1e-7).contains(Point(point)), (uid, point))
            coverage = unary_union([Point(point).buffer(task['reconnaissance_radius_m']-.02, quad_segs=32) for point in scans])
            disks.append(coverage)
            self.assertLess(required.intersection(region).difference(coverage).area, 1e-5)
            self.assertAlmostEqual(task['mission_time_s'], route.length/5 + len(scans)*10, places=5)
        union = unary_union(regions)
        self.assertLess(boundary.symmetric_difference(union).area, 1e-5)
        self.assertLess(sum(r.area for r in regions)-union.area, 1e-5)
        self.assertLess(required.difference(unary_union(disks)).area, 1e-5)
        # 边缘5米是禁止航迹贴近，不是删除待侦察面积。
        self.assertLess(required.difference(boundary.buffer(-5)).difference(unary_union(disks)).area, 1e-5)

    def test_all_recipients_have_all_waypoint_returns_to_own_home_and_avoid_other_homes(self):
        p = copy.deepcopy(self.plan); a = p['search_area']; homes = homes_for(p); a['takeoff_gps_by_uav'] = homes
        projection = a['coverage']['projection']
        def local(q):
            return [(q[1]-projection['latitude'])*projection['north_m_per_degree'],
                    -(q[0]-projection['longitude'])*projection['west_m_per_degree']]
        boundary = Polygon(a['points_m'])
        for uid, item in p['planned_uavs'].items():
            routes = rebase_gps_routes_for_takeoff(a, item['task'], homes[uid])
            self.assertEqual(routes['other_home_clearance_m'], 2.5)
            self.assertEqual(routes['avoided_home_count'], 5)
            self.assertEqual(len(routes['return_paths']), sum(len(v['task']['waypoints_m']) for v in p['planned_uavs'].values()))
            home = [homes[uid]['longitude'], homes[uid]['latitude']]
            self.assertEqual(routes['entry_path'][0], home)
            for route in [routes['entry_path']] + [r['path'] for r in routes['return_paths']]:
                line = LineString([local(q) for q in route])
                self.assertGreaterEqual(line.distance(boundary.boundary), 5)
                for other_uid, other in homes.items():
                    if other_uid != uid:
                        self.assertGreaterEqual(line.distance(Point(local([other['longitude'], other['latitude']]))), OTHER_HOME_CLEARANCE_M)
            for returned in routes['return_paths']:
                self.assertEqual(returned['path'][-1], home)
            for altitude in (.5,1,1.5,2,2.5,4.5,5,5.5,8,10,12,40,45,50,44,47,53,56,59):
                assignment=dict(task=dict(item['task'],transit_routes=routes),uav_id=int(uid),mission_id='test',
                    assignment_checksum='sum',coordinate_frame='WGS84',target_altitude_m=altitude,
                    landing_point_wgs84=[home[1],home[0],altitude])
                entry, returned = route_messages(assignment)
                self.assertTrue(all(q[2]==altitude for r in returned['routes'] for q in r['path']))
                self.assertEqual(returned['landing_point'],[home[0],home[1],altitude])

    def test_avoidance_handles_direct_obstruction_and_rejects_conflicting_endpoints(self):
        b = [[0,0],[100,0],[100,100],[0,100]]
        paths = routes_from_home(b,[20,50],[[80,50]],[[50,50]])
        self.assertGreater(len(paths[0]),2)
        self.assertGreaterEqual(LineString(paths[0]).distance(Point(50,50)),2.5)
        with self.assertRaises(ValueError):
            routes_from_home(b,[20,50],[[80,50]],[[21,50]])
        with self.assertRaises(ValueError):
            routes_from_home(b,[20,50],[[80,50]],[[80,50]])
        with self.assertRaises(ValueError):
            routes_from_home(b,[3,50],[[80,50]],[])

    def test_api_fixed_geography_all_heights_and_no_runtime_coverage_solver(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':d,'COMPETITION_DIAGNOSTICS_DIR':d})
            try:
                endpoint=next(r.endpoint for r in app.routes if r.path=='/api/v1/planning/competition-coverage')
                payload=dict(subject='subject1',flight_profile=PROFILE,coordinate_mode='gps',departure_point='stadium_center',
                             gps_origin={'latitude':39.,'longitude':121.})
                with patch('competition_backend.subject1_actual_scene.prepare_plan',side_effect=AssertionError('不应实时求解扫描')):
                    for height in ('default','around1m','around2m','around5m','around10m','around45m','around54m','subject2_50m'):
                        p=endpoint(dict(payload,flight_altitude_plan=height))
                        self.assertEqual(p['search_area']['points'],self.plan['search_area']['points'])
                        self.assertEqual(p['search_area']['gps_origin'],self.plan['search_area']['gps_origin'])
                        self.assertEqual(p['flight_altitude_plan'],height)
                for changes in (dict(coordinate_mode='xyz'),dict(departure_point='southeast')):
                    with self.assertRaises(HTTPException):endpoint(dict(payload,**changes))
            finally:app.state.audit.close()

    def test_dispatch_validates_whole_fleet_before_first_send(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':d,'COMPETITION_DIAGNOSTICS_DIR':d})
            try:
                p=copy.deepcopy(self.plan); p['search_area']['takeoff_gps_by_uav']=homes_for(p)
                o=app.state.orchestrator
                result=o.plan('subject1',flight_profile=PROFILE,flight_altitude_plan='around54m',prepared_plan=p,controller_mode='external')
                commands=[v for v in app.state.adapter.snapshot() if v['type']=='assign_task']
                self.assertEqual(len(commands),6)
                for command in commands:
                    payload=dict(command['payload'],type='assign_task',uav_id=command['uav_id'])
                    checked=validate_assignment(payload,command['uav_id'])
                    entry,ret=route_messages(checked)
                    self.assertEqual(entry['path'][0],ret['landing_point'])
                    self.assertEqual(checked['target_altitude_m'],{1:59,2:56,3:53,4:50,5:47,6:44}[command['uav_id']])
                app.state.adapter.commands.clear()
                p['search_area']['takeoff_gps_by_uav']['6']=dict(p['search_area']['takeoff_gps_by_uav']['1'])
                with self.assertRaises(ValueError):
                    o.plan('subject1',flight_profile=PROFILE,prepared_plan=p,controller_mode='external')
                self.assertFalse(app.state.adapter.commands)
            finally:app.state.audit.close()


class BootHomeTest(unittest.TestCase):
    def test_wait_valid_gps_freeze_reconnect_restart_and_reset_on_new_boot(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'home.json'
            recorder=BootGpsHome(path,'boot1')
            args=dict(connected=True,armed=False,gps_status=3,location_source=5,
                      gps=dict(latitude=33.86446,longitude=113.70563,age_seconds=.1),now=100)
            self.assertIsNone(recorder.observe(**dict(args,armed=True)))
            self.assertIsNone(recorder.observe(**dict(args,gps_status=0)))
            self.assertIsNone(recorder.observe(**dict(args,gps=dict(args['gps'],age_seconds=3))))
            home=recorder.observe(**args)
            self.assertEqual(recorder.observe(**dict(args,connected=False)),home)
            moved=dict(args,gps=dict(args['gps'],latitude=34))
            self.assertEqual(recorder.observe(**moved),home)
            self.assertEqual(BootGpsHome(path,'boot1').observe(**moved),home)
            self.assertIsNone(BootGpsHome(path,'boot2').home)
            self.assertEqual(BootGpsHome(path,'boot2').observe(**moved)['latitude'],34)
            path.write_text('[]',encoding='utf-8')
            self.assertIsNone(BootGpsHome(path,'boot2').home)
            self.assertEqual(BootGpsHome(path,'boot2').observe(**args)['latitude'],args['gps']['latitude'])



class ActualSceneLiveHomeTest(unittest.TestCase):
    def test_connected_fleet_uses_frozen_home_and_rejects_prior_boot_or_missing_reference(self):
        with tempfile.TemporaryDirectory() as d, patch('competition_backend.api.time.time',return_value=1000.):
            app=create_app({'COMPETITION_ADAPTER':'distributed','COMPETITION_DATA_DIR':d,
                'COMPETITION_LOCAL_UAV_ID':'2','COMPETITION_GROUND_PEERS':''},audit=Mock())
            app.state.adapter.connected_uav_ids_snapshot=Mock(return_value=[1,2,4,5])
            app.state.adapter.assign_task=Mock(side_effect=AssertionError('preview must not send'))
            client=TestClient(app)  # No lifespan: never starts network connections.
            payload=dict(subject='subject1',flight_profile=PROFILE,coordinate_mode='gps',departure_point='stadium_center')
            homes=homes_for(load_plan())
            def update(uid,cap=None):
                home=homes[str(uid)]
                app.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,received_at=999.8,connected=True,
                    latitude=home['latitude']+.0001,longitude=home['longitude'],gps_status=3,location_source=4,
                    gps_telemetry_age_seconds=.1,capabilities=cap if cap is not None else
                    dict(boot_id=home['boot_id'],boot_gps_home=home)))
            for uid in (1,2,4,5):update(uid)
            response=client.post('/api/v1/planning/competition-coverage',json=payload)
            self.assertEqual(response.status_code,200,response.text)
            refs=response.json()['search_area']['takeoff_gps_by_uav']
            self.assertEqual(set(refs),{'1','2','4','5'})
            self.assertEqual(refs['1']['latitude'],homes['1']['latitude'])  # Never current moving fix.
            update(5,dict(boot_id='new-boot',boot_gps_home=homes['5']))
            response=client.post('/api/v1/planning/competition-coverage',json=payload)
            self.assertEqual(response.status_code,409);self.assertIn('UAV5',response.text)
            update(5,{})
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json=payload).status_code,409)
            update(5)
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json=payload).status_code,200)
            app.state.adapter.assign_task.assert_not_called()

if __name__=='__main__':unittest.main()
