import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from competition_backend.api import create_app


class CoverageApiTest(unittest.TestCase):
    def test_preview_preserves_mission_and_never_sends_commands(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ,{
                'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':data},clear=False):
            app=create_app()
            client=TestClient(app)
            response=client.post('/api/v1/plan',json={'subject':'subject1'})
            self.assertEqual(response.status_code,200)
            mission=response.json()['mission']
            commands=app.state.adapter.snapshot()
            for subject in ('subject1','subject2'):
                result=client.post('/api/v1/planning/competition-coverage',json={'subject':subject})
                self.assertEqual(result.status_code,200)
                self.assertTrue(result.json()['preview_only'])
                self.assertEqual(result.json()['subject'],subject)
                self.assertEqual(len(result.json()['planned_uavs']),6)
                self.assertEqual(result.json()['search_area']['coverage']['hover_scan_seconds'],10)
                self.assertEqual(result.json()['search_area']['coverage']['objective'],'minimize_total_mission_time')
            self.assertEqual(app.state.adapter.snapshot(),commands)
            self.assertEqual(client.get('/api/v1/status').json()['mission'],mission)
            for payload in ({'subject':'subject3'},{'speed_mps':0},{'hover_scan_seconds':0},{'max_region_aspect_ratio':0},{'terrain_exclusions_enabled':'false'}):
                self.assertEqual(client.post('/api/v1/planning/competition-coverage',json=payload).status_code,422)


    def test_mismatched_plan_returns_conflict_instead_of_computing(self):
        from competition_backend import polygon_coverage as pc
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ,{
                'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':data},clear=False), patch.object(pc,'_compute',side_effect=AssertionError('不得临场求解')):
            client=TestClient(create_app())
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json={}).status_code,200)
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json={'forest_edge_m':75,'rebuild':True}).status_code,409)
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json={'uav_count':5}).status_code,409)
            self.assertEqual(client.post('/api/v1/planning/competition-coverage',json={'uav_count':True}).status_code,422)

    def test_saved_gps_plan_keeps_selected_height_without_adding_sea_level_altitude(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {
                'COMPETITION_ADAPTER': 'sim', 'COMPETITION_DATA_DIR': data}, clear=False):
            client = TestClient(create_app())
            for scene in ('lab', 'outdoor5', 'lab10'):
                with self.subTest(scene=scene):
                    response = client.post('/api/v1/plan', json={
                        'subject': 'subject1', 'planning_mode': 'competition',
                        'coordinate_mode': 'gps', 'flight_profile': scene,
                        'flight_altitude_plan': 'around1m', 'controller_mode': 'external',
                        'gps_origin': {'latitude': 30.78528, 'longitude': 103.86102,
                                       'altitude_m': 44.098},
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    mission = response.json()['mission']
                    self.assertEqual(mission['flight_altitude_plan'], 'around1m')
                    expected = [0.5, 1.5, 0.5, 1.0, 1.5, 1.0]
                    self.assertEqual([mission['uavs'][str(uid)]['target_altitude_m']
                                      for uid in range(1, 7)], expected)
                    task = mission['uavs']['1']['task']
                    self.assertTrue(task['waypoints_wgs84'])
                    self.assertTrue(all(len(point) == 2 for point in task['waypoints_wgs84']))
                    first_lat, first_lon = task['waypoints_wgs84'][0]
                    self.assertGreaterEqual(first_lat, 30.78528)
                    self.assertLessEqual(first_lon, 103.86102)

    def test_competition_shape_can_be_scaled_for_lab_xyz(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ,{
                'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':data},clear=False):
            client = TestClient(create_app())
            response = client.post('/api/v1/plan', json={
                'subject': 'subject1',
                'planning_mode': 'competition',
                'coordinate_mode': 'xyz',
                'flight_profile': 'lab',
                'controller_mode': 'external',
            })
            self.assertEqual(response.status_code, 200, response.text)
            mission = response.json()['mission']
            area = mission['search_area']
            self.assertEqual(area['coordinate_mode'], 'xyz')
            self.assertEqual(area['coordinate_frame'], 'ENU')
            self.assertLessEqual(area['width_m'], 3.0)
            self.assertLessEqual(area['height_m'], 3.0)
            self.assertEqual(mission['flight_profile'], 'lab')
            self.assertEqual(mission['uavs']['1']['task']['coordinate_frame'], 'ENU')
            self.assertFalse(mission['uavs']['1']['task'].get('waypoints_wgs84'))

    def test_competition_shape_can_be_scaled_to_ten_metres_and_published_as_wgs84(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ,{
                'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':data},clear=False):
            client = TestClient(create_app())
            response = client.post('/api/v1/plan', json={
                'subject': 'subject1',
                'planning_mode': 'competition',
                'coordinate_mode': 'gps',
                'flight_profile': 'lab10',
                'controller_mode': 'external',
                'gps_origin': {'latitude': 30.7852800, 'longitude': 103.8610200, 'altitude_m': 100.0},
            })
            self.assertEqual(response.status_code, 200, response.text)
            mission = response.json()['mission']
            area = mission['search_area']
            self.assertEqual(area['coordinate_mode'], 'gps')
            self.assertEqual(area['coordinate_frame'], 'WGS84')
            self.assertLessEqual(area['width_m'], 10.0)
            self.assertLessEqual(area['height_m'], 10.0)
            self.assertEqual(mission['flight_profile'], 'lab10')
            self.assertEqual(area['coverage']['reconnaissance_radius_m'], 1.0)
            self.assertEqual(area['coverage']['speed_mps'], 0.5)
            task = mission['uavs']['1']['task']
            self.assertEqual(task['coordinate_frame'], 'LOCAL_NORTH_WEST')
            self.assertTrue(task['waypoints_wgs84'])
            lat0 = 30.7852800
            lon0 = 103.8610200
            self.assertAlmostEqual(task['waypoints_wgs84'][0][0], lat0, delta=0.001)
            self.assertAlmostEqual(task['waypoints_wgs84'][0][1], lon0, delta=0.001)
            self.assertTrue(all(len(point) == 2 for point in task['waypoints_wgs84']))

    def test_three_metre_lab_gps_uses_the_onboard_reference_when_available(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ,{
                'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':data},clear=False):
            client = TestClient(create_app())
            telemetry = client.post('/api/v1/sim/uavs/1/telemetry', json={
                'connected': True,
                'latitude': 30.7852800,
                'longitude': 103.8610200,
                'altitude': 100.5,
            })
            self.assertEqual(telemetry.status_code, 200, telemetry.text)
            response = client.post('/api/v1/plan', json={
                'subject': 'subject1',
                'planning_mode': 'competition',
                'coordinate_mode': 'gps',
                'flight_profile': 'lab',
                'controller_mode': 'external',
            })
            self.assertEqual(response.status_code, 200, response.text)
            mission = response.json()['mission']
            area = mission['search_area']
            self.assertEqual(area['coordinate_mode'], 'gps')
            self.assertEqual(area['coordinate_frame'], 'WGS84')
            self.assertLessEqual(area['width_m'], 3.0)
            self.assertLessEqual(area['height_m'], 3.0)
            self.assertEqual(area['coverage']['speed_mps'], 0.2)
            self.assertEqual(area['coverage']['reconnaissance_radius_m'], 1.0)
            self.assertEqual(area['gps_origin']['latitude'], 30.7852800)
            self.assertEqual(area['gps_origin']['longitude'], 103.8610200)
            self.assertTrue(mission['uavs']['1']['task']['waypoints_wgs84'])

if __name__=='__main__':
    unittest.main()

