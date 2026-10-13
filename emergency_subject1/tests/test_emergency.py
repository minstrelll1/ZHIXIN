"""Offline tests: loopback sockets + actual executor methods, flight/ROS/SSH stubbed."""
import copy
import importlib.util
import json
import math
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'emergency_subject1'
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(ROOT / 'src/su17_competition_executor/test'))
import test_executor_autonomy as autonomy
from su17_competition_executor.tcp_link import OnboardTcpLink
from su17_competition_executor.task_protocol import validate_assignment as original_validate
from common import (Failure, assignment_checksum, build_bundle, fleet_status, load_fixed,
                    load_settings, request, validate_fixed_assignment)
from relay import Relay, make_http_server


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, TOOL/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = load_script('01_dispatch').dispatch
takeoff = load_script('02_takeoff').takeoff


def until(condition, timeout=8):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.05)
    raise AssertionError('condition timed out')


class Aircraft:
    def __init__(self, uid, folder, port):
        self.uid = uid
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.commands = []
        self.drop_assignments = 0
        self.drop_takeoffs = 0
        self.wrong_receipt = False
        self.stop_event = threading.Event()
        node = self.node = autonomy.AutonomyTest().executor(folder)
        node.uav_id = uid
        node._boot_id = self.boot
        node._assignment = None
        node._assignment_acked = False
        node._progress = {'phase': 'idle', 'next_waypoint': 0}
        node._competition_time_state = None
        node.max_distance_from_home = 2000.
        node.max_speed = 1.
        node.log_full_task = False
        node.enable_motion = True
        node._manual_override = False
        node.recognition_categories_pub = Mock()
        node.recognition_categories_topic = '/uav%d/competition/recognition_categories' % uid
        node.task_region_pub = Mock()
        node.task_region_topic = '/ground_mission_planner/vehicle_%d/task_region' % uid
        node.external_entry_path_pub = Mock()
        node.external_return_paths_pub = Mock()
        node._publish_successful_return = Mock()
        landing = load_fixed()['assignments'][str(uid)]['landing_point_wgs84']
        node._state = SimpleNamespace(armed=False, odom_valid=True, gps_status=3,
            location_source=4, latitude=landing[0], longitude=landing[1], altitude=103.,
            position=[0., 0., 0.], velocity=[0., 0., 0.], attitude=[0., 0., 0.])
        node._snapshot = lambda: (node._state, None)
        def simulated_climb(payload):
            node._gps_home = (landing[0], landing[1], 103.)
            node._state.armed = True
            node._checkpoint('taking_off', execution=dict(payload))
            return True
        node._run_takeoff = simulated_climb
        identity = dict(model='p600', flight_controller_id=uid, ros_namespace='/uav%d' % uid,
                        identity_verified=True, device_id='offline-%d' % uid)
        self.link = OnboardTcpLink(uid, '127.0.0.1', port, 'offline-tcp', self.handle,
                                   reconnect_seconds=.2, identity=identity)
        original_probe = self.link._clock_probe_reply
        def probe(message):
            reply = original_probe(message)
            reply['remote_wall'] += uid*10000  # Six deliberately unsynchronized clocks.
            return reply
        self.link._clock_probe_reply = probe
        self.link.start()
        self.thread = threading.Thread(target=self.telemetry_loop, daemon=True)
        self.thread.start()

    def handle(self, payload):
        if payload['type'] == 'sync_competition_time':
            self.node._handle_high_level_payload(payload)
            return
        self.commands.append(copy.deepcopy(payload))
        if payload['type'] == 'assign_task' and self.drop_assignments:
            self.drop_assignments -= 1
            return
        if payload['type'] == 'takeoff' and self.drop_takeoffs:
            self.drop_takeoffs -= 1
            return
        self.node._handle_high_level_payload(payload)

    def telemetry_loop(self):
        while not self.stop_event.wait(.05):
            assignment = self.node._assignment or {}
            self.link.update_telemetry(dict(type='telemetry', uav_id=self.uid,
                connected=True, armed=self.node._state.armed, odom_valid=True, failsafe=False,
                control_state=0, battery_percentage=.95, position=[0, 0, 0], velocity=[0, 0, 0],
                latitude=self.node._state.latitude, longitude=self.node._state.longitude,
                altitude=103, gps_status=3, location_source=4,
                capabilities=dict(boot_id=self.boot, motion_enabled=True, auto_takeoff_supported=True,
                    auto_takeoff_ready=True, auto_takeoff_reason='ready', boot_gps_home=None),
                task_phase=self.node._progress.get('phase'), task_complete=False,
                task_assignment_acked=self.node._assignment_acked,
                task_assignment_mission_id=assignment.get('mission_id', ''),
                task_assignment_checksum='wrong-checksum' if self.wrong_receipt else assignment.get('assignment_checksum', '')))

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.link.stop()
        motion = self.node._motion_thread
        if motion:
            motion.join(timeout=2)


class ProtocolTests(unittest.TestCase):
    def test_fixed_routes_geometry_and_original_validator(self):
        from shapely.geometry import LineString, Point, Polygon
        config, _fleet = load_settings()
        config['recognition_category_ids'] = [0, 7, 18]
        bundle = build_bundle(config, 'offline-test', 'offline-session', 123.)
        original = json.loads((ROOT/'competition_backend/competition_backend/subject1_actual_prepared.json').read_text())['plan']
        fixed = load_fixed()
        projection = fixed['layout']['projection']
        def local(p):
            return [(p[1]-projection['latitude'])*projection['north_m_per_degree'],
                    -(p[0]-projection['longitude'])*projection['west_m_per_degree']]
        boundary = Polygon(original['search_area']['points_m']).boundary
        homes = {uid: local([msg['landing_point_wgs84'][1], msg['landing_point_wgs84'][0]])
                 for uid, msg in bundle['assignments'].items()}
        for a, b in ((1,2),(3,4),(5,6),(1,3),(3,5),(2,4),(4,6)):
            self.assertAlmostEqual(math.dist(homes[str(a)], homes[str(b)]), 7., places=7)
        for uid, message in bundle['assignments'].items():
            original_validate(message, int(uid))
            validate_fixed_assignment(message, int(uid))
            task = message['task']
            self.assertEqual(task['waypoints_wgs84'], original['planned_uavs'][uid]['task']['waypoints_wgs84'])
            self.assertNotIn('speed_mps', task)
            self.assertEqual(task['recognition_selection']['category_ids'], [0, 7, 18])
            routes = task['transit_routes']
            self.assertEqual(len(routes['return_paths']), 67)
            paths = [routes['entry_path']] + [r['path'] for r in routes['return_paths']]
            for path in paths:
                line = LineString([local(p) for p in path])
                self.assertGreaterEqual(line.distance(boundary), 5.)
                for other, point in homes.items():
                    if other != uid:
                        self.assertGreaterEqual(line.distance(Point(point)), 2.5)

    def test_changed_landing_point_even_with_valid_checksum_is_rejected(self):
        config, _ = load_settings()
        assignment = build_bundle(config, 'offline', 'session', 0)['assignments']['1']
        assignment['landing_point_wgs84'][0] += .001
        assignment['assignment_checksum'] = assignment_checksum(assignment)
        with self.assertRaises(ValueError):
            validate_fixed_assignment(assignment, 1)

    def test_invalid_category_and_time_are_rejected(self):
        config, _ = load_settings()
        config['recognition_category_ids'] = [19]
        with self.assertRaises(ValueError):
            build_bundle(config, 'offline', 'session', 0)
        config['recognition_category_ids'] = []
        with self.assertRaises(Failure):
            build_bundle(config, 'offline', 'session', float('nan'))

    def test_vendor_files_are_exact_copies(self):
        import hashlib
        for target, info in json.loads((TOOL/'vendor_sources.json').read_text())['files'].items():
            self.assertEqual((TOOL/target).read_bytes(), (ROOT/info['source']).read_bytes())
            self.assertEqual(hashlib.sha256((TOOL/target).read_bytes()).hexdigest(), info['sha256'])


class SixAircraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config, self.fleet = load_settings()
        self.config['recognition_category_ids'] = [0, 7, 18]
        self.relays, self.servers, self.planes, self.endpoints = {}, {}, {}, {}
        self.check_b = patch('relay.check_program_b', return_value={'ready': True})
        self.check_b.start()
        for uid in range(1, 7):
            folder = self.root/str(uid)
            folder.mkdir()
            relay = Relay(uid, self.config, self.fleet, 'offline-peer', 'offline-tcp', folder,
                          bind_host='127.0.0.1', tcp_port=0)
            server = make_http_server(relay, '127.0.0.1', 0)
            relay.start()
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.1}, daemon=True).start()
            self.relays[uid], self.servers[uid] = relay, server
            self.endpoints[uid] = 'http://127.0.0.1:%d' % server.server_address[1]
            self.planes[uid] = Aircraft(uid, folder, relay.adapter.bound_port)
        until(lambda: all(r.status()['fresh'] for r in self.relays.values()))

    def tearDown(self):
        for plane in self.planes.values():
            plane.stop()
        for server in self.servers.values():
            server.shutdown()
            server.server_close()
        for relay in self.relays.values():
            relay.stop()
        self.check_b.stop()
        self.directory.cleanup()

    def assign(self, timeout=15):
        return dispatch(self.config, self.endpoints, 'offline-peer', self.root/'publisher',
                        elapsed_seconds=125, timeout=timeout)

    def test_tcp_six_assignments_partial_takeoff_retry_and_ros_handoff(self):
        self.planes[6].drop_assignments = 1
        bundle = self.assign()
        for uid, plane in self.planes.items():
            self.assertEqual(plane.node._assignment['mission_id'], bundle['mission_id'])
            plane.node.external_path_pub.publish.assert_not_called()
            plane.node.recon_start_mode_pub.publish.assert_not_called()
            self.assertEqual(plane.node.recognition_categories_pub.publish.call_args.args[0].data, [0,7,18])
            plane.node.task_region_pub.publish.assert_called()
        count_before = [len(p.commands) for p in self.planes.values()]
        takeoff(self.endpoints, 'offline-peer', self.root/'publisher')  # Check-only.
        self.assertEqual(count_before, [len(p.commands) for p in self.planes.values()])
        self.planes[6].drop_takeoffs = 1
        with self.assertRaisesRegex(Failure, '尚未观察到'):
            takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True, timeout=2)
        first_payload = copy.deepcopy(self.relays[6].state['takeoff']['payload'])
        self.relays[6].state['takeoff']['attempted_at'] -= 10
        takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True, retry_missing=True, timeout=8)
        self.assertEqual(first_payload, self.relays[6].state['takeoff']['payload'])
        for uid, plane in self.planes.items():
            calls = [c for c in plane.commands if c['type'] == 'takeoff']
            self.assertEqual(len(calls), 2 if uid == 6 else 1)
            self.assertAlmostEqual(calls[-1]['deadline_at']-uid*10000-time.time(),1140,delta=15)
            node = plane.node
            node.external_path_pub.publish.assert_called_once()
            node.external_entry_path_pub.publish.assert_called_once()
            node.external_return_paths_pub.publish.assert_called_once()
            returns = json.loads(node.external_return_paths_pub.publish.call_args.args[0].data)
            self.assertEqual(len(returns['routes']), 67)
            landing = node.external_landing_pub.publish.call_args.args[0].data
            expected = bundle['assignments'][str(uid)]['landing_point_wgs84']
            self.assertEqual(landing, [expected[1], expected[0], expected[2]])
            for route in returns['routes']:
                self.assertEqual(route['path'][-1], landing)
                self.assertTrue(all(point[2] == expected[2] for point in route['path']))
            self.assertEqual(node.recon_start_mode_pub.publish.call_args.args[0].data, 0)
            node.image_mission_control_pub.publish.assert_called_once()
            node._fly_to.assert_not_called()
            node._land.assert_not_called()
        before = [len(p.commands) for p in self.planes.values()]
        takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True)
        self.assertEqual(before, [len(p.commands) for p in self.planes.values()])

    def test_partial_or_wrong_receipt_blocks_all_takeoffs(self):
        self.planes[6].wrong_receipt = True
        with self.assertRaisesRegex(Failure, '未收到全部六机回执'):
            self.assign(timeout=1)
        with self.assertRaises(Failure):
            takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True)
        self.assertFalse(any(c['type'] == 'takeoff' for p in self.planes.values() for c in p.commands))

    def test_program_b_failure_blocks_the_whole_fleet(self):
        self.assign()
        def probe(vehicle, config):
            if vehicle['uav_id'] == 3:
                raise Failure('offline B unavailable')
            return {'ready': True}
        with patch('relay.check_program_b', side_effect=probe):
            with self.assertRaisesRegex(Failure, '本轮没有发送起飞'):
                takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True)
        self.assertFalse(any(c['type'] == 'takeoff' for p in self.planes.values() for c in p.commands))

    def test_reconnect_never_replays_assignment_or_takeoff_and_auth_is_required(self):
        self.assign()
        before = [len(p.commands) for p in self.planes.values()]
        plane = self.planes[2]
        old_socket = plane.link._socket
        old_socket.shutdown(2)
        until(lambda: plane.link._socket is not None and plane.link._socket is not old_socket and plane.link.connected)
        time.sleep(.3)
        self.assertEqual(before, [len(p.commands) for p in self.planes.values()])
        with self.assertRaisesRegex(Failure, '认证失败'):
            request(self.endpoints[1], 'wrong-token', '/status')

    def test_rebooted_aircraft_cannot_take_off_with_old_assignment(self):
        self.assign()
        self.planes[4].boot = 'different-boot'
        until(lambda: self.relays[4].status()['boot_changed'])
        with self.assertRaisesRegex(Failure, '重新开机'):
            takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True)
        self.assertFalse(any(c['type'] == 'takeoff' for p in self.planes.values() for c in p.commands))

    def test_relay_restart_restores_clock_and_journal_without_sending(self):
        self.assign()
        uid = 2
        before = len(self.planes[uid].commands)
        previous = self.relays[uid].status()
        port = self.relays[uid].adapter.bound_port
        self.servers[uid].shutdown()
        self.servers[uid].server_close()
        self.relays[uid].stop()
        replacement = Relay(uid, self.config, self.fleet, 'offline-peer', 'offline-tcp', self.root/str(uid),
                            bind_host='127.0.0.1', tcp_port=port)
        server = make_http_server(replacement, '127.0.0.1', 0)
        replacement.start()
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.1}, daemon=True).start()
        self.relays[uid], self.servers[uid] = replacement, server
        self.endpoints[uid] = 'http://127.0.0.1:%d' % server.server_address[1]
        until(lambda: replacement.status()['fresh'])
        restored = replacement.status()
        self.assertEqual(restored['mission_id'], previous['mission_id'])
        self.assertEqual(restored['assignment_checksum'], previous['assignment_checksum'])
        self.assertGreaterEqual(restored['competition_time']['elapsed_seconds'], previous['competition_time']['elapsed_seconds'])
        self.assertEqual(before, len(self.planes[uid].commands))

    def test_recorded_failure_prevents_retry_and_new_assignment_in_flight_is_blocked(self):
        bundle = self.assign()
        plane = self.planes[1]
        plane.node._progress['phase'] = 'takeoff_failed'
        until(lambda: self.relays[1].status()['failure_phase'] == 'takeoff_failed')
        with self.assertRaisesRegex(Failure, '起飞失败'):
            takeoff(self.endpoints, 'offline-peer', self.root/'publisher', send=True, retry_missing=True)
        self.assertFalse(any(c['type'] == 'takeoff' for p in self.planes.values() for c in p.commands))
        plane.node._state.armed = True
        until(lambda: self.relays[1].status()['telemetry']['armed'])
        with self.assertRaisesRegex(Failure, '已解锁'):
            request(self.endpoints[1], 'offline-peer', '/assignment',
                    {'assignment': bundle['assignments']['1'], 'elapsed_seconds': 140})


if __name__ == '__main__':
    unittest.main()
