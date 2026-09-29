import os
import json
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_backend import polygon_coverage
from competition_shared.fleet import default_fleet, apply_fixed_binding

_protocol_path = Path(__file__).resolve().parents[2] / 'src/su17_competition_executor/src/su17_competition_executor/task_protocol.py'
_spec = importlib.util.spec_from_file_location('dispatch_onboard_protocol', _protocol_path)
_protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_protocol)


class CompetitionDispatchTest(unittest.TestCase):
    def test_saved_plan_traverses_six_ground_nodes_without_recomputing_or_takeoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = default_fleet()
            for i in range(1, 7):
                config = apply_fixed_binding(config, i, 'p600')
            clients, apps = {}, {}
            headers = {'X-Competition-Peer-Token': 'test-peer'}
            def network(url, method='GET', payload=None):
                for i, client in clients.items():
                    base = 'http://192.168.2.%d:8000' % (197 + 5 * i)
                    if url.startswith(base + '/'):
                        r = client.request(method, url[len(base):], json=payload, headers=headers)
                        if r.status_code >= 400:
                            raise RuntimeError(r.text)
                        return r.json()
                raise AssertionError('不能连接真实网络')
            for uid in range(1, 7):
                root = Path(tmp) / str(uid)
                root.mkdir()
                fleet_path = root / 'fleet.json'
                fleet_path.write_text(json.dumps(config), encoding='utf-8')
                env = dict(os.environ, COMPETITION_ADAPTER='distributed', COMPETITION_FLEET_CONFIG=str(fleet_path),
                           COMPETITION_DATA_DIR=str(root), COMPETITION_GROUND_TERMINAL_ID=str(uid),
                           COMPETITION_TASK_PUBLISHER='', COMPETITION_PEER_TOKEN='test-peer',
                           COMPETITION_ASYNC_OPERATOR_SELECTION='0')
                app = create_app(env)
                app.state.adapter._request_json = network
                app.state.adapter.local_adapter.forward_command = Mock()
                apps[uid], clients[uid] = app, TestClient(app)
            for uid in [2, 3, 4, 5, 6, 1]:
                r = clients[uid].post('/api/v1/operator', json={'ground_terminal_id': uid, 'model': 'p600', 'task_publisher': uid == 1})
                self.assertEqual(r.status_code, 200, r.text)
            apps[1].state.adapter.connected_uav_ids_snapshot = Mock(return_value=list(range(1, 7)))
            # 真机 GPS 分派需先收到任务发布端的有效经纬度。
            apps[1].state.orchestrator.update_telemetry(Telemetry(
                uav_id=1, received_at=apps[1].state.orchestrator.clock(), connected=True,
                latitude=30.78528, longitude=103.86102, altitude=44.098, gps_status=3, location_source=4,
            ))
            with patch.object(polygon_coverage, '_compute', side_effect=AssertionError('不得临场重算')):
                response = clients[1].post('/api/v1/plan', json={'subject': 'subject1', 'planning_mode': 'competition', 'recognition_selection': {'category_count': 3, 'category_ids': [1, 8, 12]}})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result['dispatch_status']['assigned_uav_ids'], list(range(1, 7)))
            self.assertIsNotNone(result['mission']['prepared_plan'])
            self.assertEqual(result['mission']['search_area']['landing_mode'], 'onboard_home')
            for uid, app in apps.items():
                calls = app.state.adapter.local_adapter.forward_command.call_args_list
                self.assertEqual(len(calls), 1)
                actual_uid, kind, payload = calls[0].args
                self.assertEqual((actual_uid, kind), (uid, 'assign_task'))
                self.assertEqual(payload['task']['recognition_selection']['category_ids'], [1, 8, 12])
                self.assertEqual(payload['task']['sector'], uid)
                self.assertEqual(payload['task']['hover_scan_seconds'], 10)
                self.assertEqual(payload['task']['speed_mps'], 5)
                self.assertTrue(payload['task']['waypoints_wgs84'])
                self.assertIsNone(payload['landing_point_m'])
                self.assertGreaterEqual(payload['target_altitude_m'], 40)
                self.assertEqual(payload['mission_id'], result['mission']['mission_id'])
                # 同时通过现有机载协议校验，避免网页端成功而机载拒收。
                validated = _protocol.validate_assignment({'type': 'assign_task', 'uav_id': uid, **payload}, uid)
                self.assertEqual(validated['task']['waypoints_wgs84'], payload['task']['waypoints_wgs84'])

    def test_saved_plan_external_mode_uses_wgs84_without_inventing_a_global_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(dict(os.environ, COMPETITION_ADAPTER='sim', COMPETITION_DATA_DIR=tmp))
            r = TestClient(app).post('/api/v1/plan', json={'subject': 'subject2', 'planning_mode': 'competition', 'controller_mode': 'external'})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['mission']['flight_profile'], 'competition')
            self.assertEqual(r.json()['mission']['uavs']['1']['task']['coordinate_frame'], 'LOCAL_NORTH_WEST')

    def test_saved_lab_plan_external_mode_stays_enu_even_with_gps_form_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(dict(os.environ, COMPETITION_ADAPTER='sim', COMPETITION_DATA_DIR=tmp))
            r = TestClient(app).post('/api/v1/plan', json={
                'subject': 'subject2',
                'planning_mode': 'competition',
                'controller_mode': 'external',
                'coordinate_mode': 'xyz',
                'flight_profile': 'lab',
                'gps_origin': {'latitude': 30.78528, 'longitude': 103.86102, 'altitude_m': 100.0},
            })
            self.assertEqual(r.status_code, 200, r.text)
            assignment = r.json()['mission']['uavs']['1']
            self.assertEqual(assignment['task']['coordinate_frame'], 'ENU')


if __name__ == '__main__':
    unittest.main()
