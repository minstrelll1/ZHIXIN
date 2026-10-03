import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('recording_transport', Path(__file__).resolve().parents[1] / 'recording_transport.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class RecordingTransportTest(unittest.TestCase):
    def test_remote_download_manifest_is_frozen_after_bags_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = '/home/amov/flight_records/uav1_20261003_120000'
            scope = {}
            with patch.dict(sys.modules, {'fcntl': SimpleNamespace(flock=Mock(), LOCK_EX=2)}):
                exec(transport.REMOTE_WORKER, scope)
            scope['Path'] = lambda value: root if value == remote_root else Path(value)
            (root / 'flight.bag.active').write_bytes(b'collecting')
            request = dict(action='build_manifest', directory=remote_root)
            with self.assertRaisesRegex(RuntimeError, '尚未完成'):
                scope['rpc'](request)
            (root / 'flight.bag.active').rename(root / 'flight.bag')
            digest = hashlib.sha256(b'collecting').hexdigest()
            (root / 'bag_sha256.txt').write_text(digest + '  flight.bag\n')
            (root / 'position.csv').write_bytes(b'time,x,y,z\n')
            built = scope['rpc'](request)
            self.assertEqual(built, scope['rpc'](dict(action='files', directory=remote_root)))
            files = {entry['name']: entry for entry in built['files']}
            self.assertEqual({'flight.bag', 'bag_sha256.txt', 'position.csv'}, set(files))
            self.assertEqual(digest, files['flight.bag']['sha256'])
            self.assertEqual(hashlib.sha256(b'time,x,y,z\n').hexdigest(), files['position.csv']['sha256'])

    def test_lost_ssh_response_retries_same_request_and_auth_failure_stops(self):
        client = transport.RecordingTransport('amov@192.0.2.1', '/home/amov/flight_records/uav1_20261003_120000', retry_seconds=0)
        results = [SimpleNamespace(returncode=255, stderr=b'Connection timed out'),
                   SimpleNamespace(returncode=0, stdout=b'{"done":true}')]
        with patch.object(transport.subprocess, 'run', side_effect=results) as run:
            self.assertEqual({'done': True}, client.request({'action': 'job', 'job_id': 'a' * 32}))
            self.assertEqual(run.call_args_list[0], run.call_args_list[1])
            self.assertFalse(client.disconnected)
        with patch.object(transport.subprocess, 'run', return_value=SimpleNamespace(
                returncode=255, stderr=b'Permission denied (publickey,password).')) as run:
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                client.request({'action': 'job'})
            self.assertEqual(1, run.call_count)

    def test_remote_job_is_detached_and_polled_without_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = '/home/amov/flight_records/uav1_20261003_120000'
            scope = {}
            with patch.dict(sys.modules, {'fcntl': SimpleNamespace(flock=Mock(), LOCK_EX=2)}):
                exec(transport.REMOTE_WORKER, scope)
            scope['Path'] = lambda value: root if value == remote_root else Path(value)
            request = dict(action='job', directory=remote_root, job_id='a' * 32,
                           script=base64.b64encode(b'echo job').decode(), offset=0)
            with patch.object(subprocess, 'Popen', return_value=SimpleNamespace(pid=1234)) as launch, \
                 patch.object(scope['os'], 'kill'):
                first = scope['rpc'](request)
                self.assertFalse(first['done'])
                # 模拟第一次 SSH 响应丢失；机载操作已启动，重试不能再启动一次。
                second = scope['rpc'](request)
                self.assertFalse(second['done'])
                self.assertEqual(1, launch.call_count)
                self.assertTrue(launch.call_args.kwargs['start_new_session'])
                job = root / '.ground_jobs' / request['job_id']
                (job / 'output.log').write_bytes('整理完成\n'.encode())
                (job / 'done').write_text('0')
                result = scope['rpc'](request)
                self.assertTrue(result['done'])
                self.assertEqual(0, result['code'])
                self.assertEqual('整理完成\n', base64.b64decode(result['output']).decode())
                self.assertEqual(1, launch.call_count)

    def test_poll_uses_one_job_id_and_stream_offsets(self):
        client = transport.RecordingTransport('host', 'directory')
        replies = [dict(done=False, code=None, output=base64.b64encode(b'abc').decode(), offset=3),
                   dict(done=True, code=0, output=base64.b64encode(b'def').decode(), offset=6)]
        requests = []
        def request(payload):
            requests.append(dict(payload))
            return replies.pop(0)
        output = io.BytesIO()
        with patch.object(client, 'request', side_effect=request), patch.object(transport.time, 'sleep'), \
             patch.object(transport.sys, 'stdout', SimpleNamespace(buffer=output)):
            self.assertEqual(0, client.run_job('encoded'))
        self.assertEqual(b'abcdef', output.getvalue())
        self.assertEqual(requests[0]['job_id'], requests[1]['job_id'])
        self.assertEqual([0, 3], [r['offset'] for r in requests])

    def test_copy_reconnect_skips_completed_files_and_verifies_partial_before_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = b'completed-bag', b'new-csv-data'
            (root / 'a.bag').write_bytes(first)
            files = [dict(name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
                     for name, data in [('a.bag', first), ('b.csv', second)]]
            client = transport.RecordingTransport('host', '/home/amov/flight_records/uav1_20261003_120000', retry_seconds=0)
            attempts = []
            def scp(args, **kwargs):
                self.assertFalse((root / 'b.csv').exists())
                attempts.append(args)
                Path(args[-1]).write_bytes(second[:3] if len(attempts) == 1 else second)
                return SimpleNamespace(returncode=255 if len(attempts) == 1 else 0, stderr=b'Connection reset')
            with patch.object(client, 'run_job', return_value=0), \
                 patch.object(client, 'request', return_value={'files': files}), \
                 patch.object(transport.subprocess, 'run', side_effect=scp) as run:
                client.copy(root)
                self.assertEqual(second, (root / 'b.csv').read_bytes())
                self.assertEqual(2, run.call_count)
                client.copy(root)
                self.assertEqual(2, run.call_count)
            self.assertEqual(first, (root / 'a.bag').read_bytes())
            self.assertFalse((root / '.b.csv.download').exists())

    def test_bad_hash_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data.bag').write_bytes(b'old')
            client = transport.RecordingTransport('host', '/remote')
            item = dict(name='data.bag', size=3, sha256=hashlib.sha256(b'new').hexdigest())
            def scp(args, **kwargs):
                Path(args[-1]).write_bytes(b'bad')
                return SimpleNamespace(returncode=0)
            with patch.object(client, 'run_job', return_value=0), \
                 patch.object(client, 'request', return_value={'files': [item]}), \
                 patch.object(transport.subprocess, 'run', side_effect=scp):
                with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                    client.copy(root)
            self.assertEqual(b'old', (root / 'data.bag').read_bytes())


if __name__ == '__main__':
    unittest.main()
