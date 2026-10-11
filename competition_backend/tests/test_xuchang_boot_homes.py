"""许昌开机起降点、全机队返航避让及分派前原子检查。"""
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon
from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_backend.transit_routes import attach_routes, rebase_gps_routes_for_takeoff
from competition_backend.xuchang_small_scene import load_xuchang_small_plan
from competition_backend.landing_guided_avoidance import detour_guided_paths
from su17_competition_executor.transit_protocol import route_messages
from su17_competition_executor.task_protocol import validate_assignment


OFFSETS = {1:(0.,0.), 2:(10.,-45./7), 3:(-10.,10.),
           4:(0.,10.), 5:(-10.,0.), 6:(10.,10.)}


def example_homes(area):
    p=area['coverage']['projection']
    return {str(uid):dict(latitude=p['latitude']+north/p['north_m_per_degree'],
                          longitude=p['longitude']-west/p['west_m_per_degree'],
                          boot_id='example-boot-%d'%uid,source='first_valid_unarmed_gps_per_boot')
            for uid,(north,west) in OFFSETS.items()}


class XuchangBootHomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan=attach_routes(load_xuchang_small_plan())
        cls.area=copy.deepcopy(cls.plan['search_area'])
        cls.homes=example_homes(cls.area)
        cls.area['takeoff_gps_by_uav']=cls.homes
        cls.flyable=Polygon(cls.area['points_m'],holes=cls.area['excluded_polygons_m'])
        cls.routes={uid:rebase_gps_routes_for_takeoff(cls.area,item['task'],cls.homes[uid])
                    for uid,item in cls.plan['planned_uavs'].items()}

    def local(self,q):
        p=self.area['coverage']['projection']
        return [(q[1]-p['latitude'])*p['north_m_per_degree'],
                -(q[0]-p['longitude'])*p['west_m_per_degree']]

    def test_all_six_homes_all_75_returns_and_entry_avoid_every_other_landing(self):
        for uid,routes in self.routes.items():
            home=self.homes[uid]; gps=[home['longitude'],home['latitude']]
            self.assertEqual(routes['entry_path'][0],gps)
            self.assertEqual(routes['avoided_home_count'],5)
            self.assertEqual(routes['other_home_clearance_m'],2.5)
            self.assertEqual(routes['home_reference_boot_id'],home['boot_id'])
            self.assertEqual(len(routes['return_paths']),75)
            for route in [routes['entry_path']]+[r['path'] for r in routes['return_paths']]:
                line=LineString([self.local(p) for p in route])
                self.assertTrue(self.flyable.covers(line))
                self.assertGreaterEqual(line.distance(self.flyable.boundary),5.)
                for other,offset in OFFSETS.items():
                    if str(other)!=uid:
                        self.assertGreaterEqual(line.distance(Point(offset)),2.5,(uid,other))
            self.assertTrue(all(r['path'][-1]==gps for r in routes['return_paths']))
        # UAV2位于UAV1原起降点连接段上，必须新增绕行，而非只添加说明。
        r=self.routes['1']; first=self.local(r['entry_path'][1])
        self.assertGreater(len(r['entry_path']),len(self.plan['planned_uavs']['1']['task']['transit_routes']['entry_path']))
        self.assertLess(abs(first[0]-OFFSETS[2][0]),5.)

    def test_each_altitude_preserves_recipient_home_endpoints_and_protocol(self):
        for uid,item in self.plan['planned_uavs'].items():
            home=self.homes[uid]; task=dict(item['task'],transit_routes=self.routes[uid])
            for altitude in (.5,1,1.5,2,2.5,4.5,5,5.5,8,10,12,40,45,50,44,47,53,56,59):
                assignment=dict(task=task,uav_id=int(uid),mission_id='example',assignment_checksum='test',
                    coordinate_frame='WGS84',target_altitude_m=altitude,
                    landing_point_wgs84=[home['latitude'],home['longitude'],altitude])
                entry,returned=route_messages(assignment)
                expected=[home['longitude'],home['latitude'],altitude]
                self.assertEqual(entry['path'][0],expected)
                self.assertEqual(returned['landing_point'],expected)
                self.assertTrue(all(r['path'][-1]==expected for r in returned['routes']))
                self.assertTrue(all(p[2]==altitude for r in returned['routes'] for p in r['path']))

    def test_obstructed_artificial_guide_can_be_detoured_but_scan_endpoint_cannot(self):
        boundary=[[0,0],[100,0],[100,100],[0,100]]
        route=detour_guided_paths(boundary,[[[20,50],[50,50],[80,50]]],[[50,50]])[0]
        self.assertGreaterEqual(LineString(route).distance(Point(50,50)),2.5)
        with self.assertRaises(ValueError):
            detour_guided_paths(boundary,[[[20,50],[80,50]]],[[80,50]])

    def test_dispatch_checks_all_recipient_routes_before_sending_and_keeps_scan_points(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':d,'COMPETITION_DIAGNOSTICS_DIR':d})
            try:
                plan=copy.deepcopy(self.plan);plan['search_area']=copy.deepcopy(self.area)
                o=app.state.orchestrator
                o.plan('subject1',flight_profile='xuchang_small',flight_altitude_plan='around54m',
                       prepared_plan=plan,controller_mode='external')
                commands=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                self.assertEqual(6,len(commands))
                for c in commands:
                    checked=validate_assignment(dict(c['payload'],type='assign_task',uav_id=c['uav_id']),c['uav_id'])
                    self.assertEqual(checked['task']['waypoints_m'],self.plan['planned_uavs'][str(c['uav_id'])]['task']['waypoints_m'])
                    self.assertEqual(checked['task']['transit_routes']['avoided_home_count'],5)
                app.state.adapter.commands.clear()
                plan['search_area']['takeoff_gps_by_uav']['6']=dict(self.homes['1'])
                with self.assertRaises(ValueError):
                    o.plan('subject1',flight_profile='xuchang_small',prepared_plan=plan,controller_mode='external')
                self.assertFalse(app.state.adapter.commands)
            finally:app.state.audit.close()

    def test_live_api_uses_boot_home_not_current_fix_and_rejects_old_boot(self):
        with tempfile.TemporaryDirectory() as d,patch('competition_backend.api.time.time',return_value=1000.):
            app=create_app({'COMPETITION_ADAPTER':'distributed','COMPETITION_DATA_DIR':d,
                'COMPETITION_LOCAL_UAV_ID':'2','COMPETITION_GROUND_PEERS':''},audit=Mock())
            app.state.adapter.connected_uav_ids_snapshot=Mock(return_value=[1,2,4,5])
            app.state.adapter.assign_task=Mock(side_effect=AssertionError('只读预览不应发任务'))
            client=TestClient(app)
            payload=dict(subject='subject1',flight_profile='xuchang_small',coordinate_mode='gps',departure_point='fixed_xuchang')
            def update(uid,cap=None):
                h=self.homes[str(uid)]
                app.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,received_at=999.8,connected=True,
                    latitude=h['latitude']+.0001,longitude=h['longitude'],gps_status=3,location_source=4,
                    gps_telemetry_age_seconds=.1,capabilities=cap if cap is not None else
                    dict(boot_id=h['boot_id'],boot_gps_home=h)))
            for uid in (1,2,4,5):update(uid)
            response=client.post('/api/v1/planning/competition-coverage',json=payload)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['search_area']['takeoff_gps_by_uav']['2'],self.homes['2'])
            update(5,dict(boot_id='new-boot',boot_gps_home=self.homes['5']))
            response=client.post('/api/v1/planning/competition-coverage',json=payload)
            self.assertEqual(response.status_code,409);self.assertIn('UAV5',response.text)
            update(5,{})
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json=payload).status_code,409)
            update(5)
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json=payload).status_code,200)
            app.state.adapter.assign_task.assert_not_called()

if __name__=='__main__':unittest.main()
