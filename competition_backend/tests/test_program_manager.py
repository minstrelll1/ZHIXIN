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
    def setUp(self):
        # 单元测试不能读取操作员电脑上的 SSH 私钥配置。
        identity = patch('competition_backend.program_manager.ssh_identity_args', return_value=[])
        identity.start()
        self.addCleanup(identity.stop)

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

    def test_ssh_request_uses_ascii_bootstrap_for_chinese_remote_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'tools' / 'remote_programs.py'
            script.parent.mkdir()
            script.write_text(
                'import json\n# 远端程序含中文\n'
                'def rpc(payload):\n'
                '    print(json.dumps({"action": payload["action"], "program": payload["program"]}))\n',
                encoding='utf-8',
            )
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600', onboard_host='192.168.1.202')
            def run(_command, **kwargs):
                source = kwargs['input'].decode('ascii')
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(source, '<stdin>', 'exec'), {'__name__': 'remote_programs_rpc_test'})
                return SimpleNamespace(returncode=0, stdout=output.getvalue().encode('utf-8'), stderr=b'')
            with patch('competition_backend.program_manager.subprocess.run', side_effect=run):
                result = manager._request('status', {'onboard': 0}, program='flight')
            self.assertEqual(result, {'action': 'status', 'program': 'flight'})

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

    def test_start_reads_latest_config_but_status_and_stop_ignore_broken_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tools').mkdir()
            (root / 'tools/remote_programs.py').write_text(
                'import json\ndef rpc(payload):\n    print(json.dumps(payload))\n', encoding='utf-8')
            (root / 'config').mkdir()
            path = root / 'config/onboard_programs.json'
            manager = ProgramManager(root, {})
            manager.local = dict(uav_id=3, model='p600', onboard_host='192.168.1.212')
            def run(_command, **kwargs):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(kwargs['input'].decode('ascii'), {'__name__': 'config_test'})
                return SimpleNamespace(returncode=0, stdout=output.getvalue().encode(), stderr=b'')
            with patch('competition_backend.program_manager.subprocess.run', side_effect=run):
                for command in ('echo 第一次 {uav_id}', 'echo 第二次 {uav_id}'):
                    path.write_text(json.dumps({'commands': {'flight': command}}, ensure_ascii=False), encoding='utf-8-sig')
                    self.assertEqual(manager._request('start', {})['commands']['flight'], command)
                path.write_text('{broken', encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'onboard_programs.json'):
                    manager._request('start', {})
                self.assertNotIn('commands', manager._request('status', {}))
                self.assertNotIn('commands', manager._request('stop', {}, program='flight'))

    def test_config_templates_preserve_shell_syntax(self):
        # 使用独立输入；本机 config/onboard_programs.json 允许用户修改。
        commands = {key: 'source ${HOME}/setup.bash && run_' + key + ' --uav {uav_id} --model {model}' for key in remote.NAMES}
        for uid in range(1, 7):
            for model in ('p600', 'su17'):
                for key in remote.NAMES:
                    self.assertEqual(remote.command_for(key, uid, model, commands),
                                     'source ${HOME}/setup.bash && run_%s --uav %d --model %s' % (key, uid, model))
        command = 'source ${HOME}/test/setup.bash && echo 中文 {uav_id} {model}'
        self.assertEqual(remote.command_for('flight', 3, 'p600', {'flight': command}),
                         'source ${HOME}/test/setup.bash && echo 中文 3 p600')

    def test_detection_config_sources_ros_and_uses_selected_drone(self):
        commands = json.loads((ROOT / 'config/onboard_programs.json').read_text(encoding='utf-8'))['commands']
        for uid in range(1, 7):
            lines = remote.command_for('detection', uid, 'p600', commands).splitlines()
            self.assertEqual(lines[0], 'source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash')
            self.assertIn('uav_id:=%d runtime_mode:=debug debug_save_dir:=/tmp/yolo_debug' % uid, lines[1])
            self.assertEqual(len(lines), 2)
        legacy = 'roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=2 runtime_mode:=debug debug_save_dir:=/tmp/yolo_debug'
        upgraded = remote.command_for('detection', 4, 'p600', {'detection': legacy})
        self.assertEqual(upgraded, remote.command_for('detection', 4, 'p600', commands))
        old_two_line = ('source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash\n'
                        'exec roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch '
                        'uav_id:=2 runtime_mode:=debug debug_save_dir:=/tmp/yolo_debug')
        self.assertEqual(remote.command_for('detection', 4, 'p600', {'detection': old_two_line}), upgraded)
        old_default = 'source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash && exec roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:={uav_id}'
        self.assertEqual(remote.command_for('detection', 4, 'p600', {'detection': old_default}), upgraded)
        self.assertEqual(remote.command_for('detection', 4, 'p600', {'detection': 'echo 自定义 {uav_id}'}), 'echo 自定义 4')
        with self.assertRaisesRegex(ValueError, '启动指令'):
            remote.command_for('flight', 1, 'p600', {'flight': ''})

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

    def test_lost_start_response_recovers_monitoring_without_restarting_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProgramManager(tmp, {})
            manager.local = dict(uav_id=1, model='p600', onboard_host='192.168.1.202')
            calls = []
            def request(action, offsets):
                calls.append(action)
                if len(calls) < 3:
                    raise RuntimeError('ssh: connection timed out')
                manager.stop_event.set()
                return {key: dict(state='running') for key in ('onboard', 'detection', 'flight')}
            manager._request = request
            manager._watch()
            self.assertEqual(calls, ['start', 'status', 'status'])
            self.assertEqual(manager.snapshot()['programs']['flight']['state'], 'running')

    def test_healthy_launcher_with_dead_nodes_is_not_green(self):
        state = remote.apply_health(dict(state='running'), dict(ready=False, present=['a'], missing=['b']), {'started_at': 1})
        self.assertEqual(state['state'], 'error')
        self.assertIn('b', state['detail'])
        state = remote.apply_health(dict(state='stopped'), dict(ready=True, present=['a'], missing=[]), {})
        self.assertEqual(state['state'], 'running')

    def test_remote_command_uses_selected_uav_and_does_not_arm(self):
        self.assertIn('uav_id:=3', remote.command_for('detection', 3, 'p600'))
        self.assertIn('uav_id:=3 flight_mode:=outdoor', remote.command_for('flight', 3, 'p600'))
        self.assertNotIn('flight_mode:=outdoor_small_range', remote.command_for('flight', 3, 'p600'))
        self.assertIn('--expect-uav-id 3 --direct --enable-motion', remote.command_for('onboard', 3, 'p600'))
        for key in remote.NAMES:
            self.assertNotIn('arming', remote.command_for(key, 3, 'p600'))
            self.assertNotIn('takeoff', remote.command_for(key, 3, 'p600'))

    def test_remote_ros_health_reads_effective_mode_and_recognition_radius(self):
        class RosServer:
            def __init__(self, endpoint):
                self.endpoint = endpoint
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def lookupNode(self, _caller, name):
                return (1, '', 'http://node.test')
            def getPid(self, _caller):
                return (1, '', 1234)
            def getParam(self, _caller, name):
                if name.endswith('/indoor_mode'):
                    return (1, '', 'false')
                if name.endswith('/outdoor_small_range_mode'):
                    return (1, '', 'false')
                if name.endswith('/reconnaissance_radius_m'):
                    return (1, '', 30.0)
                raise AssertionError(name)
        with patch('xmlrpc.client.ServerProxy', side_effect=lambda endpoint, **_: RosServer(endpoint)):
            health = remote.ros_health(3)
        self.assertTrue(health['flight']['ready'])
        self.assertEqual(health['flight']['flight_mode'], 'outdoor')
        self.assertEqual(health['flight']['reconnaissance_radius_m'], 30.0)

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
            request = dict(action='start', uav_id=3, model='p600', commands={'flight': 'exec custom_program --uav {uav_id}'})
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
                self.assertEqual(calls[-1][-1], 'exec custom_program --uav 3')
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
