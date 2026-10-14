import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from competition_backend.prometheus_connection import PrometheusConnection
from competition_backend.distributed_adapter import DistributedFleetAdapter


class HeartbeatDisplayTests(unittest.TestCase):
    def test_actual_vendor_heartbeat_expires_without_ssh_refresh(self):
        conn = PrometheusConnection(dict(uav_id=5, onboard_host='192.168.1.222'))
        self.assertEqual(conn.heartbeat_snapshot()['state'], 'unknown')
        conn.socket = object()
        self.assertEqual(conn.heartbeat_snapshot()['state'], 'waiting')
        conn.last_heartbeat, conn.heartbeat_count = 100, 42
        with patch('competition_backend.prometheus_connection.time.monotonic', return_value=102):
            self.assertEqual(conn.heartbeat_snapshot()['state'], 'normal')
            self.assertEqual(conn.heartbeat_snapshot()['count'], 42)
        with patch('competition_backend.prometheus_connection.time.monotonic', return_value=104):
            self.assertEqual(conn.heartbeat_snapshot()['state'], 'abnormal')
        conn.external = True
        self.assertEqual(conn.heartbeat_snapshot()['state'], 'unknown')
        conn.external = False
        conn.stop_event.set()
        self.assertEqual(conn.heartbeat_snapshot()['state'], 'unknown')

    def test_peer_propagates_relative_age_expires_and_recovers(self):
        adapter = DistributedFleetAdapter(range(1,7), local_uav_id=2, node_id='ground-2',
                                         peers={5:'http://test'}, port=0)
        heartbeat = dict(state='normal', age_seconds=.4, count=6)
        adapter._request_json = Mock(return_value={'prometheus_heartbeat':heartbeat})
        with patch('competition_backend.distributed_adapter.time.monotonic', return_value=100):
            adapter._poll_peer_once(5, 'http://test')
        with patch('competition_backend.distributed_adapter.time.monotonic', return_value=102):
            state=adapter.peer_heartbeat_status()['5']
            self.assertAlmostEqual(state['age_seconds'],2.4)
            self.assertEqual(state['state'],'normal')
        with patch('competition_backend.distributed_adapter.time.monotonic', return_value=104):
            self.assertEqual(adapter.peer_heartbeat_status()['5']['state'],'unknown')
            adapter._poll_peer_once(5, 'http://test')
            self.assertEqual(adapter.peer_heartbeat_status()['5']['state'],'normal')
        adapter._request_json.return_value={}
        adapter._poll_peer_once(5,'http://test')
        self.assertNotIn('5',adapter.peer_heartbeat_status())

    def test_bad_optional_peer_heartbeat_never_breaks_status(self):
        adapter=DistributedFleetAdapter([1,2],local_uav_id=1,node_id='ground-1',peers={},port=0)
        for bad in (None, 'bad', float('nan'), -1, True):
            with patch('competition_backend.distributed_adapter.time.monotonic', return_value=100):
                adapter._peer_heartbeats[2]=({'state':'normal','age_seconds':bad},100,0)
                self.assertEqual(adapter.peer_heartbeat_status()['2']['state'],'unknown')

    def test_api_exports_local_heartbeat_and_peers_without_network_calls(self):
        from competition_backend.api import create_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'COMPETITION_DATA_DIR':directory, 'COMPETITION_ADAPTER':'distributed',
            'COMPETITION_LOCAL_UAV_ID':'2', 'COMPETITION_GROUND_PEERS':'',
            'COMPETITION_PEER_TOKEN':'test-token'}):
            app=create_app()
            expected=dict(state='normal',age_seconds=.2,count=12)
            app.state.program_manager=Mock()
            app.state.program_manager.snapshot.return_value=dict(uav_id=2, connection={'heartbeat_status':expected},programs={})
            endpoints={route.path:route.endpoint for route in app.routes if hasattr(route,'endpoint')}
            result=endpoints['/api/v1/status']()
            self.assertEqual(result['prometheus_heartbeat']['2'],expected)
            result=endpoints['/api/v1/peer/telemetry/{uav_id}'](2,'test-token')
            self.assertEqual(result['prometheus_heartbeat'],expected)

if __name__=='__main__': unittest.main()
