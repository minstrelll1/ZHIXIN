"""开机自动启动与断线重连隔离；全部使用虚拟进程及 SSH，不连接无人机。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from competition_backend.program_manager import ProgramManager
from test_program_manager import remote


class RemoteBootTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = self.home / 'competition_development'
        (self.root / 'tools').mkdir(parents=True)
        (self.root / 'tools/start_onboard_stack.sh').touch()
        self.boot = 'boot-a'
        self.live = {}
        self.borrowed = {}
        self.calls = []
        self.interrupt_at = None
        self.health = {key: dict(ready=False, present=[], missing=[], pids={}) for key in remote.NAMES}
        patches = [
            patch.dict('sys.modules', {'fcntl': SimpleNamespace(flock=lambda *args: None, LOCK_EX=2)}),
            patch.object(remote.Path, 'home', return_value=self.home),
            patch.object(remote, 'PROGRAM_SOURCE', '# isolated test', create=True),
            patch.object(remote, 'boot_id', side_effect=lambda: self.boot),
            patch.object(remote, 'stamp', side_effect=lambda pid: self.live.get(pid, '')),
            patch.object(remote, 'existing', side_effect=lambda key, uid: self.borrowed.get(key, {})),
            patch.object(remote, 'ros_health', side_effect=lambda uid: self.health),
            patch.object(remote, 'flight_allows_autostart', return_value=True),
            patch.object(remote.subprocess, 'Popen', side_effect=self.spawn),
        ]
        for item in patches:
            self.stack.enter_context(item)

    def spawn(self, argv, **kwargs):
        if self.interrupt_at == len(self.calls) + 1:
            raise KeyboardInterrupt('模拟 SSH 管理进程在部分启动后被中断')
        self.calls.append(argv)
        pid = 100 + len(self.calls)
        self.live[pid] = self.live[pid + 1000] = 'stamp'
        remote.write_json(Path(argv[3]) / 'process.json', dict(
            pid=pid, stamp='stamp', boot_id=self.boot, started_at=time.time()))
        return SimpleNamespace(pid=pid + 1000, poll=lambda: None)

    def rpc(self, action='status', **kwargs):
        payload = dict(action=action, uav_id=2, model='p600', expected_boot_id=self.boot, **kwargs)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            remote.rpc(payload)
        return json.loads(output.getvalue())

    def test_clock_probe_never_creates_directories_or_writes_program_state(self):
        with patch.object(remote.Path,'mkdir') as mkdir, \
                patch.object(remote,'write_json') as write, \
                patch.object(remote.subprocess,'run') as run:
            result=self.rpc('clock_probe')
        self.assertEqual(result['boot_id'],self.boot)
        self.assertIsInstance(result['remote_monotonic'],float)
        self.assertIsInstance(result['remote_wall'],float)
        mkdir.assert_not_called();write.assert_not_called();run.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_start_and_reconnect_never_adjust_system_clock(self):
        with patch.object(remote.subprocess, 'run') as clock:
            self.rpc('clock_probe')
            first = self.rpc('auto_start', clock_sync=dict(state='ready'))
            self.assertNotIn('_clock_sync', first)
            self.live.clear()
            self.rpc('status'); self.rpc('auto_start'); self.rpc('start')
            self.boot = 'boot-b'
            self.rpc('auto_start', clock_sync=dict(state='ready'))
            clock.assert_not_called()

    def test_existing_session_never_attempts_clock_sync(self):
        self.health['flight']['present']=['/uav2/trajectory_follower']
        with patch.object(remote.subprocess,'run') as clock:
            self.rpc('status'); self.rpc('auto_start'); self.rpc('start')
            clock.assert_not_called()

    def test_cold_boot_starts_all_once_even_when_reply_lost(self):
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES))
        self.rpc('auto_start')
        self.rpc('auto_start')  # 自动启动响应丢失后的重试也不得再次运行启动命令。
        self.assertEqual(len(self.calls), len(remote.NAMES))
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_same_boot_crash_or_ground_restart_never_auto_relaunches(self):
        self.rpc('auto_start')
        self.live.clear()
        for _ in range(3):
            self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
            self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))
        self.rpc('start')  # 仅操作员主动启动可以再试。
        self.assertEqual(len(self.calls), 2 * len(remote.NAMES))

    def test_reboot_allows_new_batch_even_if_pid_and_stamp_repeat(self):
        self.rpc('auto_start')
        self.boot = 'boot-b'
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES))
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 2 * len(remote.NAMES))
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_partial_start_interruption_resumes_only_missing_programs_on_reconnect(self):
        self.interrupt_at = 2
        with self.assertRaises(KeyboardInterrupt):
            self.rpc('auto_start')
        self.interrupt_at = None
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES)[1:])
        self.rpc('auto_start')
        self.assertEqual([Path(argv[3]).name for argv in self.calls], list(remote.NAMES))
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_interruption_before_first_worker_and_repeated_missing_replies(self):
        self.interrupt_at = 1
        for _ in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.rpc('auto_start')
            self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES))
        self.interrupt_at = None
        self.rpc('auto_start')
        for _ in range(3):
            self.rpc('status'); self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))

    def test_partial_batch_waits_until_flight_state_is_confirmed_disarmed(self):
        self.interrupt_at = 2
        with self.assertRaises(KeyboardInterrupt):
            self.rpc('auto_start')
        self.interrupt_at = None
        with patch.object(remote, 'flight_allows_autostart', return_value=False):
            state = self.rpc('auto_start')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state['_lifecycle']['auto_start_pending'], list(remote.NAMES)[1:])
        self.assertEqual(state['_lifecycle']['auto_start_waiting_for_disarm'], list(remote.NAMES)[1:])
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))

    def test_manual_stop_during_partial_batch_remains_stopped(self):
        self.interrupt_at = 2
        with self.assertRaises(KeyboardInterrupt):
            self.rpc('auto_start')
        self.interrupt_at = None
        with patch.object(remote, 'stop_program', return_value=dict(state='stopped')):
            self.rpc('stop', program='flight')
        self.rpc('auto_start')
        self.assertEqual([Path(argv[3]).name for argv in self.calls], [key for key in remote.NAMES if key != 'flight'])
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_legacy_batch_marker_does_not_resume_untracked_programs(self):
        self.rpc()
        runtime = self.root / 'ground_runtime/programs_uav2'
        remote.write_json(runtime / 'boot_autostart.json', dict(boot_id=self.boot, reason='auto_start'))
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_connection_lost_after_spawn_before_worker_receipt_cannot_duplicate_child(self):
        directory = self.root / 'ground_runtime/programs_uav2/onboard'
        original_write = remote.write_json
        attempts = []
        def spawn_worker(argv, **kwargs):
            attempts.append(argv)
            pid = 700 + len(attempts)
            self.live[pid] = 'worker-stamp'
            return SimpleNamespace(pid=pid)
        def lose_receipt(path, value):
            if path.name == 'worker.json':
                raise KeyboardInterrupt('worker 已创建，SSH 在保存回执前丢失')
            original_write(path, value)
        with patch.object(remote.subprocess, 'Popen', side_effect=spawn_worker):
            with patch.object(remote, 'write_json', side_effect=lose_receipt):
                with self.assertRaises(KeyboardInterrupt):
                    self.rpc('auto_start', program='onboard')
            self.rpc('auto_start', program='onboard')
        self.assertEqual(len(attempts), 2)
        old_id, new_id = attempts[0][-2], attempts[1][-2]
        self.assertNotEqual(old_id, new_id)
        self.live[801] = 'child-stamp'
        child = SimpleNamespace(pid=801, wait=lambda: 0)
        with patch.object(remote.subprocess, 'Popen', return_value=child) as launch:
            remote.worker(directory, 'echo synthetic', old_id)
            launch.assert_not_called()
            remote.worker(directory, 'echo synthetic', new_id)
            remote.worker(directory, 'echo synthetic', old_id)
            remote.worker(directory, 'echo synthetic', new_id)
            launch.assert_called_once()
        self.assertEqual(remote.read_json(directory / 'process.json')['exit_code'], 0)
        self.assertNotIn('onboard', self.rpc()['_lifecycle']['auto_start_pending'])

    def test_worker_checks_flight_again_and_can_resume_after_state_recovers(self):
        directory = self.root / 'ground_runtime/programs_uav2/onboard'
        self.rpc()  # 仅创建目录。
        marker = dict(boot_id=self.boot, reason='launch_pending', launch_id='test-token', automatic=True, uav_id=2)
        remote.write_json(directory / 'autostart.json', marker)
        self.live[901] = 'child-stamp'
        with patch.object(remote.subprocess, 'Popen', return_value=SimpleNamespace(pid=901, wait=lambda: 0)) as launch:
            with patch.object(remote, 'flight_allows_autostart', return_value=False):
                remote.worker(directory, 'echo synthetic', 'test-token')
            launch.assert_not_called()
            self.assertEqual(remote.read_json(directory / 'autostart.json')['reason'], 'launch_pending')
            remote.worker(directory, 'echo synthetic', 'test-token')
            launch.assert_called_once()

    def test_manual_stop_invalidates_a_worker_that_has_not_started_its_child(self):
        self.rpc()
        directory = self.root / 'ground_runtime/programs_uav2/onboard'
        remote.write_json(directory / 'autostart.json', dict(boot_id=self.boot, reason='operator_stopped'))
        with patch.object(remote.subprocess, 'Popen') as launch:
            remote.worker(directory, 'echo synthetic', 'old-token')
            launch.assert_not_called()

    def test_full_watch_recovers_base_connection_and_partial_batch_after_ssh_loss(self):
        manager = ProgramManager(self.home / 'ground', {})
        manager.local = dict(uav_id=2, model='p600', onboard_host='192.168.1.207')
        requests = []
        def request(action, offsets, program=None):
            requests.append(action)
            if len(requests) == 1:
                raise TimeoutError('无人机尚未开机')
            if requests.count('auto_start') == 1 and action == 'auto_start':
                self.interrupt_at = 2
                try:
                    self.rpc(action)
                except KeyboardInterrupt:
                    self.interrupt_at = None
                    raise RuntimeError('Timeout, server 192.168.1.207 not responding.')
            result = self.rpc(action)
            if len(self.calls) == len(remote.NAMES) and not result['_lifecycle']['auto_start_pending']:
                manager.stop_event.set()
            if len(requests) > 12:
                manager.stop_event.set()
                self.fail('全流程未在网络恢复后完成')
            return result
        manager._request = request
        def connection(**kwargs):
            ready = len(requests) >= 2
            return dict(ready=ready, auto_start_allowed=ready, state='starting', detail='等待厂商基础连接')
        with patch.object(manager, '_prepare_connection', side_effect=connection), \
                patch.object(manager.stop_event, 'wait', return_value=False):
            manager._watch()
        self.assertEqual(requests, ['status', 'status', 'status', 'auto_start', 'status', 'auto_start'])
        self.assertEqual([Path(argv[3]).name for argv in self.calls], list(remote.NAMES))
        self.assertEqual(manager.auto_blocked_boot, '')

    def test_first_contact_with_existing_flight_session_only_monitors(self):
        self.live[999] = 'flight-stamp'
        self.borrowed['flight'] = dict(pid=999, stamp='flight-stamp', boot_id=self.boot, borrowed=True)
        lifecycle = self.rpc()['_lifecycle']
        self.assertEqual(lifecycle['auto_start_reason'], 'existing_session')
        self.assertEqual(lifecycle['auto_start_pending'], [])
        self.live.clear()
        self.borrowed.clear()
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])

    def test_ros_nodes_alone_also_prevent_cold_start_assumption(self):
        self.health['flight']['present'] = ['/uav2/trajectory_follower']
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])

    def test_wrong_or_unavailable_boot_identity_cannot_launch(self):
        with self.assertRaisesRegex(RuntimeError, '开机标识'):
            with contextlib.redirect_stdout(io.StringIO()):
                remote.rpc(dict(action='auto_start', uav_id=2, model='p600', expected_boot_id='old-boot'))
        self.boot = ''
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
        with self.assertRaisesRegex(RuntimeError, '开机标识'):
            self.rpc('auto_start')
        self.assertEqual(self.calls, [])

    def test_boot_claim_must_be_saved_before_any_spawn(self):
        original = remote.write_json
        def fail_claim(path, value):
            if path.name == 'boot_autostart.json':
                raise OSError('只读磁盘')
            original(path, value)
        with patch.object(remote, 'write_json', side_effect=fail_claim):
            with self.assertRaises(OSError):
                self.rpc('auto_start')
        self.assertEqual(self.calls, [])

    def test_failed_start_does_not_block_other_programs_or_repeat(self):
        self.rpc('auto_start', commands={'detection': ''})
        self.assertEqual(len(self.calls), len(remote.NAMES) - 1)
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES) - 1)

    def test_explicit_stop_intent_survives_failure_and_new_ground_process(self):
        with patch.object(remote, 'stop_program', side_effect=RuntimeError('停止回执丢失')):
            self.rpc('stop', program='flight')
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])
        self.boot = 'boot-b'
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), len(remote.NAMES))


    def test_five_configured_programs_survive_ground_reopen_and_manual_restart_uses_new_command(self):
        commands = {key: 'echo first_' + key + ' {uav_id} {model}' for key in remote.NAMES}
        with patch.object(remote.subprocess, 'Popen', side_effect=self.spawn) as launch:
            self.rpc('auto_start', commands=commands)
            self.assertEqual(len(self.calls), len(remote.NAMES))
            for call in launch.call_args_list:
                argv = call.args[0]
                key = Path(argv[3]).name
                self.assertEqual(argv[-1], 'echo first_%s 2 p600' % key)
                self.assertTrue(call.kwargs['start_new_session'])
                self.assertTrue(call.kwargs['close_fds'])
                self.assertEqual(call.kwargs['stdin'], remote.subprocess.DEVNULL)
                self.assertIsNotNone(call.kwargs['stdout'])
                self.assertEqual(call.kwargs['stderr'], call.kwargs['stdout'])
        running = dict(self.live)
        ground = ProgramManager(self.home / 'ground', {})
        with patch.object(ground, '_request') as request:
            ground.stop()
            self.assertTrue(ground.stop_event.is_set())
            request.assert_not_called()
        self.assertEqual(self.live, running)
        updated = {key: 'echo next_' + key + ' {uav_id}' for key in remote.NAMES}
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
        self.rpc('auto_start', commands=updated)
        self.assertEqual(len(self.calls), len(remote.NAMES))
        def stop(directory, key, uid):
            for name in ('process.json', 'worker.json'):
                meta = remote.read_json(directory / name)
                self.live.pop(meta.get('pid'), None)
                remote.write_json(directory / name, {})
            return dict(state='stopped', stop_verified=True)
        for key in remote.NAMES:
            count = len(self.calls)
            self.rpc('start', program=key, commands=updated)
            self.assertEqual(len(self.calls), count)  # 已运行时不能应用配置或重复启动
            with patch.object(remote, 'stop_program', side_effect=stop):
                self.assertTrue(self.rpc('stop', program=key)[key]['stop_verified'])
            self.rpc('status')
            self.rpc('auto_start', commands=updated)
            self.assertEqual(len(self.calls), count)
            self.rpc('start', program=key, commands=updated)
            self.assertEqual(len(self.calls), count + 1)
            self.assertEqual(Path(self.calls[-1][3]).name, key)
            self.assertEqual(self.calls[-1][-1], 'echo next_%s 2' % key)
            self.assertEqual(len(self.live), 2 * len(remote.NAMES))  # 其他程序及 worker 仍保持运行


def snapshot(boot='boot-a', pending=False):
    result = {key: dict(state='running') for key in remote.NAMES}
    result['_lifecycle'] = dict(boot_id=boot, auto_start_pending=list(remote.NAMES) if pending else [])
    return result


class GroundBootTests(unittest.TestCase):
    def watch(self, responses):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=2, model='p600', onboard_host='192.168.1.207')
            calls = []
            def request(action, offsets, program=None):
                calls.append(action)
                item = responses[len(calls) - 1]
                if len(calls) == len(responses):
                    manager.stop_event.set()
                if isinstance(item, Exception):
                    raise item
                return item
            manager._request = request
            with patch.object(manager.stop_event, 'wait', return_value=False):
                manager._watch()
            return calls, manager.snapshot()

    def test_ground_first_then_drone_boot_link_recovery_and_real_reboot(self):
        calls, result = self.watch([
            TimeoutError('连接超时'), snapshot(pending=True), snapshot(),
            TimeoutError('连接超时'), snapshot(), snapshot('boot-b', True), snapshot('boot-b')])
        self.assertEqual(calls, ['status', 'status', 'auto_start', 'status', 'status', 'status', 'auto_start'])
        self.assertEqual(result['boot_id'], 'boot-b')
        self.assertEqual(result['programs']['flight']['state'], 'running')

    def test_lost_start_reply_reads_persisted_claim_instead_of_restarting(self):
        calls, _ = self.watch([snapshot(pending=True), TimeoutError('SSH 响应丢失'), snapshot(), snapshot()])
        self.assertEqual(calls, ['status', 'auto_start', 'status', 'status'])

    def test_openssh_server_timeout_does_not_block_the_remaining_boot_batch(self):
        calls, result = self.watch([snapshot(pending=True),
            RuntimeError('SSH 连接或机载程序管理失败：Timeout, server 192.168.1.202 not responding.'),
            snapshot(pending=True), snapshot()])
        self.assertEqual(calls, ['status', 'auto_start', 'status', 'auto_start'])
        self.assertEqual(result['programs']['flight']['state'], 'running')

    def test_new_ground_instance_only_monitors_existing_boot(self):
        calls, _ = self.watch([snapshot(), snapshot()])
        self.assertEqual(calls, ['status', 'status'])

    def test_boot_identity_unavailable_keeps_monitoring_without_start(self):
        calls, _ = self.watch([snapshot('', True), snapshot('', True)])
        self.assertEqual(calls, ['status', 'status'])

    def test_bad_config_does_not_end_monitoring_or_loop_start_requests(self):
        calls, _ = self.watch([snapshot(pending=True), RuntimeError('启动配置格式错误'), snapshot(pending=True)])
        self.assertEqual(calls, ['status', 'auto_start', 'status'])


class FlightGuardTests(unittest.TestCase):
    def test_only_fresh_connected_disarmed_sample_allows_autostart(self):
        for state, expected in (({}, False), (dict(connected=True), False),
                                (dict(connected=True, armed=True), False),
                                (dict(connected=False, armed=False), False),
                                (dict(connected=True, armed=False), True)):
            with self.subTest(state=state), patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(state).encode())) as run:
                self.assertEqual(remote.flight_allows_autostart(2), expected)
                self.assertEqual(run.call_args.kwargs['timeout'], 4)
        with patch.object(remote.subprocess, 'run', side_effect=remote.subprocess.TimeoutExpired('probe', 4)):
            self.assertFalse(remote.flight_allows_autostart(2))


if __name__ == '__main__':
    unittest.main()
