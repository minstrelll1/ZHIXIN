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

    def test_cold_boot_starts_three_once_even_when_reply_lost(self):
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES))
        self.rpc('auto_start')
        self.rpc('auto_start')  # 自动启动响应丢失后的重试也不得再次运行启动命令。
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_same_boot_crash_or_ground_restart_never_auto_relaunches(self):
        self.rpc('auto_start')
        self.live.clear()
        for _ in range(3):
            self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
            self.rpc('auto_start')
        self.assertEqual(len(self.calls), 3)
        self.rpc('start')  # 仅操作员主动启动可以再试。
        self.assertEqual(len(self.calls), 6)

    def test_reboot_allows_new_batch_even_if_pid_and_stamp_repeat(self):
        self.rpc('auto_start')
        self.boot = 'boot-b'
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], list(remote.NAMES))
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

    def test_partial_start_interruption_does_not_fill_missing_programs_on_reconnect(self):
        self.interrupt_at = 2
        with self.assertRaises(KeyboardInterrupt):
            self.rpc('auto_start')
        self.interrupt_at = None
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])

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
        self.assertEqual(len(self.calls), 2)
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 2)

    def test_explicit_stop_intent_survives_failure_and_new_ground_process(self):
        with patch.object(remote, 'stop_program', side_effect=RuntimeError('停止回执丢失')):
            self.rpc('stop', program='flight')
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])
        self.boot = 'boot-b'
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 3)


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

    def test_new_ground_instance_only_monitors_existing_boot(self):
        calls, _ = self.watch([snapshot(), snapshot()])
        self.assertEqual(calls, ['status', 'status'])

    def test_boot_identity_unavailable_keeps_monitoring_without_start(self):
        calls, _ = self.watch([snapshot('', True), snapshot('', True)])
        self.assertEqual(calls, ['status', 'status'])

    def test_bad_config_does_not_end_monitoring_or_loop_start_requests(self):
        calls, _ = self.watch([snapshot(pending=True), RuntimeError('启动配置格式错误'), snapshot(pending=True)])
        self.assertEqual(calls, ['status', 'auto_start', 'status'])


if __name__ == '__main__':
    unittest.main()
