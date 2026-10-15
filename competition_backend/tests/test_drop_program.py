"""抛投程序生命周期回归：虚拟 SSH/ROS/进程，不连接无人机或操作串口。"""
import contextlib
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.ground_entry import GroundEntry
from competition_backend.program_manager import ProgramManager
from competition_shared.fleet import default_fleet
from test_program_manager import remote
import test_program_boot as boot_tests


class DropLifecycleTests(unittest.TestCase):
    setUp = boot_tests.RemoteBootTests.setUp
    spawn = boot_tests.RemoteBootTests.spawn
    rpc = boot_tests.RemoteBootTests.rpc

    def test_boot_starts_drop_once_and_reconnect_or_crash_never_restarts(self):
        self.assertIn('drop', self.rpc()['_lifecycle']['auto_start_pending'])
        self.rpc('auto_start')
        self.assertEqual({Path(call[3]).name for call in self.calls},
                         {'onboard', 'detection', 'detection_person', 'flight', 'drop'})
        self.assertEqual(next(call[-1] for call in self.calls if Path(call[3]).name == 'drop'),
                         remote.drop_command())
        self.live.clear()
        for _ in range(3):
            self.rpc('status')
            self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))
        self.boot = 'boot-b'
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 2 * len(remote.NAMES))

    def test_upgrade_of_existing_boot_does_not_add_drop_until_operator_requests(self):
        runtime = self.root / 'ground_runtime/programs_uav2'
        runtime.mkdir(parents=True)
        remote.write_json(runtime / 'boot_autostart.json', dict(boot_id=self.boot, reason='auto_start'))
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])
        self.rpc('start', program='drop')
        self.rpc('start', program='drop')
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(Path(self.calls[0][3]).name, 'drop')

    def test_drop_failure_is_isolated_and_is_not_retried(self):
        result = self.rpc('auto_start', commands={'drop': ''})
        self.assertEqual(result['drop']['state'], 'error')
        self.assertEqual({Path(call[3]).name for call in self.calls}, {'onboard', 'detection', 'detection_person', 'flight'})
        for key in ('onboard', 'detection', 'detection_person', 'flight'):
            self.assertNotEqual(result[key]['state'], 'error')
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES) - 1)

    def test_explicit_drop_start_uses_custom_config_and_is_idempotent(self):
        command = remote.drop_command().replace('/dev/ttyUSB0', '/dev/ttyUSB1')
        self.rpc('start', program='drop', commands={'drop': command})
        self.rpc('start', program='drop', commands={'drop': command})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][-1], command)

    def test_manual_drop_process_is_reused_without_starting_any_program(self):
        self.live[900] = 'servo-stamp'
        self.borrowed['drop'] = dict(pid=900, stamp='servo-stamp', boot_id=self.boot, borrowed=True)
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_reason'], 'existing_session')
        self.rpc('auto_start')
        self.rpc('start', program='drop')
        self.assertEqual(self.calls, [])

    def test_operator_stop_does_not_auto_restart_drop(self):
        self.rpc('auto_start')
        with patch.object(remote, 'stop_program', return_value=dict(state='stopped', stop_verified=True)):
            self.rpc('stop', program='drop')
        self.live.clear()
        self.rpc('status')
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))
        self.rpc('start', program='drop')
        self.assertEqual(len(self.calls), len(remote.NAMES) + 1)
        self.assertEqual(Path(self.calls[-1][3]).name, 'drop')


class DropIntegrationTests(unittest.TestCase):
    def test_launcher_without_uav_id_matches_only_named_workspace(self):
        command = ['roslaunch', '/home/amov/paotou_ws/src/servo/launch_servo/servo.launch', 'port:=/dev/ttyUSB0']
        def item(pid, args):
            return dict(pid=pid, stamp='s', parent=0, args=args)
        table = {10: item(10, command), 11: item(11, command),
                 12: item(12, ['bash', '-lc', ' '.join(command)]),
                 13: item(13, ['roslaunch', '/other/servo.launch']),
                 14: item(14, ['python3', 'runner.py', '--worker', '/tmp/drop', ' '.join(command)])}
        for uid in range(1, 7):
            self.assertEqual(set(remote.matching_launchers(table, 'drop', uid)), {10, 11})

    def test_drop_stop_kills_only_its_launchers_and_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            launch = ['roslaunch', '/home/amov/paotou_ws/src/servo/launch_servo/servo.launch', 'port:=/dev/ttyUSB0']
            table = {100: dict(pid=100, stamp='a', parent=0, args=launch),
                     101: dict(pid=101, stamp='b', parent=100, args=['servo_serial_node']),
                     200: dict(pid=200, stamp='c', parent=0, args=['roslaunch', '/other/servo.launch']),
                     300: dict(pid=300, stamp='d', parent=0, args=['rosout'])}
            live = set(table)
            def kill(pid, _sig):
                live.discard(pid)
            with patch.object(remote, 'process_table', side_effect=lambda: {pid:v for pid,v in table.items() if pid in live}), \
                    patch.object(remote, 'alive', side_effect=lambda meta: meta.get('pid') in live), \
                    patch.object(remote, 'ros_health', return_value={'drop': dict(present=[], pids={})}), \
                    patch.object(remote.os, 'kill', side_effect=kill), \
                    patch.object(signal, 'SIGKILL', 9, create=True):
                result = remote.stop_program(directory, 'drop', 4)
            self.assertTrue(result['stop_verified'])
            self.assertEqual(result['checked_pids'], [100, 101])
            self.assertEqual(live, {200, 300})

    def test_health_requires_serial_node_response(self):
        class RosServer:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def lookupNode(self, caller, name):
                return (1, '', 'http://servo.test') if name == '/servo_serial_node' else (-1, '', '')
            def getPid(self, caller): return (1, '', 1234)
        with patch('xmlrpc.client.ServerProxy', side_effect=lambda *a, **k: RosServer()):
            health = remote.ros_health(4)
        self.assertTrue(health['drop']['ready'])
        self.assertEqual(health['drop']['pids'], {'/servo_serial_node': 1234})
        self.assertFalse(health['flight']['ready'])
        self.assertEqual(remote.apply_health(dict(state='stopped'), health['drop'], {})['state'], 'running')
        missing = dict(ready=False, present=[], missing=['/servo_serial_node'])
        self.assertEqual(remote.apply_health(dict(state='running'), missing, {'started_at': 1})['state'], 'error')

    def test_status_start_stop_and_chinese_log_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            fleet = Path(tmp) / 'fleet.json'
            fleet.write_text(json.dumps(default_fleet()), encoding='utf-8')
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=4, model='p600', onboard_host='192.168.1.217')
            manager._append('drop', '抛投程序打印测试\n')
            manager.start_program = Mock(return_value=dict(state='starting', detail='等待节点'))
            manager.stop_program = Mock(return_value=dict(state='stopped', stop_verified=True))
            entry = GroundEntry({'COMPETITION_FLEET_CONFIG': str(fleet)}, programs_factory=lambda *a: manager)
            with TestClient(entry) as client:
                self.assertIn('drop', client.get('/api/v1/programs').json()['programs'])
                self.assertEqual(client.post('/api/v1/programs/drop/start', headers={'Origin':'https://other.example'}).status_code, 403)
                manager.start_program.assert_not_called()
                self.assertEqual(client.post('/api/v1/programs/drop/start').status_code, 200)
                manager.start_program.assert_called_once_with('drop')
                self.assertTrue(client.post('/api/v1/programs/drop/stop').json()['stop_verified'])
                manager.stop_program.assert_called_once_with('drop')
                log = client.get('/api/v1/programs/drop/logs').json()['text']
                self.assertIn('抛投程序打印测试', log)
                self.assertIn('地面记录', log)
                self.assertTrue((Path(tmp) / 'ground_logs/programs/uav4_drop.log').is_file())

    def test_ssh_start_forwards_drop_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tools').mkdir()
            (root / 'tools/remote_programs.py').write_text('import json\ndef rpc(p): print(json.dumps(p))\n', encoding='utf-8')
            (root / 'config').mkdir()
            config = root / 'config/onboard_programs.json'
            manager = ProgramManager(root, {})
            manager.local = dict(uav_id=4, model='p600', onboard_host='192.168.1.217')
            def run(_command, **kwargs):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(kwargs['input'].decode('ascii'), {'__name__':'drop_config_test'})
                return SimpleNamespace(returncode=0, stdout=output.getvalue().encode(), stderr=b'')
            with patch('competition_backend.program_manager.ssh_identity_args', return_value=[]), \
                    patch('competition_backend.program_manager.subprocess.run', side_effect=run):
                for port in ('ttyUSB0', 'ttyUSB1'):
                    command = remote.drop_command().replace('ttyUSB0', port)
                    config.write_text(json.dumps({'commands': {'drop': command}}), encoding='utf-8')
                    payload = manager._request('start', {}, program='drop')
                    self.assertEqual(payload['program'], 'drop')
                    self.assertEqual(payload['commands']['drop'], command)


if __name__ == '__main__':
    unittest.main()
