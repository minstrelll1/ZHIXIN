"""在本机套接字上模拟帧中断和 ACK 丢失，不需要 ROS 或真实无人机。"""
import ast
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from su17_image_transfer.protocol import encode_frame, receive_ack, receive_response

spec = importlib.util.spec_from_file_location('image_recovery_receiver', ROOT / 'ground/ground_image_receiver.py')
receiver_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver_module)


def sender_methods():
    tree = ast.parse((ROOT / 'scripts/onboard_image_sender.py').read_text(encoding='utf-8'))
    methods = {'_announce_mission', '_mark_reconciliation_needed', '_reconcile_once',
               '_load_cached_metadata', '_reconcile_timer_callback', '_mark_image_acked'}
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    functions = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    from competition_shared.return_events import matches_return_event, competition_elapsed
    scope = dict(time=time, json=json, rospy=Mock(), encode_frame=encode_frame,
                 matches_return_event=matches_return_event, competition_elapsed=competition_elapsed)
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<sender-recovery>', 'exec'), scope)
    return type('Sender', (), {name: scope[name] for name in methods})


@contextmanager
def connection(receiver):
    local, remote = socket.socketpair()
    local.settimeout(2)
    worker = threading.Thread(target=receiver._handle_client, args=(remote, ('127.0.0.1', 12345)), daemon=True)
    worker.start()
    try:
        yield local
    finally:
        local.close()
        worker.join(2)
        if worker.is_alive():
            raise AssertionError('图片连接线程未退出')


class ImageReconnectionTest(unittest.TestCase):
    def test_partial_frame_and_lost_ack_recover_only_missing_cached_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receiver = receiver_module.GroundImageReceiver('127.0.0.1', 0, root / 'ground', '',
                                                           status_interval=0, client_timeout=.08)
            cache = root / 'cache'
            cache.mkdir()
            jpeg = b'\xff\xd8' + b'image' * 100 + b'\xff\xd9'
            for name in ('received', 'partial'):
                (cache / (name + '.jpg')).write_bytes(jpeg)
                (cache / (name + '.json')).write_text(json.dumps(dict(
                    mission_id='reconnect-test', uav_id=1, request_id=name,
                    file_name=name + '.jpg', target_id=name)), encoding='utf-8')
            received = json.loads((cache / 'received.json').read_text())
            with connection(receiver) as client:
                client.sendall(encode_frame(received, jpeg))
                # 地面已落盘，但机载端没有收到/读取 ACK。
                deadline = time.monotonic() + 1
                while not list((root / 'ground').rglob('received.json')) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(list((root / 'ground').rglob('received.json')))
            partial = json.loads((cache / 'partial.json').read_text())
            with connection(receiver) as client:
                client.sendall(encode_frame(partial, jpeg)[:-50])
                time.sleep(.15)
                self.assertEqual(b'', client.recv(1))
            self.assertFalse(list((root / 'ground').rglob('partial.jpg')))
            sender = sender_methods()()
            sender.enable_offline_recovery = True
            sender.uav_id, sender.auth_token = 1, ''
            sender.active_mission_id = 'reconnect-test'
            sender.mission_started_at_unix_ns = time.time_ns() - 10_000_000_000
            sender.mission_lock = threading.Lock()
            sender._mission_cache_dir = lambda _: cache
            frames = []
            with connection(receiver) as client:
                def exchange(metadata):
                    client.sendall(encode_frame(metadata, b'\x00'))
                    success, response = receive_response(client)
                    self.assertTrue(success)
                    return response
                def send(frame, label):
                    frames.append(label)
                    client.sendall(frame)
                    return receive_ack(client), 1, ''
                sender._exchange_control, sender._send_with_retries = exchange, send
                self.assertEqual((2, 1), sender._reconcile_once('reconnect-test'))
                self.assertEqual((2, 0), sender._reconcile_once('reconnect-test'))
            self.assertEqual(['补传图片 partial.jpg'], frames)
            self.assertEqual(jpeg, next((root / 'ground').rglob('partial.jpg')).read_bytes())
            info = json.loads(next((root / 'ground').rglob('partial.json')).read_text(encoding='utf-8'))
            self.assertEqual('partial', info['target_id'])
            self.assertTrue(info['retransmission'])
            receiver.stop()

    def test_mission_announce_failure_requests_recovery_without_any_images(self):
        sender = sender_methods()()
        sender.uav_id, sender.auth_token = 1, ''
        sender.active_mission_id = 'empty-mission'
        sender.mission_lock = threading.Lock()
        sender.recovery_revision = 0
        sender.recovery_pending, sender.reconcile_done = False, True
        sender._exchange_control = Mock(side_effect=TimeoutError('lost network'))
        sender._publish_status = Mock()
        sender._announce_mission('empty-mission', 123456789)
        self.assertTrue(sender.recovery_pending)
        self.assertFalse(sender.reconcile_done)
        sender.enable_offline_recovery = True
        sender.mission_started_monotonic = time.monotonic()
        sender.reconcile_after_minutes, sender.final_reconcile_done = 23.5, False
        sender._competition_time, sender._competition_time_received = {}, 0
        sender._successful_return = {}
        sender.final_reconcile_trigger = ""
        sender._schedule_pending_delivery = Mock()
        sender._schedule_reconciliation = Mock()
        sender._reconcile_timer_callback(None)
        sender._schedule_reconciliation.assert_not_called()
        sender._schedule_pending_delivery.assert_called_once()
        self.assertTrue(sender._announce_pending)


if __name__ == '__main__':
    unittest.main()
