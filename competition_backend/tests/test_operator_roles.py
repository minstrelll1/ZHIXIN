"""地面角色、跨机控制以及配置同步边界测试，不连接真实无人机。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_shared.fleet import apply_fixed_binding, default_fleet, revision


class FixedBindingTest(unittest.TestCase):
    def test_all_p600_defaults_and_manual_overrides(self):
        config = default_fleet()
        for i in range(1, 7):
            config = apply_fixed_binding(config, i, 'p600')
            v = config['vehicles'][i - 1]
            self.assertEqual(v['onboard_host'], '192.168.1.%d' % (197 + 5 * i))
            self.assertEqual(v['video_rtsp_source'], 'rtsp://%s:8554/live' % v['onboard_host'])
        config['vehicles'][0].update(onboard_host='10.1.1.2', peer_host='10.2.2.2',
                                     video_rtsp_source='rtsp://10.1.1.2:8554/custom', device_id='bound')
        self.assertEqual(apply_fixed_binding(config, 1, 'p600'), config)
        changed = apply_fixed_binding(config, 1, 'su17')['vehicles'][0]
        self.assertEqual(changed['device_id'], '')
        self.assertEqual(changed['video_rtsp_source'], '')
        self.assertEqual(changed['onboard_host'], '192.168.1.88')


class OperatorRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = default_fleet()
        for i in range(1, 7):
            self.config = apply_fixed_binding(self.config, i, 'p600')
        self.apps = {}
        self.clients = {}
        for terminal in (1, 2):
            root = Path(self.tmp.name) / str(terminal)
            root.mkdir()
            path = root / 'fleet.json'
            path.write_text(json.dumps(self.config), encoding='utf-8')
            env = {'COMPETITION_FLEET_CONFIG': str(path), 'COMPETITION_ADAPTER': 'distributed',
                   'COMPETITION_GROUND_TERMINAL_ID': str(terminal), 'COMPETITION_DATA_DIR': str(root),
                   'COMPETITION_PEER_TOKEN': 'unit-test-peer', 'COMPETITION_TASK_PUBLISHER': '',
                   'COMPETITION_POINTCLOUD_ROOT': str(root / 'cloud')}
            with patch.dict(os.environ, env):
                app = create_app()
            self.apps[terminal] = app
            self.clients[terminal] = TestClient(app)
            app.state.adapter.local_adapter.forward_command = Mock()
            app.state.adapter._request_json = self.network
        self.headers = {'X-Competition-Peer-Token': 'unit-test-peer'}

    def network(self, url, method='GET', payload=None):
        for terminal in self.apps:
            base = 'http://192.168.2.%d:8000' % (197 + 5 * terminal)
            if url.startswith(base + '/'):
                response = self.clients[terminal].request(method, url[len(base):], json=payload, headers=self.headers)
                if response.status_code >= 400:
                    raise RuntimeError(response.json().get('detail', response.text))
                return response.json()
        raise RuntimeError('测试中的地面终端离线')

    def select(self, terminal, publisher=False, model='p600'):
        return self.clients[terminal].post('/api/v1/operator', json={
            'ground_terminal_id': terminal, 'model': model, 'task_publisher': publisher})

    def test_selection_required_and_follower_permission_enforced(self):
        c = self.clients[1]
        self.assertEqual(c.post('/api/v1/plan', json={'subject': 'subject1'}).status_code, 409)
        self.assertEqual(self.select(1).status_code, 200)
        data = c.get('/api/v1/fleet/config').json()
        self.assertFalse(data['operator']['can_edit_fleet'])
        self.assertEqual(c.put('/api/v1/fleet/config', json=data, headers=self.headers).status_code, 403)
        self.assertEqual(c.post('/api/v1/uavs/2/restart-executor').status_code, 403)
        app = self.apps[1]
        app.state.adapter.connected_uav_ids_snapshot = Mock(return_value=[1, 2])
        response = c.post('/api/v1/plan', json={'subject': 'subject1'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['dispatch_status']['assigned_uav_ids'], [1])
        self.assertEqual(response.json()['dispatch_status']['mode'], 'local_only')
        self.assertTrue(all(call.args[0] == 1 for call in app.state.adapter.local_adapter.forward_command.call_args_list))

    def test_publisher_sync_conflict_stale_revision_and_restart_guard(self):
        self.select(2)
        selected = self.select(1, publisher=True)
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertTrue(selected.json()['synchronization']['peers']['2']['ok'])
        self.assertFalse(selected.json()['synchronization']['peers']['3']['ok'])
        self.assertEqual(self.select(2, publisher=True).status_code, 409)
        c = self.clients[1]
        data = c.get('/api/v1/fleet/config').json()
        data['config']['vehicles'][0]['video_rtsp_source'] = 'rtsp://192.168.1.202:8554/custom'
        response = c.put('/api/v1/fleet/config', json=data, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['synchronization']['peers']['2']['ok'])
        follower = self.clients[2].get('/api/v1/fleet/config').json()
        self.assertEqual(follower['revision'], response.json()['revision'])
        self.assertTrue(follower['restart_required'])
        old_sync = self.clients[2].post('/api/v1/peer/fleet-config', json={'publisher_id': 'ground-1', 'config': self.config}, headers=self.headers)
        self.assertEqual(old_sync.status_code, 409)
        self.assertEqual(self.clients[2].get('/api/v1/fleet/config').json()['revision'], follower['revision'])
        self.assertEqual(c.post('/api/v1/plan', json={'subject': 'subject1'}).status_code, 409)
        self.assertEqual(c.put('/api/v1/fleet/config', json=data, headers=self.headers).status_code, 422)
        self.assertEqual(self.select(1, publisher=True).status_code, 200)
        self.assertEqual(c.get('/api/v1/fleet/config').json()['config']['vehicles'][0]['video_rtsp_source'], 'rtsp://192.168.1.202:8554/custom')

    def test_peer_cannot_bypass_role_with_shared_token(self):
        # 地面1先保持未选发布角色，之后首次选择发布端；运行中不允许切换角色。
        self.select(2)
        c = self.clients[2]
        payload = {'coordinator_id': 'ground-1', 'uav_id': 2, 'command_type': 'takeoff', 'payload': {}}
        self.assertEqual(c.post('/api/v1/peer/command', json=payload, headers=self.headers).status_code, 403)
        self.assertEqual(c.post('/api/v1/peer/fleet-config', json={'publisher_id': 'ground-1', 'config': self.config}, headers=self.headers).status_code, 403)
        self.select(1, publisher=True)
        self.assertEqual(c.post('/api/v1/peer/command', json=payload, headers=self.headers).status_code, 409)
        self.assertEqual(c.post('/api/v1/peer/lease', json={'coordinator_id': 'ground-1'}, headers=self.headers).status_code, 200)
        self.assertEqual(c.post('/api/v1/peer/command', json=payload, headers=self.headers).status_code, 200)
        self.apps[2].state.adapter._coordinator_seen_at = time.time() - 31
        self.assertEqual(c.post('/api/v1/peer/command', json=payload, headers=self.headers).status_code, 409)

    def test_model_is_fixed_by_terminal_and_changes_use_fleet_editor(self):
        self.select(1, publisher=True)
        result = self.select(2, model='su17')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.apps[2].state.fleet_store.read()['vehicles'][1]['model'], 'p600')
        draft = self.clients[1].get('/api/v1/fleet/selection-preview').json()
        self.assertEqual(draft['config']['vehicles'][1]['model'], 'p600')

    def test_refresh_while_armed_is_idempotent_but_role_change_blocked(self):
        self.select(1)
        self.apps[1].state.orchestrator.update_telemetry(Telemetry(uav_id=1, received_at=time.time(), armed=True))
        self.assertEqual(self.select(1).status_code, 200)
        self.assertEqual(self.select(1, publisher=True).status_code, 409)

    def test_follower_returns_only_paired_uav_from_mirrored_mission(self):
        self.select(2)
        self.apps[2].state.adapter.accept_peer_snapshot('ground-1', {'mission': {
            'mission_id': 'remote-mission', 'phase': 'running', 'uavs': {'1': {}, '2': {}}}})
        response = self.clients[2].post('/api/v1/return', json={'reason': 'manual'})
        self.assertEqual(response.status_code, 200, response.text)
        command=self.apps[2].state.adapter.local_adapter.forward_command.call_args.args
        self.assertEqual(command[:2],(2,'return_home'))
        self.assertEqual(command[2]['mission_id'],'remote-mission')
        self.assertEqual(command[2]['reason'],'manual')
        self.assertTrue(command[2]['land_after_return'])
        request=response.json()['return_requests']['2']
        self.assertEqual(request['request_id'],command[2]['request_id'])
        self.assertEqual(request['state'],'waiting')
        self.apps[2].state.orchestrator.update_telemetry(Telemetry(uav_id=2, received_at=time.time(), return_ack={
            'uav_id':2,'request_id':request['request_id'],'mission_id':'remote-mission','assignment_checksum':'',
            'state':'forwarded','ack_seq':2,'detail':'机载已向程序B发布'}))
        self.assertEqual(self.clients[2].get('/api/v1/status').json()['return_requests']['2']['state'],'forwarded')

    def test_follower_cannot_return_other_uav_or_stale_mission(self):
        self.select(2)
        self.apps[2].state.adapter.accept_peer_snapshot('ground-1', {'mission': {
            'mission_id': 'current', 'phase': 'running', 'uavs': {'1': {}, '2': {}}}})
        client = self.clients[2]
        response = client.post('/api/v1/return', json={'uav_ids': [1, 2]})
        self.assertEqual(response.status_code, 403)
        response = client.post('/api/v1/return', json={'uav_ids': [2], 'mission_id': 'old'})
        self.assertEqual(response.status_code, 409)
        self.apps[2].state.adapter.local_adapter.forward_command.assert_not_called()

    def test_invalid_return_selection_is_rejected(self):
        self.select(1, publisher=True)
        for values in ([], [1,1], [7], [True], '1'):
            with self.subTest(values=values):
                self.assertEqual(self.clients[1].post('/api/v1/return', json={'uav_ids':values}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
