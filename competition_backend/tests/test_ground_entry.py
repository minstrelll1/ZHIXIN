import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.ground_entry import GroundEntry, GroundServices
from competition_shared.fleet import default_fleet, apply_fixed_binding


class GroundEntryTest(unittest.TestCase):
    def test_netstat_output_uses_bytes_and_tolerates_windows_code_page(self):
        output = (
            b"TCP    0.0.0.0:8554    0.0.0.0:0    LISTENING    7764\r\n"
            b"TCP    0.0.0.0:8891    0.0.0.0:0    LISTENING    7764\r\n"
            b"\xbb\xcc"
        )
        with patch('competition_backend.ground_entry.subprocess.run',
                   return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(GroundServices._existing_media_pid(8554, 8891), 7764)

    def test_netstat_empty_output_does_not_call_splitlines_on_none(self):
        with patch('competition_backend.ground_entry.subprocess.run',
                   return_value=SimpleNamespace(stdout=None)):
            self.assertIsNone(GroundServices._existing_media_pid(8554, 8891))

    def test_media_reuses_matching_existing_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'media'
            media.mkdir()
            (media / 'mediamtx.template.yml').write_text(
                'path: __UAV_PATH__\nsource: __RTSP_SOURCE__\nport: __WEBRTC_PORT__\n',
                encoding='utf-8',
            )
            (media / ('mediamtx.exe' if os.name == 'nt' else 'mediamtx')).touch()
            local = {'uav_id': 1, 'video_rtsp_source': 'rtsp://192.168.1.202:8554/live',
                     'video_webrtc_port': 8891}
            runtime = root / 'ground_runtime'
            runtime.mkdir()
            (runtime / 'mediamtx_uav1.yml').write_text(
                'path: uav1\nsource: rtsp://192.168.1.202:8554/live\nport: 8891\n',
                encoding='utf-8',
            )
            service = GroundServices(local, {'COMPETITION_MEDIAMTX_DIRECTORY': str(media)})
            with patch('competition_backend.ground_entry.ROOT', root), \
                    patch.object(GroundServices, '_existing_media_pid', return_value=7764), \
                    patch('competition_backend.ground_entry.subprocess.Popen') as spawn:
                service.start_media()
            spawn.assert_not_called()
            self.assertIsNone(service.process)

    def test_media_rejects_reuse_when_config_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'media'
            media.mkdir()
            (media / 'mediamtx.template.yml').write_text('source: __RTSP_SOURCE__\n', encoding='utf-8')
            (media / ('mediamtx.exe' if os.name == 'nt' else 'mediamtx')).touch()
            local = {'uav_id': 1, 'video_rtsp_source': 'rtsp://192.168.1.202:8554/live',
                     'video_webrtc_port': 8891}
            service = GroundServices(local, {'COMPETITION_MEDIAMTX_DIRECTORY': str(media)})
            with patch('competition_backend.ground_entry.ROOT', root), \
                    patch.object(GroundServices, '_existing_media_pid', return_value=7764):
                with self.assertRaisesRegex(RuntimeError, '当前配置与本终端不一致'):
                    service.start_media()

    def test_no_terminal_or_services_before_web_selection_and_cleanup_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fleet.json'
            config = default_fleet()
            for uid in range(1, 7):
                config = apply_fixed_binding(config, uid, 'p600')
            path.write_text(json.dumps(config), encoding='utf-8')
            env = dict(os.environ, COMPETITION_FLEET_CONFIG=str(path), COMPETITION_DATA_DIR=tmp,
                       COMPETITION_TCP_TOKEN='test', COMPETITION_PEER_TOKEN='test', COMPETITION_PORT='8000')
            services = Mock()
            reached, gate = threading.Event(), threading.Event()
            def start_services():
                reached.set()
                gate.wait(3)
            services.start.side_effect = start_services
            def factory(environment):
                app = create_app(environment)
                for thing in (app.state.orchestrator, app.state.image_collector, app.state.pointcloud_collector, app.state.traffic_monitor):
                    thing.start = Mock()
                    thing.stop = Mock()
                app.state.adapter._request_json = Mock(side_effect=RuntimeError('离线测试'))
                return app
            wrapped = Mock(side_effect=factory)
            entry = GroundEntry(env, app_factory=wrapped, services_factory=Mock(return_value=services), programs_factory=Mock(return_value=Mock()))
            with TestClient(entry) as client:
                self.assertIsNone(client.get('/api/v1/operator').json()['ground_terminal_id'])
                self.assertEqual(client.post('/api/v1/plan', json={}).status_code, 409)
                wrapped.assert_not_called()
                start = time.monotonic()
                r = client.post('/api/v1/operator', json={'ground_terminal_id': 4, 'model': 'p600', 'task_publisher': False})
                self.assertEqual(r.status_code, 200)
                self.assertLess(time.monotonic() - start, 1.0)
                self.assertTrue(reached.wait(1))
                self.assertEqual(wrapped.call_args.args[0]['COMPETITION_LOCAL_UAV_ID'], '4')
                self.assertIsNone(entry.active)
                gate.set()
                deadline = time.monotonic()+3
                while entry.active is None and time.monotonic()<deadline:
                    time.sleep(.01)
                r = client.get('/api/v1/operator')
                self.assertEqual(r.json()['ground_terminal_id'], 4)
                self.assertEqual(entry.active.state.adapter.local_uav_id, 4)
            services.stop.assert_called_once()

    def test_publisher_selection_returns_without_waiting_for_offline_peers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fleet.json'
            path.write_text(json.dumps(default_fleet()), encoding='utf-8')
            app=create_app(dict(os.environ,COMPETITION_FLEET_CONFIG=str(path),COMPETITION_DATA_DIR=tmp,
                                COMPETITION_ADAPTER='distributed',COMPETITION_ASYNC_OPERATOR_SELECTION='1'))
            gate = threading.Event()
            def roles():
                gate.wait(2)
                return {2: {'configured': True, 'task_publisher': True}}
            app.state.adapter.peer_operator_status=roles
            client=TestClient(app)
            start=time.monotonic()
            result=client.post('/api/v1/operator',json={'ground_terminal_id':1,'model':'p600','task_publisher':True})
            self.assertLess(time.monotonic()-start,1)
            self.assertTrue(result.json()['selection_pending'])
            self.assertEqual(client.post('/api/v1/plan',json={'subject':'subject1'}).status_code,409)
            gate.set()
            deadline=time.monotonic()+2
            while client.get('/api/v1/operator').json()['selection_pending'] and time.monotonic()<deadline:
                time.sleep(.01)
            role=client.get('/api/v1/operator').json()
            self.assertFalse(role['configured'])
            self.assertIn('已被选为任务发布端',role['selection_error'])


if __name__ == '__main__':
    unittest.main()
