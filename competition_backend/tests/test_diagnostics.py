import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Body, HTTPException
from fastapi.testclient import TestClient

from competition_backend.diagnostics import Diagnostics, DiagnosticMiddleware


class DiagnosticsTest(unittest.TestCase):
    def read(self, audit):
        return [json.loads(line) for line in (audit.directory / 'events.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_records_readable_before_shutdown_and_session_is_unique(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = Diagnostics(root), Diagnostics(root)
            try:
                first.record('图片已保存', mission_id='任务1', uav_id=2)
                self.assertNotEqual(first.directory, second.directory)
                self.assertEqual(self.read(first)[-1]['details']['mission_id'], '任务1')
                self.assertNotIn('程序会话正常结束', [r['event'] for r in self.read(first)])
                first.close()
                self.assertEqual(self.read(first)[-1]['event'], '程序会话正常结束')
            finally:
                first.close()
                second.close()

    def test_request_error_and_redaction_without_changing_response(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Diagnostics(root)
            app = FastAPI()
            app.add_middleware(DiagnosticMiddleware, audit=audit)
            @app.post('/api/v1/plan')
            def plan(payload: dict = Body(...)):
                raise HTTPException(409, '机载拒收：测试失败')
            try:
                with TestClient(app) as client:
                    reply = client.post('/api/v1/plan', json={'request_id': 'p1', 'auth_token': 'sensitive-secret', 'coordinate_mode': 'gps'})
                self.assertEqual(reply.status_code, 409)
                rows = self.read(audit)
                end = rows[-1]['details']
                self.assertEqual(end['http_status'], 409)
                self.assertEqual(end['response']['detail'], '机载拒收：测试失败')
                self.assertEqual(end['request']['request_id'], 'p1')
                self.assertEqual(rows[-2]['details']['request_id'], end['request_id'])
                self.assertNotIn('sensitive-secret', json.dumps(rows))
            finally:
                audit.close()

    def test_large_media_document_and_polling_not_copied(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Diagnostics(root)
            app = FastAPI()
            app.add_middleware(DiagnosticMiddleware, audit=audit)
            @app.post('/api/v1/subject1/report/prepare')
            def prepare(payload: dict = Body(...)):
                return {'draft_id': 'abc', 'target_count': 16}
            @app.get('/api/v1/status')
            def status():
                return {}
            try:
                with TestClient(app) as client:
                    for _ in range(5):
                        client.get('/api/v1/status')
                    client.post('/api/v1/subject1/report/prepare', json={'document': {'features': ['RAW_DATA']}})
                rows = self.read(audit)
                self.assertEqual(len(rows), 3)
                self.assertNotIn('RAW_DATA', json.dumps(rows))
                self.assertEqual(rows[-1]['details']['response']['target_count'], 16)
            finally:
                audit.close()

    def test_write_failure_does_not_raise_into_business(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Diagnostics(root)
            try:
                audit.record('开始测试')
                with patch.object(audit._handler, 'emit', side_effect=OSError('磁盘测试失败')):
                    with self.assertLogs('competition_backend.diagnostics', level='ERROR'):
                        audit.record('准备起飞')
            finally:
                audit.close()

    def test_rotation_keeps_current_file_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Diagnostics(root)
            try:
                audit.record('开始测试')
                audit._handler.maxBytes = 1000
                for i in range(20):
                    audit.record('测试', message='测' * 50, index=i)
                self.assertTrue((audit.directory / 'events.jsonl.1').is_file())
                self.assertLessEqual(len(list(audit.directory.iterdir())), 5)
                self.assertEqual(self.read(audit)[-1]['details']['index'], 19)
            finally:
                audit.close()

    def test_assignment_artifact_keeps_all_waypoints_without_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Diagnostics(root)
            try:
                payload = {'mission_id': 'm', 'task': {'waypoints': [[n, n, 5] for n in range(100)]}, 'auth_token': 'DO_NOT_SAVE'}
                name = audit.save_assignment(2, payload)
                result = json.loads((audit.directory / name).read_text(encoding='utf-8'))
                self.assertEqual(len(result['task']['waypoints']), 100)
                self.assertEqual(result['auth_token'], '[已隐藏]')
            finally:
                audit.close()

    def test_real_api_plan_and_takeoff_rejection_share_session(self):
        from competition_backend.api import create_app
        with tempfile.TemporaryDirectory() as root:
            app = create_app({'COMPETITION_ADAPTER': 'sim', 'COMPETITION_DATA_DIR': root,
                              'COMPETITION_DIAGNOSTICS_DIR': str(Path(root) / 'logs')})
            with TestClient(app) as client:
                response = client.post('/api/v1/plan', json={'subject': 'subject1'})
                self.assertEqual(response.status_code, 200)
                mission = response.json()['mission']['mission_id']
                reply = client.post('/api/v1/takeoff/confirm', json={'token': 'invalid-test-token'})
                self.assertEqual(reply.status_code, 409)
                rows = self.read(app.state.audit)
                tasks = [r for r in rows if r['event'] == '准备发送机载任务']
                self.assertEqual(len(tasks), 6)
                self.assertTrue(all(r['details']['mission_id'] == mission for r in tasks))
                self.assertTrue(all((app.state.audit.directory / r['details']['assignment_file']).is_file() for r in tasks))
                self.assertTrue(any(r['event'] == '任务事件' and r['details']['kind'] == 'mission_planned' for r in rows))
                self.assertEqual(rows[-1]['details']['http_status'], 409)
            self.assertEqual(self.read(app.state.audit)[-1]['event'], '程序会话正常结束')


if __name__ == '__main__':
    unittest.main()
