import json
import threading
import time
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch

from competition_backend.distributed_adapter import DistributedFleetAdapter
from competition_backend.models import Telemetry


class IndependentPeerPollingTest(unittest.TestCase):
    def adapter(self):
        adapter = DistributedFleetAdapter(range(1, 7), 3, 'ground-3',
            {uid: 'http://peer{}'.format(uid) for uid in [1, 2, 4, 5, 6]}, poll_interval_sec=.2)
        adapter.local_adapter.start = Mock()
        adapter.local_adapter.stop = Mock()
        return adapter

    def test_offline_peers_and_slow_state_posts_do_not_block_online_telemetry(self):
        adapter = self.adapter()
        release = threading.Event()
        slow = threading.Event()
        refreshed = threading.Event()
        times = []
        adapter.set_snapshot_provider(lambda: {'mission': {'mission_id': 'read-only-test'}})
        adapter.claim_peer_coordinator(adapter.node_id)
        adapter._is_coordinator = True

        def request(url, method='GET', payload=None):
            if 'peer1/' in url and '/telemetry/' in url:
                times.append(time.monotonic())
                if len(times) >= 4:
                    refreshed.set()
                return {'telemetry': asdict(Telemetry(uav_id=1, connected=True, received_at=0))}
            slow.set()
            release.wait(3)
            raise TimeoutError('离线节点或慢状态请求')

        adapter._request_json = request
        try:
            adapter.start()
            self.assertTrue(slow.wait(1))
            self.assertTrue(refreshed.wait(1.6), '其他节点仍在阻塞时，在线遥测应持续更新')
            self.assertFalse(release.is_set())
            self.assertLess(max(b-a for a,b in zip(times, times[1:])), .65)
            self.assertTrue(adapter._peer_telemetry[1].connected)
            self.assertIn('request_duration_ms', adapter.peer_sync_status()['1'])
            thread = adapter._poll_thread
            adapter.start()
            self.assertIs(adapter._poll_thread, thread)
        finally:
            release.set()
            adapter.stop()
        self.assertFalse(adapter._poll_thread.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in adapter._peer_workers))

    def test_online_mission_mirror_not_blocked_by_offline_peers(self):
        adapter = self.adapter()
        release = threading.Event()
        mirrored = threading.Event()
        copies = []
        adapter.claim_peer_coordinator(adapter.node_id)
        adapter._is_coordinator = True
        adapter.set_snapshot_provider(lambda: {'mission': {'mission_id': 'm-test'}})

        def request(url, method='GET', payload=None):
            if 'peer1/' in url:
                if url.endswith('/mission'):
                    copies.append(payload['snapshot']['mission']['mission_id'])
                    if len(copies) >= 2:
                        mirrored.set()
                return {'telemetry': asdict(Telemetry(uav_id=1, connected=True))}
            release.wait(3)
            raise TimeoutError('其他四台终端离线')

        adapter._request_json = request
        try:
            adapter.start()
            self.assertTrue(mirrored.wait(2.0))
            self.assertFalse(release.is_set())
            self.assertEqual(copies, ['m-test', 'm-test'])
        finally:
            release.set()
            adapter.stop()

    def test_peer_requests_bypass_system_proxy_but_keep_authentication(self):
        adapter = self.adapter()
        adapter.peer_token = 'test-peer-token'
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'accepted': True}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch('competition_backend.distributed_adapter.urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(adapter._request_json('http://peer1/api/v1/peer/telemetry/1'), {'accepted': True})
        self.assertEqual(build.call_args.args[0].proxies, {})
        req = opener.open.call_args.args[0]
        self.assertEqual(dict(req.header_items())['X-competition-peer-token'], 'test-peer-token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 2.0)


if __name__ == '__main__':
    unittest.main()
