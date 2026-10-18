"""UAV5 Windows OpenSSH 8.1 实际报错回归；SSH、选路和日志均隔离。"""
import base64
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from competition_backend.program_manager import ProgramManager, SSHTransportError

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'competition_backend.program_manager.'
CONNECT_ERROR = b'ssh: connect to host 192.168.1.222 port 22: Invalid argument\r\n'


def reply(code=0, error=b'', data=None, output=None):
    if output is None:
        output = (b'ZHX1:' + base64.b64encode(zlib.compress(json.dumps(data or {'ok': True}).encode()))
                  if code == 0 else b'')
    return SimpleNamespace(returncode=code, stderr=error, stdout=output)


class SSHBindCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.manager = ProgramManager(ROOT, {})
        self.manager.local = dict(uav_id=5, model='p600', onboard_host='192.168.1.222',
                                  ground_host='192.168.1.230')
        self.manager._append = Mock()
        self.identity = ['-i', 'C:/test/zhixin_competition_ed25519', '-o', 'IdentitiesOnly=yes']
        for name, value in (('sys.platform', 'win32'), ('ssh_identity_args', Mock(return_value=self.identity))):
            p = patch(PREFIX + name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch(PREFIX + 'socket.socket')
        self.socket_factory = p.start()
        self.addCleanup(p.stop)
        self.probe = self.socket_factory.return_value.__enter__.return_value
        self.probe.getsockname.return_value = ('192.168.1.230', 49152)
        p = patch(PREFIX + 'subprocess.run')
        self.run = p.start()
        self.addCleanup(p.stop)

    def test_working_bound_ssh_is_unchanged(self):
        self.run.return_value = reply()
        self.assertEqual(self.manager._request('status', {}), {'ok': True})
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index('-b') + 1], '192.168.1.230')
        self.socket_factory.assert_not_called()
        self.manager._append.assert_not_called()

    def test_known_error_retries_same_payload_and_identity_then_checks_route_on_each_request(self):
        self.run.side_effect = [reply(255, CONNECT_ERROR), reply(), reply()]
        self.assertEqual(self.manager._request('auto_start', {}), {'ok': True})
        first, retry = self.run.call_args_list
        bound = first.args[0]
        index = bound.index('-b')
        self.assertEqual(retry.args[0], bound[:index] + bound[index + 2:])
        self.assertEqual(retry.kwargs, first.kwargs)  # 原请求、压缩正文和超时不变。
        self.assertEqual(retry.args[0][1:5], self.identity)
        self.assertIn('BatchMode=yes', retry.args[0])
        self.assertIn('StrictHostKeyChecking=accept-new', retry.args[0])
        self.socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.probe.connect.assert_called_once_with(('192.168.1.222', 22))
        self.probe.send.assert_not_called()
        self.probe.sendto.assert_not_called()
        self.socket_factory.return_value.__exit__.assert_called_once()
        # 同一管理器后续轮询不再每次先触发一次无效绑定。
        self.assertEqual(self.manager._request('status', {}), {'ok': True})
        self.assertNotIn('-b', self.run.call_args.args[0])
        self.assertEqual(self.probe.connect.call_count, 2)
        self.manager._append.assert_called_once()

    def test_wrong_default_route_blocks_retry_and_can_recover_after_route_restored(self):
        self.run.side_effect = [reply(255, CONNECT_ERROR), reply()]
        self.probe.getsockname.return_value = ('192.168.2.222', 49152)
        with self.assertRaisesRegex(SSHTransportError, '192.168.2.222.*192.168.1.230'):
            self.manager._request('auto_start', {})
        self.run.assert_called_once()
        self.manager._append.assert_not_called()
        self.probe.getsockname.return_value = ('192.168.1.230', 49152)
        self.assertEqual(self.manager._request('status', {}), {'ok': True})
        self.assertEqual(self.run.call_count, 2)
        self.assertNotIn('-b', self.run.call_args.args[0])

    def test_route_change_after_success_does_not_reuse_old_validation(self):
        self.run.side_effect = [reply(255, CONNECT_ERROR), reply()]
        self.manager._request('status', {})
        self.probe.getsockname.return_value = ('192.168.43.15', 49152)
        with self.assertRaisesRegex(SSHTransportError, '默认出口.*不一致'):
            self.manager._request('start', {}, program='onboard')
        self.assertEqual(self.run.call_count, 2)

    def test_route_probe_error_closes_socket_and_does_not_send_request(self):
        self.run.return_value = reply(255, CONNECT_ERROR)
        self.probe.connect.side_effect = OSError('network unavailable')
        with self.assertRaisesRegex(SSHTransportError, '默认出口核验失败'):
            self.manager._request('status', {})
        self.run.assert_called_once()
        self.socket_factory.return_value.__exit__.assert_called_once()

    def test_other_errors_never_trigger_compatibility_or_replay(self):
        cases = [
            reply(255, b'Permission denied (publickey,password).'),
            reply(255, b'Host key verification failed.'),
            reply(255, b'REMOTE HOST IDENTIFICATION HAS CHANGED!'),
            reply(255, b'ssh: connect to host 192.168.1.222 port 22: Connection timed out'),
            reply(255, b'Connection reset by peer'),
            reply(1, b'ValueError: Invalid argument'),
            reply(255, CONNECT_ERROR, output=b'remote command already started'),
            reply(255, b'remote script output\n' + CONNECT_ERROR),
            reply(255, CONNECT_ERROR.replace(b'192.168.1.222', b'192.168.1.227')),
        ]
        for result in cases:
            with self.subTest(error=result.stderr, output=result.stdout):
                self.run.reset_mock()
                self.run.return_value = result
                with self.assertRaises(RuntimeError):
                    self.manager._request('auto_start', {})
                self.run.assert_called_once()
                self.socket_factory.assert_not_called()
                self.assertFalse(self.manager._ssh_bind_unsupported)

    def test_no_fallback_without_binding_or_on_non_windows(self):
        self.run.return_value = reply(255, CONNECT_ERROR)
        with patch(PREFIX + 'sys.platform', 'linux'), self.assertRaises(SSHTransportError):
            self.manager._request('status', {})
        self.run.assert_called_once()
        self.run.reset_mock()
        del self.manager.local['ground_host']
        with self.assertRaises(SSHTransportError):
            self.manager._request('status', {})
        self.run.assert_called_once()
        self.socket_factory.assert_not_called()

    def test_auth_failure_on_compatible_connection_keeps_auth_classification(self):
        self.run.side_effect = [reply(255, CONNECT_ERROR), reply(255, b'Permission denied (publickey,password).')]
        with self.assertRaises(RuntimeError) as result:
            self.manager._request('status', {})
        self.assertNotIsInstance(result.exception, SSHTransportError)
        self.assertIn('Permission denied', str(result.exception))
        self.assertEqual(self.run.call_count, 2)

    def test_interrupted_compatible_request_is_not_replayed(self):
        for failure in (subprocess.TimeoutExpired('ssh', 4), reply(255, b'Connection reset by peer'),
                        reply(output=b'ZHX1:truncated')):
            with self.subTest(failure=failure):
                self.manager._ssh_bind_unsupported = False
                self.run.reset_mock()
                self.run.side_effect = [reply(255, CONNECT_ERROR), failure]
                with self.assertRaises((subprocess.TimeoutExpired, SSHTransportError)):
                    self.manager._request('clock_probe', {})
                self.assertEqual(self.run.call_count, 2)
                self.assertEqual(self.run.call_args.kwargs['timeout'], 4)

    def test_diagnostic_log_failure_does_not_break_connection(self):
        self.manager._append.side_effect = OSError('log file busy')
        self.run.side_effect = [reply(255, CONNECT_ERROR), reply()]
        self.assertEqual(self.manager._request('status', {}), {'ok': True})


if __name__ == '__main__':
    unittest.main()
