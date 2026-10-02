from email.parser import BytesParser
from email.policy import default
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from competition_backend import subject1_reporting as report

ROOT = Path(__file__).resolve().parents[2]


def sample():
    value=json.loads((ROOT/'docs/subject1_format/target-submission-template.json').read_text(encoding='utf-8'))
    value['name']=report.TEAM_NAME
    return value


class SubmissionReportTest(unittest.TestCase):
    def test_publisher_freeze_keeps_two_nearby_targets_from_same_uav(self):
        with tempfile.TemporaryDirectory() as tmp:
            mission_id = 'subject1-same-uav-neighbors'
            directory = Path(tmp) / 'UAV1' / mission_id
            directory.mkdir(parents=True)
            for name, longitude, confidence in (('one', 121.660000, .7),
                                                 ('two', 121.660040, .9)):
                metadata = dict(mission_id=mission_id, target_id=name,
                                target_type='车辆', target_model='车辆1', is_moving=False,
                                target_latitude=39.05, target_longitude=longitude,
                                confidence=confidence,
                                image_stamp={'secs': 1700000000, 'nsecs': 0})
                (directory / (name + '.json')).write_text(json.dumps(metadata), encoding='utf-8')
                (directory / (name + '.jpg')).write_bytes(b'jpeg')
            reporter = report.Subject1Reporter(tmp, publisher_dedup=True)
            built = reporter.build(mission_id, report.TEAM_NAME)
            self.assertEqual(['two', 'one'], [feature['id'] for feature in built['features']])
            frozen = reporter.prepare(built, already_deduplicated=True)
            final = json.loads(reporter.draft(frozen['draft_id']).read_text(encoding='utf-8'))
            self.assertEqual(['two', 'one'], [feature['id'] for feature in final['features']])
            self.assertEqual(2, report.validate_document(final)['target_count'])
            app = FastAPI()
            app.include_router(report.reporting_router(tmp, Mock(), publisher_dedup=True))
            with TestClient(app) as client:
                response = client.post('/api/v1/subject1/report/prepare', json={'mission_id': mission_id})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(2, response.json()['target_count'])
                downloaded = client.get(response.json()['download_url']).json()
                self.assertEqual(['two', 'one'], [feature['id'] for feature in downloaded['features']])
                imported = client.post('/api/v1/subject1/report/prepare', json={'document': built})
                self.assertEqual(200, imported.status_code, imported.text)
                self.assertEqual(2, imported.json()['target_count'])

    def test_imported_json_is_deduplicated_sorted_and_limited_to_sixteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = sample()
            exemplar = next(feature for feature in document['features']
                            if feature['properties']['targetCategory'] == '固定')
            document['features'] = []
            for index in range(18):
                feature = json.loads(json.dumps(exemplar))
                feature['id'] = 'target-%02d' % index
                feature['geometry']['coordinates'] = [121.66 + index * .001, 39.05]
                feature['properties']['confidence'] = index / 20
                document['features'].append(feature)
            reporter = report.Subject1Reporter(tmp)
            prepared = reporter.prepare(document)
            self.assertEqual(16, prepared['target_count'])
            final = json.loads(reporter.draft(prepared['draft_id']).read_text(encoding='utf-8'))
            self.assertEqual(['target-17', 'target-16'], [item['id'] for item in final['features'][:2]])
            self.assertEqual('target-02', final['features'][-1]['id'])
            self.assertEqual(2, len(json.loads((reporter.root / (prepared['draft_id'] + '.dedup.json'))
                                         .read_text(encoding='utf-8'))['omitted']))
            self.assertEqual(16, report.validate_document(final)['target_count'])

    def test_official_template_and_validation(self):
        self.assertEqual(report.validate_document(sample()),dict(target_count=3,fixed=2,moving=1))
        too_many = sample()
        too_many['features'] = [dict(too_many['features'][0], id='extra-%02d' % index)
                                for index in range(17)]
        with self.assertRaisesRegex(ValueError, '最多包含 16 个目标'):
            report.validate_document(too_many)
        mutations=[lambda d:d.update(features=[]),
                   lambda d:d['features'][0]['geometry'].update(coordinates=[39,116]),
                   lambda d:d['features'][0]['geometry'].update(coordinates=[116,39,50]),
                   lambda d:d['features'][0]['properties'].update(confidence=float('nan')),
                   lambda d:d['features'][0]['properties'].update(timestamp='2026-09-01T12:00:00'),
                   lambda d:d['features'][0]['properties'].pop('targetModel'),
                   lambda d:d['features'][2].update(id='target-001'),
                   lambda d:d['features'][1]['properties']['trackPoints'][0].update(coordinates=[1,2]),
                   lambda d:d['features'][1]['properties'].update(trackEndTime='2026-07-23T10:15:00+08:00')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                document=sample();mutation(document)
                with self.assertRaises(ValueError):report.validate_document(document)

    def test_wire_is_utf8_file_field_without_tokens_proxy_or_auto_send(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(report.urllib.request,'build_opener') as builder:
            reporter=report.Subject1Reporter(tmp)
            from unittest.mock import Mock
            reporter.audit = Mock()
            draft=reporter.prepare(sample())
            builder.assert_not_called()
            raw=reporter.draft(draft['draft_id']).read_bytes()
            self.assertIn(report.TEAM_NAME.encode('utf-8'),raw)
            self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
            response=io.BytesIO('{"success":true,"message":"已收到"}'.encode('utf-8'));response.code=200
            builder.return_value.open.return_value=response
            receipt=reporter.submit(draft['draft_id'])
            request=builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'http://192.168.1.199:8001/api/v1/public/recognition-results')
            self.assertEqual(request.method,'POST')
            self.assertFalse(any('token' in key.lower() or 'authorization' in key.lower() for key in request.headers))
            self.assertEqual(builder.call_args.args[0].proxies,{})
            message=BytesParser(policy=default).parsebytes(('Content-Type: '+request.headers['Content-type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+request.data)
            parts=list(message.iter_parts());self.assertEqual(len(parts),1)
            self.assertEqual(parts[0].get_param('name',header='content-disposition'),'file')
            self.assertEqual(parts[0].get_filename(),'target-submission.json')
            self.assertEqual(parts[0].get_payload(decode=True),raw)
            self.assertEqual(receipt['state'],'http_received')
            events = {call.args[0]: call.kwargs for call in reporter.audit.record.call_args_list}
            self.assertEqual(events['赛事上报回执']['receipt']['draft_id'], draft['draft_id'])
            self.assertTrue(Path(events['赛事上报回执']['receipt_file']).is_file())
            self.assertIn('已收到',receipt['response'])
            self.assertEqual(len(list(reporter.root.glob('receipt-*.json'))),1)

    def test_timeout_unknown_http_rejection_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(report.urllib.request,'build_opener') as builder:
            reporter=report.Subject1Reporter(tmp);draft=reporter.prepare(sample())
            builder.return_value.open.side_effect=TimeoutError('timeout')
            receipt=reporter.submit(draft['draft_id'])
            self.assertEqual(receipt['state'],'unknown');self.assertEqual(builder.return_value.open.call_count,1)
            builder.return_value.open.side_effect=urllib.error.HTTPError(report.ENDPOINT,422,'Invalid',{},io.BytesIO(b'{"detail":"invalid"}'))
            self.assertEqual(reporter.submit(draft['draft_id'])['state'],'rejected')
            reporter.draft(draft['draft_id']).write_bytes(b'changed')
            with self.assertRaises(ValueError):reporter.submit(draft['draft_id'])
            self.assertEqual(builder.return_value.open.call_count,2)
            with self.assertRaises(ValueError):reporter.draft('../../config/fleet.json')

    def test_api_authorization_prepare_download_and_explicit_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI();permission=Mock();app.include_router(report.reporting_router(tmp,permission))
            with TestClient(app) as client,patch.object(report.urllib.request,'build_opener') as builder:
                self.assertEqual(client.get('/api/v1/subject1/report').json()['team_name'],report.TEAM_NAME)
                draft=client.post('/api/v1/subject1/report/prepare',json={'document':sample(),'team_name':report.TEAM_NAME}).json()
                builder.assert_not_called()
                self.assertEqual(client.get(draft['download_url']).json()['name'],report.TEAM_NAME)
                self.assertEqual(client.post('/api/v1/subject1/report/submit',json={'draft_id':draft['draft_id']}).status_code,422)
                self.assertEqual(client.post('/api/v1/subject1/report/submit',headers={'Origin':'https://untrusted.example'},json={'draft_id':draft['draft_id'],'confirmed':True}).status_code,403)
                builder.assert_not_called()
                permission.side_effect=HTTPException(status_code=403,detail='非发布端')
                self.assertEqual(client.post('/api/v1/subject1/report/submit',json={'draft_id':draft['draft_id'],'confirmed':True}).status_code,403)
                builder.assert_not_called()

    def test_real_app_registers_reporting_routes_without_network(self):
        from competition_backend.api import create_app
        with tempfile.TemporaryDirectory() as tmp,patch.object(report.urllib.request,'build_opener') as builder:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':str(Path(tmp)/'state'),
                            'COMPETITION_IMAGE_ROOT':str(Path(tmp)/'images')})
            with TestClient(app) as client:
                result=client.get('/api/v1/subject1/report')
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json()['team_name'],report.TEAM_NAME)
                prepared=client.post('/api/v1/subject1/report/prepare',json={'document':sample()})
                self.assertEqual(prepared.status_code,200)
                self.assertEqual(prepared.json()['target_count'],3)
                planned=client.post('/api/v1/plan',json={'subject':'subject1'})
                self.assertEqual(planned.status_code,200,planned.text)
                mission_id=planned.json()['mission']['mission_id']
                automatic=client.get('/api/v1/subject1/report').json()['automatic']
                self.assertEqual(automatic['mission_id'],mission_id)
                self.assertEqual(automatic['attempts_started'],0)
            builder.assert_not_called()

    def test_aggregate_same_mission_2d_coordinates_and_sorted_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter=report.Subject1Reporter(tmp)
            for uid,second in ((1,5),(3,1)):
                directory=Path(tmp)/('UAV%d'%uid)/'subject1-common';directory.mkdir(parents=True)
                metadata=dict(mission_id='subject1-common',global_id='move',target_type='车辆',target_model='车辆2',is_moving=True,target_latitude=33.86,target_longitude=113.70,target_altitude=42,image_stamp={'secs':1700000000+second,'nsecs':0})
                (directory/'target.json').write_text(json.dumps(metadata),encoding='utf-8')
            self.assertEqual(reporter.missions(),['subject1-common'])
            document=reporter.build('subject1-common',report.TEAM_NAME)
            draft=reporter.prepare(document)
            self.assertEqual(draft['moving'],1)
            self.assertEqual(document['features'][0]['geometry']['coordinates'],[[113.7,33.86],[113.7,33.86]])
            props=document['features'][0]['properties']
            self.assertLess(props['trackStartTime'],props['trackEndTime'])
            self.assertNotIn('confidence',props);self.assertNotIn('imagePath',props)
            with self.assertRaises(ValueError):reporter.build('../../other',report.TEAM_NAME)

    def test_auto_report_uses_current_mission_at_24_minutes_five_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = dict(running=True, is_authority=True, session_id='competition-a', elapsed_seconds=1439)
            clock = Mock(snapshot=lambda: dict(state))
            auto = report.AutoSubject1Reporter(tmp, clock, lambda: True)
            auto.note_mission('subject1-old')
            auto.note_mission('subject1-current')
            with patch.object(report.Subject1Reporter, 'build', return_value=sample()) as build, \
                    patch.object(report.Subject1Reporter, 'submit', return_value=dict(
                        state='http_received', http_status=200, detail='已收到')) as submit:
                self.assertFalse(auto.tick(now=100))
                state['elapsed_seconds'] = 1440
                self.assertTrue(auto.tick(now=100))
                self.assertFalse(auto.tick(now=104.9))
                state['elapsed_seconds'] = 1200  # 已启动的五次上报仍按真实五秒间隔完成。
                for number in range(2, 6):
                    self.assertTrue(auto.tick(now=100 + (number - 1) * 5))
                self.assertFalse(auto.tick(now=130))
                deadline = time.monotonic() + 3
                while auto.snapshot()['attempts_finished'] != 5 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(auto.snapshot()['attempts_finished'], 5)
                self.assertEqual(submit.call_count, 5)
                self.assertEqual([call.args[0] for call in build.call_args_list], ['subject1-current'] * 5)
                self.assertEqual(auto.snapshot()['last_result']['state'], 'http_received')
            state['session_id'] = 'competition-b'
            state['elapsed_seconds'] = 1500
            self.assertFalse(auto.tick(now=140))  # 新比赛不能沿用上一场任务。
            self.assertIsNone(auto.snapshot()['mission_id'])
            self.assertEqual(auto.snapshot()['attempts_started'], 0)

    def test_auto_report_only_runs_on_publisher_and_reports_missing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = dict(running=True, is_authority=True, session_id='competition-a', elapsed_seconds=1440)
            role = [False]
            auto = report.AutoSubject1Reporter(tmp, Mock(snapshot=lambda: dict(state)), lambda: role[0])
            auto.note_mission('subject1-current')
            self.assertFalse(auto.tick(now=100))
            role[0] = True
            auto.note_mission('subject1-current')
            self.assertTrue(auto.tick(now=100))
            deadline = time.monotonic() + 3
            while auto.snapshot()['attempts_finished'] != 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(auto.snapshot()['last_result']['state'], 'failed')
            self.assertEqual(auto.snapshot()['attempts_started'], 1)


if __name__=='__main__':unittest.main()
