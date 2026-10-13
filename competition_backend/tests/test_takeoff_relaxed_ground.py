"""地面起飞可用性检查与排队规划；只用模拟适配器，不连接飞机。"""
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_backend.plan_jobs import PlanJobRegistry, plan_checkpoint


class RelaxedTakeoffApiTest(unittest.TestCase):
    def make_app(self, directory):
        app = create_app({'COMPETITION_ADAPTER': 'sim', 'COMPETITION_DATA_DIR': directory})
        client = TestClient(app)
        result = client.post('/api/v1/plan', json={'subject': 'subject1', 'controller_mode': 'external'})
        self.assertEqual(result.status_code, 200, result.text)
        backend = app.state.orchestrator
        mission = backend._mission
        for uid in backend.active_uav_ids:
            backend.update_telemetry(Telemetry(
                uav_id=uid, received_at=time.time()-600, connected=True, odom_valid=True,
                battery_percentage=.2, armed=False, control_state=0,
                capabilities={'auto_takeoff_supported': True, 'auto_takeoff_ready': True},
                task_assignment_acked=True, task_assignment_mission_id=mission.mission_id,
                task_assignment_checksum=mission.uavs[uid].assignment_checksum))
        return app, client

    def test_program_b_not_running_missing_or_stale_cannot_block_prepare_or_confirm(self):
        for status in (None, {'state':'error'}, {'state':'starting'},
                       {'state':'running','received_at':time.time()-600}):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                app, client = self.make_app(tmp)
                app.state.program_manager = Mock()
                app.state.program_manager.snapshot.return_value = {'programs':{'flight':status}}
                prepared = client.post('/api/v1/takeoff/prepare')
                self.assertEqual(prepared.status_code, 200, prepared.text)
                data = prepared.json()
                self.assertTrue(data['ready'], data)
                self.assertFalse(data['preflight']['battery_threshold_enforced'])
                self.assertFalse(data['preflight']['telemetry_age_enforced'])
                confirmed = client.post('/api/v1/takeoff/confirm', json={'token':data['confirmation_token']})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()['mission']['phase'], 'taking_off')
                app.state.program_manager.snapshot.assert_not_called()

    def test_queued_plan_is_cancelled_before_takeoff_and_never_runs_after_it(self):
        registry = PlanJobRegistry()
        with tempfile.TemporaryDirectory() as tmp, patch('competition_backend.api.PlanJobRegistry', return_value=registry):
            app, client = self.make_app(tmp)
            mission_id = app.state.orchestrator._mission.mission_id
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            calls = []
            def handle(payload):
                calls.append(payload['request_id'])
                if payload['request_id'] == 'old':
                    started.set()
                    try:
                        if not release.wait(15): raise RuntimeError('test release timeout')
                        plan_checkpoint('后续分派')
                    finally:
                        finished.set()
                return {'mission':{'mission_id':payload['request_id']}}
            registry.submit({'request_id':'old'}, handle)
            self.assertTrue(started.wait(2))
            try:
                queued = registry.submit({'request_id':'queued'}, handle)
                self.assertEqual(queued['state'], 'queued')
                result = client.post('/api/v1/takeoff/prepare')
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()['ready'], result.text)
                self.assertTrue(registry.get(queued['job_id'])['cancelled_for_takeoff'])
                result = client.post('/api/v1/takeoff/confirm', json={'token':result.json()['confirmation_token']})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['mission']['mission_id'], mission_id)
                self.assertEqual(result.json()['mission']['phase'], 'taking_off')
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            deadline = time.monotonic()+2
            while registry._running is not None and time.monotonic()<deadline: time.sleep(.01)
            self.assertIsNone(registry._running)
            self.assertIsNone(registry._queued)
            self.assertEqual(calls, ['old'])
            self.assertEqual(registry.get(queued['job_id'])['state'], 'superseded')
            # 空中仍不能再分派覆盖任务。
            self.assertEqual(client.post('/api/v1/plan', json={'subject':'subject1'}).status_code, 409)

    def test_peer_takeoff_is_forwarded_without_program_b_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app({'COMPETITION_ADAPTER':'distributed', 'COMPETITION_DATA_DIR':tmp,
                              'COMPETITION_LOCAL_UAV_ID':'4', 'COMPETITION_GROUND_PEERS':'',
                              'COMPETITION_PEER_TOKEN':'test-peer'})
            app.state.program_manager = Mock()
            app.state.program_manager.snapshot.return_value = {'programs':{'flight':{'state':'error'}}}
            app.state.adapter.local_adapter.forward_command = Mock()
            app.state.adapter.mirrored_snapshot = Mock(return_value={'mission':{'controller_mode':'external'}})
            client=TestClient(app)
            payload={'mission_id':'confirmed-mission', 'target_altitude_m':50}
            body={'coordinator_id':'publisher', 'uav_id':4, 'command_type':'takeoff', 'payload':payload}
            self.assertEqual(client.post('/api/v1/peer/command', json=body).status_code, 403)
            result=client.post('/api/v1/peer/command', json=body, headers={'X-Competition-Peer-Token':'test-peer'})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['accepted'])
            app.state.adapter.local_adapter.forward_command.assert_called_once_with(4,'takeoff',payload)
            app.state.program_manager.snapshot.assert_not_called()

    def test_running_dispatch_and_unacknowledged_task_still_block(self):
        registry = PlanJobRegistry()
        with tempfile.TemporaryDirectory() as tmp, patch('competition_backend.api.PlanJobRegistry', return_value=registry):
            app, client = self.make_app(tmp)
            with registry._lock:
                registry._current = {'state':'running', 'job_id':'in-progress'}
            for endpoint, payload in (('prepare',{}), ('confirm',{'token':'unused'})):
                result=client.post('/api/v1/takeoff/'+endpoint, json=payload)
                self.assertEqual(result.status_code, 409)
                self.assertIn('正在执行', result.text)
            with registry._lock: registry._current = None
            app.state.orchestrator._telemetry[1].task_assignment_acked=False
            result=client.post('/api/v1/takeoff/prepare')
            self.assertFalse(result.json()['ready'])
            self.assertIn('task assignment is not acknowledged', result.json()['preflight']['failures']['1'])
            self.assertFalse(any(c['type']=='takeoff' for c in app.state.adapter.commands))


if __name__ == '__main__':
    unittest.main()
