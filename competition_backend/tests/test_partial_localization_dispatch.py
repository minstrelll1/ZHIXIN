"""真实 API 的参与名单、GPS检查和起飞范围；所有网络与飞机发送均隔离。"""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.final_scenes import load_plan, departure_for, altitude_profile
from competition_backend.models import Telemetry


class PartialLocalizationDispatchTest(unittest.TestCase):
    def setup_scene(self, profile, invalid=()):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixed = load_plan(profile)
        self.home = self.fixed['search_area']['departure_point_wgs84']
        self.app = create_app(dict(COMPETITION_ADAPTER='distributed',
            COMPETITION_DATA_DIR=directory.name, COMPETITION_IMAGE_ROOT=directory.name,
            COMPETITION_FIXED_TARGETS_CONFIG=str(Path(directory.name)/'fixed.json'),
            COMPETITION_LOCAL_UAV_ID='1', COMPETITION_CONFIRM_LIVE_CONFIG='1',
            COMPETITION_GROUND_PEERS=';'.join('%d=http://offline-%d:8000'%(i,i) for i in range(2,7))),
            audit=Mock())
        self.backend, self.adapter = self.app.state.orchestrator, self.app.state.adapter
        self.adapter.connected_uav_ids_snapshot = Mock(return_value=list(range(1,7)))
        self.adapter._request_json = Mock(return_value={'accepted':True})
        self.adapter.local_adapter.forward_command = Mock()
        self.adapter.command_assign_task = Mock(wraps=self.adapter.command_assign_task)
        self.adapter.command_takeoff = Mock(wraps=self.adapter.command_takeoff)
        self.adapter.acquire_coordination = Mock(wraps=self.adapter.acquire_coordination)
        self.adapter.release_coordination = Mock()  # 不启动后台网络线程。
        self.client = TestClient(self.app)  # 不启动 lifespan、TCP 服务或轮询线程。
        self.selection = dict(subject='subject2', planning_mode='competition', coordinate_mode='gps',
            controller_mode='external', flight_profile=profile, departure_point=departure_for(profile),
            flight_altitude_plan=altitude_profile(profile))
        self.states = {}
        for uid in range(1,7):
            self.update(uid, valid=uid not in invalid, home=uid not in invalid, gps=3 if uid not in invalid else 0)

    def update(self, uid, valid=True, home=True, gps=3):
        boot_home = dict(self.home, boot_id='boot-%d'%uid, source='first_valid_unarmed_gps_per_boot')
        state = Telemetry(uav_id=uid, received_at=time.time(), connected=True, odom_valid=valid,
            latitude=self.home['latitude'], longitude=self.home['longitude'], gps_status=gps, location_source=4,
            battery_percentage=.95, capabilities=dict(boot_id=boot_home['boot_id'],
                boot_gps_home=boot_home if home else None, auto_takeoff_supported=True, auto_takeoff_ready=True))
        self.states[uid] = state
        self.backend.update_telemetry(state)

    def plan(self):
        response = self.client.post('/api/v1/plan', json=self.selection)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def acknowledge(self, mission):
        for uid, task in mission['uavs'].items():
            state = replace(self.states[int(uid)], task_assignment_acked=True,
                task_assignment_mission_id=mission['mission_id'], task_assignment_checksum=task['assignment_checksum'])
            self.states[int(uid)] = state
            self.backend.update_telemetry(state)

    def test_finals_only_assign_and_takeoff_valid_members_recovery_does_not_join(self):
        for profile in ('final_scene_1','final_scene_2','xuchang_small_2'):
            with self.subTest(profile=profile):
                self.setup_scene(profile, invalid=(2,))
                expected=[1,3,4,5,6]
                preview=self.client.post('/api/v1/planning/competition-coverage',json=self.selection)
                self.assertEqual(preview.status_code,200,preview.text)
                self.assertNotIn('2',preview.json()['search_area']['takeoff_gps_by_uav'])
                result=self.plan(); mission=result['mission']
                self.assertEqual(result['dispatch_status']['assigned_uav_ids'],expected)
                self.assertEqual(result['dispatch_status']['excluded_uavs'],{'2':'定位状态无效'})
                self.assertEqual(set(mission['planned_uavs']),set(map(str,range(1,7))))
                self.assertEqual(set(mission['uavs']),set(map(str,expected)))
                self.assertEqual([c.args[0] for c in self.adapter.command_assign_task.call_args_list],expected)
                self.adapter.acquire_coordination.assert_called_once_with(expected)
                self.assertFalse(any('offline-2:' in c.args[0] for c in self.adapter._request_json.call_args_list))
                for uid in expected:
                    for key in ('polygon_m','waypoints_m','waypoints_wgs84','layers'):
                        self.assertEqual(mission['uavs'][str(uid)]['task'].get(key),
                                         self.fixed['planned_uavs'][str(uid)]['task'].get(key))
                self.acknowledge(mission)
                self.update(2)  # 后恢复且没有当前任务回执，仍不参与本次起飞。
                status=self.client.get('/api/v1/status').json()
                self.assertEqual(status['dispatch_status']['excluded_uavs'],{'2':'定位状态无效'})
                self.assertEqual(status['dispatch_status']['acknowledged_uav_ids'],expected)
                prepared=self.client.post('/api/v1/takeoff/prepare').json()
                self.assertTrue(prepared['ready'],prepared)
                self.assertEqual(set(prepared['preflight']['failures']),set(map(str,expected)))
                response=self.client.post('/api/v1/takeoff/confirm',json={'token':prepared['confirmation_token']})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual([c.args[0] for c in self.adapter.command_takeoff.call_args_list],expected)

    def test_all_invalid_is_preview_only_without_leases_or_takeoff(self):
        self.setup_scene('final_scene_2',invalid=range(1,7))
        result=self.plan()
        self.assertEqual(result['mission']['uavs'],{})
        self.assertEqual(len(result['mission']['planned_uavs']),6)
        self.assertEqual(result['dispatch_status']['mode'],'planning_only')
        self.assertEqual(len(result['dispatch_status']['excluded_uavs']),6)
        self.adapter.acquire_coordination.assert_not_called()
        self.adapter.command_assign_task.assert_not_called()
        self.assertFalse(self.client.post('/api/v1/takeoff/prepare').json()['ready'])
        self.adapter.command_takeoff.assert_not_called()

    def test_replanning_can_add_recovered_aircraft_and_exclude_publisher_aircraft(self):
        self.setup_scene('final_scene_2',invalid=(1,2))
        first=self.plan()
        self.assertEqual(first['dispatch_status']['assigned_uav_ids'],[3,4,5,6])
        self.update(2)
        second=self.plan()
        self.assertNotEqual(first['mission']['mission_id'],second['mission']['mission_id'])
        self.assertEqual(second['dispatch_status']['assigned_uav_ids'],[2,3,4,5,6])
        self.assertEqual(second['dispatch_status']['excluded_uavs'],{'1':'定位状态无效'})
        self.acknowledge(second['mission'])
        prepared=self.client.post('/api/v1/takeoff/prepare').json()
        self.assertTrue(prepared['ready'],prepared)
        response=self.client.post('/api/v1/takeoff/confirm',json={'token':prepared['confirmation_token']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([c.args[0] for c in self.adapter.command_takeoff.call_args_list],[2,3,4,5,6])
        self.adapter.local_adapter.forward_command.assert_not_called()

    def test_participating_aircraft_losing_localization_still_blocks_preflight(self):
        self.setup_scene('final_scene_1',invalid=(2,))
        self.acknowledge(self.plan()['mission'])
        self.backend.update_telemetry(replace(self.states[3],odom_valid=False))
        prepared=self.client.post('/api/v1/takeoff/prepare').json()
        self.assertFalse(prepared['ready'])
        self.assertIn('odometry is invalid',prepared['preflight']['failures']['3'])
        self.assertNotIn('2',prepared['preflight']['failures'])
        self.adapter.command_takeoff.assert_not_called()

    def test_valid_localization_still_requires_gps_and_current_boot_home(self):
        for home,gps in ((False,3),(True,2)):
            with self.subTest(home=home,gps=gps):
                self.setup_scene('final_scene_2')
                self.update(2,home=home,gps=gps)
                response=self.client.post('/api/v1/plan',json=self.selection)
                self.assertEqual(response.status_code,409,response.text)
                self.assertIn('UAV2',response.json()['detail'])
                self.adapter.command_assign_task.assert_not_called()

    def test_localization_lost_while_loading_is_removed_before_dispatch(self):
        self.setup_scene('final_scene_2')
        def load_and_lose(profile):
            plan=load_plan(profile)
            self.update(2,valid=False)
            return plan
        with patch('competition_backend.final_scenes.load_plan',side_effect=load_and_lose):
            result=self.plan()
        self.assertEqual(result['dispatch_status']['assigned_uav_ids'],[1,3,4,5,6])
        self.assertNotIn('2',result['mission']['search_area']['takeoff_gps_by_uav'])


if __name__=='__main__':unittest.main()
