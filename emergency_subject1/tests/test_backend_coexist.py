"""Six unchanged FastAPI apps and real onboard acceptance; no flight/network I/O."""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'competition_backend'),
                str(ROOT / 'src/su17_competition_executor/test')]
# Load the complete original shared package before the client-side frozen subset.
from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_backend.subject1_reporting import Subject1Reporter
from fastapi.testclient import TestClient
from competition_shared.fleet import apply_fixed_binding, default_fleet
import test_executor_autonomy as autonomy
from su17_competition_executor.task_protocol import validate_assignment
sys.path.insert(0, str(ROOT / 'emergency_subject1'))
import backend_client as bc
from common import Failure, load_fixed, load_settings


class CoexistTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, _ = load_settings()
        self.apps, self.clients, self.nodes, self.wires = {}, {}, {}, {}
        self.calls = []
        self.suppress_receipt = set()
        self.fail_takeoff = None
        self.lost_response = None
        self.boots = {uid: 'offline-boot-' + str(uid) for uid in range(1, 7)}
        fleet = default_fleet()
        for uid in range(1, 7):
            fleet = apply_fixed_binding(fleet, uid, 'p600')

        def network(url, method='GET', payload=None, timeout=2.):
            for uid, client in self.clients.items():
                prefix = 'http://192.168.2.%d:8000' % (197 + uid * 5)
                if url.startswith(prefix + '/'):
                    reply = client.request(method, url[len(prefix):], json=payload,
                                           headers={'X-Competition-Peer-Token': 'offline-peer'})
                    if reply.status_code >= 400:
                        raise RuntimeError(reply.text)
                    return reply.json()
            raise AssertionError('Unexpected external request: ' + url)

        for uid in range(1, 7):
            folder = self.root / str(uid)
            folder.mkdir()
            fleet_path = folder / 'fleet.json'
            fleet_path.write_text(json.dumps(fleet), encoding='utf-8')
            app = create_app(dict(COMPETITION_ADAPTER='distributed', COMPETITION_FLEET_CONFIG=str(fleet_path),
                COMPETITION_DATA_DIR=str(folder), COMPETITION_DIAGNOSTICS_DIR=str(folder / 'logs'),
                COMPETITION_IMAGE_ROOT=str(folder / 'images'), COMPETITION_GROUND_TERMINAL_ID=str(uid),
                COMPETITION_PEER_TOKEN='offline-peer', COMPETITION_ASYNC_OPERATOR_SELECTION='0',
                COMPETITION_CONFIRM_LIVE_CONFIG='1', COMPETITION_PUBLISHER_RESULTS='1' if uid == 1 else '0'))
            self.addCleanup(app.state.audit.close)
            app.state.program_manager = Mock()
            app.state.program_manager.snapshot.return_value = {'programs': {'flight': {
                'state': 'running', 'flight_mode': 'outdoor'}}}
            app.state.adapter._request_json = network
            app.state.adapter.local_adapter.forward_command = Mock(side_effect=self.forward)
            self.apps[uid], self.clients[uid] = app, TestClient(app, raise_server_exceptions=False)
            self.addCleanup(self.clients[uid].close)
            node = autonomy.AutonomyTest().executor(folder)
            node.uav_id = uid
            node._assignment = None
            node._assignment_acked = False
            node._boot_id = self.boots[uid]
            node._gps_home = None  # No boot GPS/home data anywhere in the fixture.
            node._progress = {'phase': 'idle', 'next_waypoint': 0}
            node.max_distance_from_home, node.max_speed, node.log_full_task = 2000., 1., False
            for name in ('recognition_categories_pub', 'task_region_pub', 'external_entry_path_pub',
                         'external_return_paths_pub', '_publish_successful_return'):
                setattr(node, name, Mock())
            node.recognition_categories_topic = '/test/categories'
            node.task_region_topic = '/test/region'
            landing = load_fixed()['assignments'][str(uid)]['landing_point_wgs84']
            state = SimpleNamespace(armed=False, odom_valid=True, gps_status=3, location_source=4,
                latitude=landing[0], longitude=landing[1], altitude=103.7,
                position=[0., 0., 0.], velocity=[0., 0., 0.], attitude=[0., 0., 0.])
            node._state, node._snapshot = state, lambda state=state: (state, None)
            self.nodes[uid] = node
        for uid in (2, 3, 4, 5, 6, 1):
            response = self.clients[uid].post('/api/v1/operator', json=dict(
                ground_terminal_id=uid, model='p600', task_publisher=uid == 1))
            self.assertEqual(response.status_code, 200, response.text)
        self.publisher = self.apps[1]
        self.publisher.state.adapter.peer_program_b_status = Mock(side_effect=lambda: {
            str(uid): dict(state='running', received_at=time.time(), flight_mode='outdoor') for uid in range(2, 7)})
        self.publisher.state.adapter.connected_uav_ids_snapshot = Mock(return_value=list(range(1, 7)))
        self.backend = SimpleNamespace(call=self.call)
        self.folder = self.root / 'controller'
        self.refresh()

    def refresh(self):
        for uid, node in self.nodes.items():
            a = node._assignment or {}
            old = self.publisher.state.orchestrator.telemetry_snapshot().get(str(uid), {})
            self.publisher.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,
                received_at=time.time(), connected=True, armed=node._state.armed, odom_valid=True,
                control_state=0, battery_percentage=.95, gps_status=3, location_source=4,
                latitude=node._state.latitude, longitude=node._state.longitude,
                identity={'identity_verified': True}, capabilities=dict(boot_id=self.boots[uid],
                motion_enabled=True, auto_takeoff_supported=True, auto_takeoff_ready=True),
                task_phase=node._progress['phase'], task_assignment_acked=bool(a) and uid not in self.suppress_receipt,
                task_assignment_mission_id=a.get('mission_id', ''), task_assignment_checksum=a.get('assignment_checksum', ''),
                successful_return=old.get('successful_return', {})))

    def forward(self, uid, kind, payload):
        if kind == 'assign_task':
            wire = json.loads(json.dumps(dict(payload, type=kind, uav_id=uid)))
            validate_assignment(wire, uid)
            node = self.nodes[uid]
            node._accept_assignment(wire)
            self.assertTrue(node._assignment_acked, node._publish_status.call_args_list)
            self.wires[uid] = wire
        elif kind == 'takeoff':
            if self.fail_takeoff == uid:
                raise RuntimeError('simulated radio failure')
            node = self.nodes[uid]
            node._state.armed = True
            node._progress['phase'] = 'taking_off'
        else:
            raise AssertionError('Unexpected command: ' + kind)
        self.refresh()

    def call(self, path, payload=None, method=None):
        method = method or ('GET' if payload is None else 'POST')
        self.calls.append((method, path))
        self.refresh()
        reply = self.clients[1].request(method, '/api/v1' + path, json=payload)
        if reply.status_code >= 400:
            raise Failure(reply.text)
        if self.lost_response == path:
            self.lost_response = None
            raise Failure('simulated response loss')
        return reply.json()

    def dispatch(self, **kwargs):
        return bc.dispatch(self.config, self.folder, backend=self.backend, timeout=.03, **kwargs)

    def takeoff(self, **kwargs):
        return bc.takeoff(self.config, self.folder, backend=self.backend, timeout=.03, **kwargs)

    def test_six_frozen_tasks_no_homes_then_takeoff_and_original_deduplicated_report(self):
        # Any call to the live competition planner is a test failure.
        with patch('competition_backend.subject1_actual_scene.load_plan', side_effect=AssertionError('planner used')), \
             patch('competition_backend.transit_routes.rebase_gps_routes_for_takeoff',
                   side_effect=AssertionError('live home used')), \
             patch('competition_backend.orchestrator.plan_rectangular_search', side_effect=AssertionError('planner used')), \
             patch('competition_backend.orchestrator.plan_quadrilateral_search', side_effect=AssertionError('planner used')):
            record = self.dispatch(elapsed_seconds=120)
        self.assertEqual(len(self.wires), 6)
        for uid, wire in self.wires.items():
            fixed = load_fixed()['assignments'][str(uid)]
            self.assertEqual(wire['landing_point_wgs84'], fixed['landing_point_wgs84'])
            self.assertEqual(wire['landing_mode'], 'selected_departure')
            self.assertEqual(wire['coordinate_frame'], 'WGS84')
            self.assertEqual(wire['task']['transit_routes'], fixed['task']['transit_routes'])
            self.assertEqual(wire['task']['task_region'], fixed['task']['task_region'])
            self.assertEqual([p[:2] for p in wire['task']['waypoints_wgs84']], fixed['task']['waypoints_wgs84'])
            self.assertEqual(wire['task']['recognition_selection']['category_ids'], [])
            self.assertEqual(wire['competition_time']['session_id'], record['session_id'])
            self.assertGreaterEqual(wire['competition_time']['elapsed_seconds'], 120)
            self.nodes[uid].external_path_pub.publish.assert_not_called()
        self.takeoff()  # Prepare alone sends no flight commands.
        self.assertTrue(all(a.state.adapter.local_adapter.forward_command.call_count == 1 for a in self.apps.values()))
        self.takeoff(send=True)
        self.takeoff(send=True)  # No duplicated fleet confirmation.
        self.assertEqual(self.calls.count(('POST', '/takeoff/confirm')), 1)
        self.assertEqual(self.publisher.state.orchestrator.takeoff_participants_snapshot(),
                         dict(mission_id=record['mission_id'], uav_ids=list(range(1, 7))))
        reporter = self.publisher.state.auto_subject1_reporter
        for uid, node in self.nodes.items():
            _, kind, payload = self.apps[uid].state.adapter.local_adapter.forward_command.call_args.args
            self.assertEqual(kind, 'takeoff')
            node._gps_home = (node._state.latitude, node._state.longitude, 103.7)
            node._motion_entry('takeoff', lambda _: True, payload)
            expected = load_fixed()['assignments'][str(uid)]['landing_point_wgs84']
            self.assertEqual(node.external_landing_pub.publish.call_args.args[0].data,
                             [expected[1], expected[0], expected[2]])
            self.assertEqual(len(json.loads(node.external_return_paths_pub.publish.call_args.args[0].data)['routes']), 67)
            self.assertEqual(node.recon_start_mode_pub.publish.call_args.args[0].data, 0)
            # Two nearby detections per UAV, well-separated from the next UAV.
            folder = Path(reporter.image_root) / ('UAV' + str(uid)) / record['mission_id']
            folder.mkdir(parents=True)
            for index in (0, 1):
                meta = dict(mission_id=record['mission_id'], target_id='u%d-%d' % (uid, index),
                    target_type='车辆', target_model='车辆1', is_moving=False,
                    target_latitude=33.864, target_longitude=113.705 + uid * .001 + index * .00001,
                    confidence=.8 + .1 * index, localization_time=dict(secs=1700000000, nsecs=0))
                (folder / ('target%d.json' % index)).write_text(json.dumps(meta), encoding='utf-8')
                (folder / ('target%d.jpg' % index)).write_bytes(b'offline-jpeg')
            telemetry = self.publisher.state.orchestrator._telemetry[uid]
            telemetry.successful_return = dict(phase='return_descent', mission_id=record['mission_id'],
                competition_session_id=record['session_id'], uav_id=uid,
                assignment_checksum=record['checksums'][str(uid)], event_id='return-%d' % uid)
        submitted = []
        def fake_submit(instance, draft, scheduled=False):
            submitted.append(json.loads(instance.draft(draft).read_text(encoding='utf-8')))
            self.assertTrue(scheduled)
            return dict(state='http_received', http_status=200, detail='offline only')
        with patch.object(Subject1Reporter, 'submit', fake_submit):
            self.assertFalse(reporter.tick(now=100.))
            self.assertTrue(reporter.tick(now=103.1))
            until = time.monotonic() + 5
            while not reporter.snapshot()['attempts_finished'] and time.monotonic() < until:
                time.sleep(.02)
        self.assertEqual(reporter.snapshot()['participant_uav_ids'], list(range(1, 7)))
        self.assertEqual(reporter.snapshot()['last_result']['state'], 'http_received')
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len(submitted[0]['features']), 6)  # Twelve detections deduplicated to six.
        self.assertEqual(self.clients[1].get('/api/v1/subject1/report').status_code, 200)

    def test_missing_receipt_blocks_takeoff_and_resume_only_polls(self):
        self.suppress_receipt.add(6)
        with self.assertRaisesRegex(Failure, '尚缺'):
            self.dispatch()
        with self.assertRaisesRegex(Failure, '回执'):
            self.takeoff(send=True)
        self.suppress_receipt.clear()
        self.dispatch(resume=True)
        self.assertEqual(self.calls.count(('POST', '/plan/jobs')), 1)
        self.assertNotIn(('POST', '/takeoff/confirm'), self.calls)

    def test_lost_plan_response_resume_does_not_resend(self):
        self.lost_response = '/plan/jobs'
        with self.assertRaisesRegex(Failure, 'response loss'):
            self.dispatch()
        self.dispatch(resume=True)
        self.assertEqual(self.calls.count(('POST', '/plan/jobs')), 1)

    def test_partial_takeoff_or_lost_confirm_never_replays_fleet(self):
        self.dispatch()
        self.fail_takeoff = 3
        with self.assertRaisesRegex(Failure, '尚未观察到'):
            self.takeoff(send=True)
        self.fail_takeoff = None
        with self.assertRaisesRegex(Failure, '尚未观察到'):
            self.takeoff(send=True)
        self.assertEqual(self.calls.count(('POST', '/takeoff/confirm')), 1)
        self.assertTrue(self.nodes[1]._state.armed)
        self.assertFalse(self.nodes[3]._state.armed)

    def test_lost_successful_confirm_response_checks_receipts(self):
        self.dispatch()
        self.lost_response = '/takeoff/confirm'
        self.takeoff(send=True)
        self.takeoff(send=True)
        self.assertEqual(self.calls.count(('POST', '/takeoff/confirm')), 1)

    def test_wrong_report_mission_reboot_and_missing_b_block_commands(self):
        self.dispatch()
        reporter = self.publisher.state.auto_subject1_reporter
        correct = reporter._mission_id
        reporter._mission_id = 'subject1-wrong'
        with self.assertRaisesRegex(Failure, '自动上报'):
            self.takeoff(send=True)
        reporter._mission_id = correct
        self.boots[6] = 'rebooted'
        with self.assertRaisesRegex(Failure, '已重启'):
            self.takeoff(send=True)
        self.boots[6] = 'offline-boot-6'
        self.apps[1].state.program_manager.snapshot.return_value = {'programs': {'flight': {'state': 'stopped'}}}
        with self.assertRaisesRegex(Failure, '程序 B'):
            self.takeoff(send=True)
        self.assertNotIn(('POST', '/takeoff/confirm'), self.calls)

    def test_five_connected_never_accepted_as_complete(self):
        self.publisher.state.adapter.connected_uav_ids_snapshot.return_value = list(range(1, 6))
        with self.assertRaisesRegex(Failure, '全部六机'):
            self.dispatch()
        self.assertNotIn(('POST', '/takeoff/confirm'), self.calls)

    def test_takeoff_retry_missing_not_available_in_coexist_mode(self):
        with self.assertRaisesRegex(Failure, '单机补发'):
            self.takeoff(send=True, retry_missing=True)
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
