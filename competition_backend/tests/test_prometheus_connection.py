import json
from pathlib import Path
import socket
import struct
import time
import unittest
from unittest.mock import Mock, patch

from competition_backend.prometheus_connection import PrometheusConnection, connect_packet, crc16, decode_packet


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.local = dict(uav_id=2, model='p600', onboard_host='192.168.1.207', ground_host='192.168.1.230')
        self.connection = PrometheusConnection(self.local)
        self.connection._listen = Mock()
        self.connection._connect = Mock()
        self.probe = dict(boot_id='boot-one', bridge=dict(ROBOT_ID=2, uav_id=2, is_simulation=0,
            autoload=True, uav_control_start='vendor-command', tcp_port=55555, tcp_heartbeat_port=55556),
            flight={}, registered=False, base_present=False, business_present=False, prepared=False)
        self.switch = Mock(return_value=dict(ready=False, detail='等待参数'))

    def heartbeat(self):
        self.connection.boot_id = self.probe['boot_id']
        self.connection.last_heartbeat = time.monotonic()

    def test_native_vendor_packet_for_all_six_ids(self):
        # 来自 UAV2 实装厂商 libcommunication_aarch64.so 的 encodeMsg<ModeSelection>。
        checksums = ['523a', 'b3f5', 'ed70', '722a', '2caf', 'cd60']
        for uid, checksum in enumerate(checksums, 1):
            packet = connect_packet(uid)
            self.assertEqual(packet[:8], b'am\x53\x00\x00\x00\xca' + bytes([uid]))
            self.assertEqual(packet[-2:].hex(), checksum)
            kind, decoded_id, body = decode_packet(packet)
            self.assertEqual((kind, decoded_id), (202, uid))
            self.assertEqual(body, dict(cmd='', is_simulation=False, mode=1,
                                       selectId=[uid], swarm_num=0, use_mode=0))
            with self.assertRaises(ValueError):
                decode_packet(packet[:-1] + bytes([packet[-1] ^ 1]))
        for uid in (0, 7, True, '2'):
            with self.assertRaises(ValueError):
                connect_packet(uid)

    def test_cold_boot_connects_but_does_not_start_business_or_switch_without_flight(self):
        result = self.connection.step(self.probe, self.switch)
        self.assertFalse(result['ready'])
        self.connection._connect.assert_called_once_with(55555)
        self.switch.assert_not_called()

    def test_waits_for_heartbeat_and_flight_then_prepares_gps(self):
        self.heartbeat()
        self.connection.step(self.probe, self.switch)
        self.switch.assert_not_called()
        self.probe['flight'] = dict(connected=True, armed=False, location_source=10)
        self.connection.step(self.probe, self.switch)
        self.switch.assert_called_once_with('boot-one')

    def test_prepared_reconnect_only_recovers_heartbeat(self):
        self.probe.update(prepared=True, registered=True, base_present=True)
        self.probe['flight'] = dict(connected=True, armed=True, location_source=4)
        self.connection.step(self.probe, self.switch)
        self.connection._connect.assert_called_once_with(55555)
        self.switch.assert_not_called()
        self.heartbeat()
        result = self.connection.step(self.probe, self.switch)
        self.assertTrue(result['ready'])
        self.assertFalse(result['auto_start_allowed'])

    def test_reused_gps_audit_is_forwarded_and_preserved_on_reconnect(self):
        self.heartbeat()
        self.probe['flight'] = dict(connected=True, armed=False, location_source=4)
        audit = dict(boot_id='boot-one', mode='reuse_existing_gps', parameter_changes={}, ready=True)
        self.switch.return_value = dict(ready=True, detail='已复用', preparation_audit=audit)
        result = self.connection.step(self.probe, self.switch)
        self.assertEqual(result['preparation_audit'], audit)
        self.probe.update(prepared=True, preparation_audit=audit)
        result = self.connection.step(self.probe, self.switch)
        self.assertEqual(result['preparation_audit'], audit)
        self.switch.assert_called_once()

    def test_existing_flight_never_switches_gps(self):
        self.heartbeat()
        self.probe['flight'] = dict(connected=True, armed=True, location_source=10)
        result = self.connection.step(self.probe, self.switch)
        self.assertTrue(result['ready'])
        self.assertFalse(result['auto_start_allowed'])
        self.switch.assert_not_called()

    def test_partial_base_and_ros_restart_never_restart_base(self):
        for key in ('base_present', 'business_present', 'prepared'):
            self.probe[key] = True
            with self.assertRaisesRegex(RuntimeError, '不重复启动'):
                self.connection.step(self.probe, self.switch)
            self.probe[key] = False
        self.connection._connect.assert_not_called()

    def test_port_conflict_never_kills_or_takes_over_other_listener(self):
        self.connection._listen.side_effect = OSError(10048, 'in use')
        with self.assertRaisesRegex(RuntimeError, '占用'):
            self.connection.step(self.probe, self.switch)
        self.probe.update(registered=True, prepared=True)
        self.probe['flight'] = dict(connected=True, armed=False, location_source=4)
        self.assertTrue(self.connection.step(self.probe, self.switch)['ready'])
        self.connection._connect.assert_not_called()

    def test_wrong_id_and_simulation_cannot_send_connect(self):
        self.probe['bridge']['ROBOT_ID'] = 4
        with self.assertRaises(RuntimeError):
            self.connection.step(self.probe, self.switch)
        self.probe['bridge']['ROBOT_ID'] = 2
        self.probe['bridge']['is_simulation'] = 1
        with self.assertRaises(RuntimeError):
            self.connection.step(self.probe, self.switch)
        self.connection._connect.assert_not_called()

    def test_real_listener_fragmented_frames_rejects_wrong_uav(self):
        connection = PrometheusConnection(dict(uav_id=2, onboard_host='127.0.0.1', ground_host='127.0.0.1'))
        connection._listen('127.0.0.1', 0)
        self.addCleanup(connection.close)
        address = connection.socket.getsockname()
        for uid in (1, 2):
            data = b'am' + struct.pack('<IBB', 11, 6, uid) + b'{"count":1}'
            packet = data + struct.pack('<H', crc16(data))
            with socket.create_connection(address, timeout=1) as sock:
                sock.sendall(packet[:3]); sock.sendall(packet[3:])
            deadline = time.monotonic() + .6
            while not connection.last_heartbeat and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(bool(connection.last_heartbeat), uid == 2)


class PreparationIntegrationTests(unittest.TestCase):
    def test_ground_saves_parameter_audit_once_and_recovers_it_after_restart(self):
        import tempfile
        from competition_backend.program_manager import ProgramManager
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root/'tools/remote_prometheus.py'
            helper.parent.mkdir(); helper.write_text('', encoding='utf8')
            audit = dict(boot_id='boot-one', mode='reuse_existing_gps', ready=True,
                         parameter_changes={}, before=dict(control=dict(maximum_safe_vel_z=6)),
                         after=dict(control=dict(maximum_safe_vel_z=6)))
            for _ in range(2):
                manager = ProgramManager(directory, {})
                manager.local = dict(uav_id=2, model='p600', onboard_host='test')
                manager.connection = Mock()
                manager.connection.step.return_value = dict(ready=True, detail='已复用', preparation_audit=audit)
                manager._request = Mock(return_value=dict(boot_id='boot-one'))
                manager.audit = Mock()
                manager._prepare_connection(force=True)
                manager._prepare_connection(force=True)
                events = [c for c in manager.audit.record.call_args_list if c.args[0] == '厂商GPS准备参数核验']
                self.assertEqual(len(events), 1)
            path = root/'ground_logs/programs/uav2_connection_parameters.jsonl'
            entries = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(entries), 2)  # 每个地面会话补记一次，轮询不重复刷写。
            self.assertEqual(entries[0]['before']['control']['maximum_safe_vel_z'], 6)
            self.assertEqual(entries[0]['parameter_changes'], {})
            self.assertIn('ground_recorded_at', entries[0])

    def test_four_program_start_waits_for_preparation_and_runs_once(self):
        import tempfile
        from competition_backend.program_manager import ProgramManager
        with tempfile.TemporaryDirectory() as directory:
            manager = ProgramManager(directory, {})
            manager.local = dict(uav_id=2, model='p600', onboard_host='test')
            manager._prepare_connection = Mock(side_effect=[
                dict(ready=False, state='starting', detail='等待飞控'),
                dict(ready=False, state='starting', detail='等待 GPS'),
                dict(ready=True, auto_start_allowed=True),
                dict(ready=True, auto_start_allowed=True)])
            manager.stop_event.wait = Mock(return_value=False)
            calls = []
            def request(action, offsets):
                calls.append(action)
                if action == 'auto_start': manager.stop_event.set()
                return dict({key: dict(state='idle', detail='尚未启动') for key in ('onboard','detection','detection_person','flight','drop')},
                            _lifecycle=dict(boot_id='boot', auto_start_pending=['onboard','detection','detection_person','flight','drop']))
            manager._request = request
            manager._watch()
            self.assertEqual(calls, ['status','status','status','auto_start'])

    def test_config_failure_preserves_program_monitoring(self):
        import tempfile
        from competition_backend.program_manager import ProgramManager
        with tempfile.TemporaryDirectory() as directory:
            manager = ProgramManager(directory, {})
            manager.local = dict(uav_id=2, model='p600', onboard_host='test')
            config = Path(directory)/'config/onboard_programs.json'
            config.parent.mkdir(); config.write_text('{broken', encoding='utf8')
            calls = []
            def request(action, offsets):
                calls.append(action); manager.stop_event.set()
                return {key: dict(state='running', detail='已就绪') for key in ('onboard','detection','detection_person','flight','drop')}
            manager._request = request
            manager._watch()
            self.assertEqual(calls, ['status'])
            self.assertEqual(manager.connection_status['state'], 'error')
            self.assertEqual(manager.states['flight']['state'], 'running')

    def test_background_close_cannot_send_connect_after_probe(self):
        import tempfile
        from competition_backend.program_manager import ProgramManager
        with tempfile.TemporaryDirectory() as directory:
            manager = ProgramManager(directory, {})
            manager.local = dict(uav_id=2, model='p600', onboard_host='test')
            helper = Path(directory)/'tools/remote_prometheus.py'
            helper.parent.mkdir(); helper.write_text('', encoding='utf8')
            manager.connection = Mock()
            def request(*args): manager.stop_event.set(); return dict(boot_id='boot')
            manager._request = request
            self.assertFalse(manager._prepare_connection(force=True)['ready'])
            manager.connection.step.assert_not_called()


if __name__ == '__main__':
    unittest.main()
