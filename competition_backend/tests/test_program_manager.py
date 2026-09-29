import importlib.util
import contextlib
import io
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.ground_entry import GroundEntry
from competition_backend.program_manager import ProgramManager
from competition_shared.fleet import default_fleet

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('remote_programs', ROOT / 'tools/remote_programs.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class ProgramsTest(unittest.TestCase):
    def test_assets_and_program_status_available_before_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'fleet.json'
            config.write_text(json.dumps(default_fleet()), encoding='utf-8')
            manager = ProgramManager(tmp, {})
            entry = GroundEntry({'COMPETITION_FLEET_CONFIG': str(config)}, programs_factory=lambda *args: manager)
            with TestClient(entry) as client:
                self.assertEqual(client.get('/map-assets/terrain_basemap.js').status_code, 200)
                image = client.get('/map-assets/competition_esri_20170724.jpg')
                self.assertEqual(image.status_code, 200)
                self.assertTrue(image.content.startswith(b'\xff\xd8'))
                self.assertEqual(client.get('/map-assets/private.json').status_code, 404)
                states = client.get('/api/v1/programs').json()['programs']
                self.assertEqual(states['ground']['state'], 'running')
                self.assertEqual(states['flight']['state'], 'idle')
                self.assertEqual(client.get('/api/v1/programs/wrong/logs').status_code, 404)
                self.assertIsNone(manager.thread)
                self.assertEqual(client.post('/api/v1/programs/reconnect', headers={'Origin': 'https://unrelated.example'}).status_code, 403)
                self.assertEqual(client.post('/api/v1/programs/reconnect').status_code, 409)
                # 选终端后依旧由入口提供资源与日志，不受运行时路由切换影响。
                entry.active = Mock()
                self.assertEqual(client.get('/api/v1/programs').status_code, 200)
                self.assertEqual(client.get('/map-assets/terrain_basemap.js').status_code, 200)
                entry.active.assert_not_called()

    def test_log_paging_preserves_chinese_and_hides_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {'AUTH_TOKEN': 'secret-value'})
            original = '中文打印\n' * 17000 + 'AUTH_TOKEN: secret-value\n'
            manager._append('ground', original)
            offset, chunks = 0, []
            while True:
                page = manager.read_log('ground', offset)
                chunks.append(page['text'])
                offset = page['offset']
                if offset >= page['size']:
                    break
            full = ''.join(chunks)
            self.assertNotIn('secret-value', full)
            self.assertNotIn('\ufffd', full)
            self.assertEqual(full.count('中文打印'), 17000)

    def test_select_returns_immediately_deduplicates_and_failure_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            gate = threading.Event()
            def request(*args):
                gate.wait(2)
                raise RuntimeError('SSH 密钥验证失败')
            manager._request = Mock(side_effect=request)
            manager.select(dict(uav_id=3, model='p600', onboard_host='192.168.1.212'))
            thread = manager.thread
            manager.select(dict(uav_id=3, model='p600', onboard_host='192.168.1.212'))
            self.assertIs(thread, manager.thread)
            self.assertEqual(manager.snapshot()['programs']['ground']['state'], 'running')
            gate.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(manager._request.call_count, 1)
            for key in ('onboard', 'detection', 'flight'):
                self.assertEqual(manager.snapshot()['programs'][key]['state'], 'error')
                self.assertIn('密钥验证失败', manager.read_log(key)['text'])
            manager.stop()

    def test_one_program_failure_does_not_hide_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=3, model='p600')
            def request(*args):
                manager.stop_event.set()
                return {'onboard': dict(state='running'), 'detection': dict(state='error', detail='环境缺失'),
                        'flight': dict(state='running')}
            manager._request = request
            manager._watch()
            state = manager.snapshot()['programs']
            self.assertEqual(state['onboard']['state'], 'running')
            self.assertEqual(state['detection']['state'], 'error')
            self.assertEqual(state['flight']['state'], 'running')

    def test_permission_denied_authorizes_once_then_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600', onboard_host='192.168.1.202')
            calls = []
            def request(action, offsets):
                calls.append(action)
                if len(calls) == 1:
                    raise RuntimeError('Permission denied (publickey,password).')
                manager.stop_event.set()
                return {key: dict(state='running') for key in ('onboard', 'detection', 'flight')}
            manager._request = request
            manager._authorize = Mock(return_value=True)
            manager._watch()
            self.assertEqual(calls, ['start', 'start'])
            manager._authorize.assert_called_once()
            self.assertEqual(manager.snapshot()['programs']['flight']['state'], 'running')

    def test_authorization_cancel_does_not_repeat_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600', onboard_host='192.168.1.202')
            manager._request = Mock(side_effect=RuntimeError('Permission denied (publickey,password)'))
            manager._authorize = Mock(side_effect=RuntimeError('用户关闭授权窗口'))
            manager._watch()
            manager._authorize.assert_called_once()
            self.assertEqual(manager.snapshot()['authorization']['state'], 'error')
            self.assertEqual(manager.snapshot()['programs']['ground']['state'], 'running')

    def test_healthy_launcher_with_dead_nodes_is_not_green(self):
        state = remote.apply_health(dict(state='running'), dict(ready=False, present=['a'], missing=['b']), {'started_at': 1})
        self.assertEqual(state['state'], 'error')
        self.assertIn('b', state['detail'])
        state = remote.apply_health(dict(state='stopped'), dict(ready=True, present=['a'], missing=[]), {})
        self.assertEqual(state['state'], 'running')

    def test_remote_command_uses_selected_uav_and_does_not_arm(self):
        self.assertIn('uav_id:=3', remote.command_for('detection', 3, 'p600'))
        self.assertIn('uav_id:=3 flight_mode:=outdoor_small_range', remote.command_for('flight', 3, 'p600'))
        self.assertIn('--expect-uav-id 3 --direct --enable-motion', remote.command_for('onboard', 3, 'p600'))
        for key in remote.NAMES:
            self.assertNotIn('arming', remote.command_for(key, 3, 'p600'))
            self.assertNotIn('takeoff', remote.command_for(key, 3, 'p600'))

    def test_remote_rpc_launch_is_idempotent_and_status_never_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / 'competition_development'
            (root / 'tools').mkdir(parents=True)
            (root / 'tools/start_onboard_stack.sh').touch()
            calls = []
            def spawn(argv, **kwargs):
                directory = Path(argv[3])
                pid = 100 + len(calls)
                calls.append(argv)
                remote.write_json(directory / 'process.json', {'pid': pid, 'stamp': 'live'})
                return SimpleNamespace(pid=pid + 1000, poll=lambda: None)
            fake_fcntl = SimpleNamespace(flock=lambda *args: None, LOCK_EX=2)
            request = dict(action='start', uav_id=3, model='p600')
            with patch.dict('sys.modules', {'fcntl': fake_fcntl}), \
                    patch.object(remote.Path, 'home', return_value=home), \
                    patch.object(remote, 'PROGRAM_SOURCE', '# isolated test', create=True), \
                    patch.object(remote, 'existing', return_value={}), \
                    patch.object(remote, 'ros_health', return_value={key: dict(ready=False, present=[], missing=[]) for key in remote.NAMES}), \
                    patch.object(remote, 'stamp', return_value='live'), \
                    patch.object(remote.subprocess, 'Popen', side_effect=spawn), \
                    contextlib.redirect_stdout(io.StringIO()):
                remote.rpc(request)
                remote.rpc(request)
                self.assertEqual(len(calls), 3, '重复确认不能重复启动三个程序')
                target = root / 'ground_runtime/programs_uav3/detection/process.json'
                remote.write_json(target, {'exit_code': 1})
                remote.rpc(dict(request, action='status'))
                self.assertEqual(len(calls), 3, '状态检查不能自动重启崩溃程序')

    def test_child_crash_is_red_even_if_other_child_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'console.log').write_text('[node-a-1] process has died [pid 1]\nprocess[node-b-2]: started with pid [2]\n', encoding='utf-8')
            with patch.object(remote, 'alive', return_value=True):
                self.assertEqual(remote.status(directory, 0)['state'], 'error')
                with (directory / 'console.log').open('a', encoding='utf-8') as stream:
                    stream.write('process[node-a-1]: started with pid [3]\n')
                self.assertEqual(remote.status(directory, 0)['state'], 'running')


if __name__ == '__main__':
    unittest.main()
