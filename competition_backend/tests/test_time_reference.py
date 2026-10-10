"""只读事件时间参考：内存网络及假时钟，不连接飞机或调整系统时间。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from competition_backend.time_reference import TimeReferenceCollector
from competition_backend.models import Telemetry
from test_competition_clock import SixGroundClockTest

BOOT1 = '11111111-1111-4111-8111-111111111111'
BOOT2 = '22222222-2222-4222-8222-222222222222'


class TimeReferenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100.
        self.epoch = 1791532800.
        self.offset = 0.
        self.boot = BOOT1
        self.enabled = True
        self.delays = iter((.8, .2, .5))
        self.updated = Mock()
        self.collector = self.make_collector()

    def make_collector(self):
        return TimeReferenceCollector(Path(self.tmp.name) / 'time_references.json', 1,
            lambda: self.enabled, lambda: [1], self.probe, on_update=self.updated,
            monotonic=lambda: self.now, wall_clock=lambda: self.epoch + self.now + self.offset)

    def probe(self, uid):
        delay = next(self.delays)
        self.now += delay
        return dict(boot_id=self.boot, remote_monotonic=self.now - delay/2 + 5000,
                    remote_wall=1970000.)

    def test_smallest_rtt_uses_publisher_wall_not_remote_wall(self):
        self.collector.collect_once()
        content = json.loads(self.collector.path.read_text(encoding='utf-8'))
        reference = content['references'][BOOT1]
        self.assertAlmostEqual(reference['uncertainty_sec'], .1)
        self.assertAlmostEqual(reference['ground_epoch'] - reference['remote_monotonic'], self.epoch - 5000, places=5)
        self.assertEqual(reference['source'], 'publisher_windows')
        self.assertEqual(self.updated.call_count, 1)
        self.assertEqual(self.collector.snapshot()['by_uav']['1']['state'], 'ready')

    def test_history_survives_collector_restart_and_new_boot(self):
        self.collector.collect_once()
        self.boot = BOOT2
        self.delays = iter((.1, .2, .3))
        restarted = self.make_collector()
        restarted.collect_once()
        self.assertEqual(set(json.loads(restarted.path.read_text())['references']), {BOOT1, BOOT2})

    def test_clock_jump_discards_whole_batch_and_does_not_write(self):
        probe = self.collector.probe
        def jumping(uid):
            result = probe(uid)
            self.offset += 120
            return result
        self.collector.probe = jumping
        self.collector.collect_once()
        self.assertFalse(self.collector.path.exists())
        self.assertIn('Windows 时间发生跳变', self.collector.snapshot()['by_uav']['1']['message'])
        self.updated.assert_not_called()

    def test_clock_jump_in_failed_probes_discards_earlier_success(self):
        probe = self.collector.probe
        calls = []
        def failing_probe(uid):
            calls.append(uid)
            if len(calls) == 1:
                return probe(uid)
            self.now += .5
            self.offset += 60.
            raise TimeoutError('探针失败')
        self.collector.probe = failing_probe
        self.collector.collect_once()
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.collector.path.exists())
        self.assertIn('Windows 时间发生跳变', self.collector.snapshot()['by_uav']['1']['message'])
        self.updated.assert_not_called()

    def test_reboot_during_batch_discards_prior_good_sample(self):
        probe = self.collector.probe
        def rebooting(uid):
            result = probe(uid)
            self.boot = BOOT2
            return result
        self.collector.probe = rebooting
        self.collector.collect_once()
        self.assertFalse(self.collector.path.exists())
        self.assertIn('重新开机', self.collector.snapshot()['by_uav']['1']['message'])

    def test_lag_offline_and_nonpublisher_do_not_change_time(self):
        self.enabled = False
        self.collector.probe = Mock(side_effect=AssertionError('ordinary terminal must not collect'))
        self.collector.collect_once()
        self.collector.probe.assert_not_called()
        self.enabled = True
        self.delays = iter((2.1, 3., 4.))
        self.collector.probe = self.probe
        self.collector.collect_once()
        self.assertFalse(self.collector.path.exists())
        self.assertIn('超过 2 秒', self.collector.snapshot()['by_uav']['1']['message'])

    def test_one_aircraft_failure_does_not_block_other_aircraft(self):
        self.collector.connected_uavs = lambda: [1, 2]
        self.collector.probe = lambda uid: (dict(boot_id=BOOT1, remote_monotonic=123., remote_wall=1.)
            if uid == 1 else (_ for _ in ()).throw(TimeoutError('离线')))
        self.collector.collect_once()
        state = self.collector.snapshot()['by_uav']
        self.assertEqual(state['1']['state'], 'ready')
        self.assertEqual(state['2']['state'], 'unavailable')

    def test_new_or_reconnected_aircraft_sample_promptly_without_repeating_every_tick(self):
        self.collector.probe = Mock(return_value=dict(boot_id=BOOT1, remote_monotonic=200., remote_wall=1.))
        self.collector.collect_once(due_only=True)
        self.collector.collect_once(due_only=True)
        self.assertEqual(self.collector.probe.call_count, 3)
        self.collector.connected_uavs = lambda: []
        self.collector.collect_once(due_only=True)
        self.assertEqual(self.collector.snapshot()['by_uav']['1']['state'], 'disconnected')
        self.collector.connected_uavs = lambda: [1]
        self.collector.collect_once(due_only=True)
        self.assertEqual(self.collector.probe.call_count, 6)

    def test_corrupt_history_is_not_overwritten(self):
        self.collector.path.write_text('broken', encoding='utf-8')
        collector = self.make_collector()
        collector.collect_once()
        self.assertEqual(collector.path.read_text(), 'broken')
        self.assertIn('未覆盖历史文件', collector.snapshot()['by_uav']['1']['message'])


class TimeReferenceApiTest(unittest.TestCase):
    def setUp(self):
        SixGroundClockTest.setUp(self)
        for uid, app in self.apps.items():
            collector = app.state.target_time_reference_collector
            collector.path = Path(self.tmp.name) / str(uid) / 'time_references.json'
            collector._references = {}
            collector._load_error = ''
    select = SixGroundClockTest.select

    def network(self, url, method='GET', payload=None, timeout=2.):
        return SixGroundClockTest.network(self, url, method, payload)

    def attach(self, uid, boot):
        manager = SimpleNamespace(local=dict(uav_id=uid),
            _request=Mock(return_value=dict(boot_id=boot, remote_monotonic=100., remote_wall=123.)))
        self.apps[uid].state.program_manager = manager
        self.apps[uid].state.orchestrator.update_telemetry(Telemetry(
            uav_id=uid, received_at=time.time(), connected=True))
        return manager

    def test_peer_probe_auth_binding_freshness_and_read_only_operation(self):
        self.select(2)
        manager = self.attach(2, BOOT2)
        route = '/api/v1/peer/clock-probe/2'
        self.assertEqual(self.clients[2].get(route).status_code, 403)
        self.assertEqual(self.clients[2].get('/api/v1/peer/clock-probe/1', headers=self.headers).status_code, 404)
        response = self.clients[2].get(route, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        manager._request.assert_called_once_with('clock_probe', {})
        self.apps[2].state.orchestrator.update_telemetry(Telemetry(
            uav_id=2, received_at=time.time()-10, connected=True))
        self.assertEqual(self.clients[2].get(route, headers=self.headers).status_code, 503)
        self.assertEqual(manager._request.call_count, 1)

    def test_publisher_collects_local_and_peer_against_only_its_own_clock(self):
        self.select(2)
        self.select(1, True)
        first = self.attach(1, BOOT1)
        second = self.attach(2, BOOT2)
        self.apps[1].state.orchestrator.update_telemetry(Telemetry(
            uav_id=2, received_at=time.time(), connected=True))
        timer_before = self.apps[1].state.competition_clock.snapshot()
        collector = self.apps[1].state.target_time_reference_collector
        collector.collect_once()
        references = json.loads(collector.path.read_text())['references']
        self.assertEqual(set(references), {BOOT1, BOOT2})
        for reference in references.values():
            self.assertAlmostEqual(reference['ground_epoch'], time.time(), delta=2.)
            self.assertEqual(reference['publisher_terminal_id'], 1)
        self.assertEqual(first._request.call_count, 3)
        self.assertEqual(second._request.call_count, 3)
        for manager in (first, second):
            self.assertTrue(all(call.args == ('clock_probe', {}) for call in manager._request.call_args_list))
        self.assertEqual(self.apps[1].state.competition_clock.snapshot()['revision'], timer_before['revision'])
        self.assertEqual(self.clients[1].get('/api/v1/status').json()['target_time_reference']['reference_count'], 2)
        self.apps[2].state.target_time_reference_collector.collect_once()
        self.assertFalse(self.apps[2].state.target_time_reference_collector.path.exists())

    def test_reference_update_only_enqueues_rebuild_on_publisher(self):
        from competition_backend.api import create_app
        root = Path(self.tmp.name) / 'reference-rebuild'
        root.mkdir()
        app = create_app({'COMPETITION_ADAPTER': 'distributed', 'COMPETITION_LOCAL_UAV_ID': '1',
                          'COMPETITION_TASK_PUBLISHER': '1', 'COMPETITION_IMAGE_ROOT': str(root),
                          'COMPETITION_DATA_DIR': str(root / 'data'),
                          'COMPETITION_DIAGNOSTICS_DIR': str(root / 'logs')})
        self.addCleanup(app.state.audit.close)
        (root / 'UAV1' / 'subject1-time-test').mkdir(parents=True)
        collector = app.state.target_time_reference_collector
        # 缺省入口未配置发布角色时先模拟配置完成；only callback still checks role.
        from fastapi.testclient import TestClient
        client = TestClient(app)
        response = client.post('/api/v1/operator', json={
            'ground_terminal_id': 1, 'model': 'p600', 'task_publisher': True})
        self.assertEqual(response.status_code, 200, response.text)
        collector.on_update(1, {})
        self.assertEqual(app.state.image_collector.status()['pending_missions'], ['subject1-time-test'])
        # _queue_mission仅发唤醒事件，未创建新成果文件或调用机载动作。
        self.assertFalse((root / 'subject1-time-test' / 'target-submission.json').exists())
        ordinary = self.apps[2]
        ordinary.state.image_collector._queue_mission = Mock()
        ordinary.state.target_time_reference_collector.on_update(2, {})
        ordinary.state.image_collector._queue_mission.assert_not_called()
