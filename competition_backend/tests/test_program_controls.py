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


class RemoteSingleStartTests(unittest.TestCase):
    setUp = boot_tests.RemoteBootTests.setUp
    spawn = boot_tests.RemoteBootTests.spawn
    rpc = boot_tests.RemoteBootTests.rpc
    def test_selected_start_does_not_start_other_two_and_does_not_duplicate(self):
        self.rpc('start', program='detection', commands={'detection': "echo '独立启动'; sleep 20"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(Path(self.calls[0][3]).name, 'detection')
        self.assertIn('独立启动', self.calls[0][-1])
        self.rpc('start', program='detection')
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 1)


class SingleProgramControlsTests(unittest.TestCase):
    def test_start_api_local_only_and_no_ground_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'fleet.json'
            config.write_text(json.dumps(default_fleet()), encoding='utf-8')
            manager = ProgramManager(tmp, {})
            manager.start_program = Mock(return_value={'state': 'starting', 'detail': '正在启动'})
            entry = GroundEntry({'COMPETITION_FLEET_CONFIG': str(config)}, programs_factory=lambda *args: manager)
            with TestClient(entry) as client:
                self.assertEqual(client.post('/api/v1/programs/detection/start', headers={'Origin': 'https://evil.example'}).status_code, 403)
                manager.start_program.assert_not_called()
                self.assertEqual(client.post('/api/v1/programs/detection/start').status_code, 200)
                manager.start_program.assert_called_once_with('detection')
                manager.start_program.side_effect = RuntimeError('连接中断')
                self.assertEqual(client.post('/api/v1/programs/detection/start').status_code, 409)
            with self.assertRaises(ValueError):
                ProgramManager(tmp, {}).start_program('ground')

    def test_start_only_clears_selected_stop_and_reads_config_each_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600', onboard_host='192.168.1.202')
            manager.operator_stopped = {'flight', 'detection', 'onboard'}
            manager._request = Mock(return_value={'detection': dict(state='starting', detail='等待节点', log='a')})
            self.assertEqual(manager.start_program('detection')['state'], 'starting')
            self.assertEqual(manager.operator_stopped, {'flight', 'onboard'})
            self.assertFalse(manager.restart_request.is_set())
            manager._request.assert_called_once_with('start', {}, program='detection')
            manager._request.return_value = {'detection': dict(state='error', detail='指令失败')}
            with self.assertRaisesRegex(RuntimeError, '启动未确认'):
                manager.start_program('detection')
            self.assertFalse(manager.restart_request.is_set())

    def test_busy_operation_cannot_queue_hidden_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600')
            with manager.operation_lock:
                with self.assertRaisesRegex(RuntimeError, '另一程序操作'):
                    manager.start_program('flight')
            self.assertFalse(manager.restart_request.is_set())

    def test_match_all_launchers_ignores_management_shell_and_other_uav(self):
        def item(pid, args):
            return dict(pid=pid, stamp='s', parent=0, args=args)
        command = 'roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=1'
        table = {10: item(10, command.split()), 11: item(11, command.split()),
                 12: item(12, ['bash', '-c', command]), 13: item(13, command.replace(':=1', ':=2').split())}
        self.assertEqual(set(remote.matching_launchers(table, 'detection', 1)), {10, 11})

    def test_stop_finds_duplicate_launchers_and_orphan_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            args = ['roslaunch', 'spirecv_ros', 'uav_yolo26_botsort_geolocation.launch', 'uav_id:=1']
            table = {100: dict(pid=100, stamp='a', parent=0, args=args),
                     101: dict(pid=101, stamp='b', parent=100, args=['node']),
                     200: dict(pid=200, stamp='c', parent=0, args=args),
                     201: dict(pid=201, stamp='d', parent=0, args=['python3', str(directory / 'runner.py'), '--worker', str(directory)]),
                     300: dict(pid=300, stamp='e', parent=0, args=['rosout'])}
            live = set(table)
            killed = []
            def kill(pid, sig):
                killed.append(pid)
                live.discard(pid)
            with patch.object(remote, 'process_table', side_effect=lambda: {pid: v for pid, v in table.items() if pid in live}), patch.object(remote, 'alive', side_effect=lambda item: item.get('pid') in live), patch.object(remote, 'ros_health', return_value={'detection': dict(present=[], pids={})}), patch.object(remote.os, 'kill', side_effect=kill), patch.object(signal, 'SIGKILL', 9, create=True):
                result = remote.stop_program(directory, 'detection', 1)
            self.assertTrue(result['stop_verified'])
            self.assertEqual(set(killed), {100, 101, 200, 201})
            self.assertEqual(live, {300})

    def test_unreachable_ros_cannot_be_reported_as_verified_stop(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(remote, 'process_table', return_value={}), patch.object(remote, 'existing', return_value={}), patch.object(remote, 'ros_health', return_value={'flight': dict(present=[], pids={}, errors={'master': 'timeout'})}), patch.object(signal, 'SIGKILL', 9, create=True):
            self.assertFalse(remote.stop_program(Path(tmp), 'flight', 1)['stop_verified'])

    def test_stale_registration_requires_same_uri_and_verified_exited_pid(self):
        name = '/uav1/trajectory_follower'
        initial = dict(pids={name: 71}, addresses={name: 'http://amov:111/'})
        final = dict(refused=[name], addresses={name: 'http://amov:111/'})
        targets = {71: dict(pid=71, stamp='original')}
        with patch.object(remote, 'alive', return_value=False):
            self.assertEqual(remote.stale_stopped_nodes(initial, final, targets), [name])
            self.assertEqual(remote.stale_stopped_nodes(initial, final, {}), [])
            self.assertEqual(remote.stale_stopped_nodes(initial, dict(final, addresses={name:'http://amov:222/'}), targets), [])
            self.assertEqual(remote.stale_stopped_nodes(initial, dict(final, refused=[]), targets), [])
        with patch.object(remote, 'alive', return_value=True):
            self.assertEqual(remote.stale_stopped_nodes(initial, final, targets), [])


if __name__ == '__main__':
    unittest.main()
