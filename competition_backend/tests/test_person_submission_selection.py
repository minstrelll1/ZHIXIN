"""人员选择、真实补位、整条最近观测及正式冻结/上传格式的离线回归。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from competition_shared.subject1_dedup import consolidate
from competition_shared.submission_format import format_submission
from competition_shared.reporting_selection import save_reporting_selection, load_reporting_selection
from competition_backend.subject1_reporting import Subject1Reporter, TEAM_NAME, validate_document
from test_subject1_dedup_rules import feature, document


def selection(*ids):
    return dict(category_count=len(ids), category_ids=list(ids))


class PersonSelectionTest(unittest.TestCase):
    def test_static_representative_vehicle_building_then_count_success_score(self):
        cases = [
            [feature('person', broad='人员', model='人员1', count=999, success=True, score=1),
             feature('vehicle', count=1, score=.1)],
            [feature('vehicle', count=8, success=True), feature('building', broad='工事', model='工事2', count=9)],
            [feature('no-success', count=10), feature('success', count=9, success=True)],
            [feature('low-score', count=10, success=True, score=.1), feature('high-score', count=10, success=True, score=.9)],
        ]
        for values, expected in zip(cases, ('vehicle','building','no-success','high-score')):
            result,_=consolidate(document(values))
            self.assertEqual([expected],[f['id'] for f in result['features']])

    def test_selected_person_wins_same_static_or_moving_group(self):
        for moving in (False,True):
            with self.subTest(moving=moving):
                values=[feature('unselected',broad='人员',model='人员1',moving=moving,count=999,success=True,track_points=8),
                        feature('selected',broad='人员',model='人员2',moving=moving,count=1,track_points=5)]
                result,audit=consolidate(document(values),recognition_selection=selection(16 if moving else 12))
                self.assertEqual(['selected'],[f['id'] for f in result['features']])
                self.assertEqual([],audit['person_selection']['remappings'])

    def test_full_primary_set_does_not_admit_unselected_people(self):
        values=[feature('car%d'%i,east=i*100) for i in range(16)]
        values.extend([feature('person',broad='人员',model='人员4',north=3000,count=999),
                       feature('moving-person',broad='人员',model='运动的人员4',moving=True,north=4000,count=999)])
        result,audit=consolidate(document(values),recognition_selection=selection(11,15))
        self.assertEqual(16,len(result['features']))
        self.assertTrue(all(f['properties']['targetType']=='车辆' for f in result['features']))
        self.assertEqual([],audit['person_selection']['remappings'])

    def test_unique_people_fill_before_merged_static_duplicates(self):
        values=[feature('duplicate%d'%i,count=i) for i in range(16)]
        values.extend(feature('person%d'%i,broad='人员',model='人员4',north=500,east=i*100) for i in range(4))
        result,audit=consolidate(document(values),recognition_selection=selection(12))
        self.assertEqual(16,len(result['features']))
        self.assertEqual(4,len(audit['person_selection']['remappings']))
        self.assertEqual(11,len(audit['backfilled']))
        self.assertTrue(all(f['properties']['targetModel']=='人员2' for f in result['features'] if f['properties']['targetType']=='人员'))

    def test_fixed_moving_mapping_balances_separately_and_never_exceeds_quota(self):
        values=[feature('selected-static',broad='人员',model='人员1',east=0),
                feature('static-a',broad='人员',model='人员2',east=100),
                feature('static-b',broad='人员',model='人员4',east=200),
                feature('selected-moving',broad='人员',model='运动的人员2',moving=True,north=1000)]
        values.extend(feature('moving%d'%i,broad='人员',model='运动的人员1',moving=True,north=2000+i*100) for i in range(3))
        original=copy.deepcopy(values)
        result,audit=consolidate(document(values),recognition_selection=selection(11,13,16,18))
        result,_=format_submission(result)
        self.assertEqual(dict(target_count=6,fixed=3,moving=3),validate_document(result))
        self.assertEqual(values,original)
        for f in result['features']:
            p=f['properties']
            self.assertEqual('人员',p['targetType'])
            self.assertIn(p['targetModel'],('人员2','人员4') if p['targetCategory']=='移动' else ('人员1','人员3'))
            if p['targetCategory']=='移动':self.assertEqual(5,len(p['trackPoints']))
        self.assertEqual({'固定':{1:2,3:1},'移动':{2:2,4:1}},audit['person_selection']['final_model_counts'])
        self.assertEqual(4,len(audit['person_selection']['remappings']))
        self.assertEqual(10,audit['selection']['shortfall'])

    def test_empty_selection_or_no_model_in_group_preserves_original(self):
        for configured in (None,selection(),selection(0,7),selection(15)):
            original=document([feature('static',broad='人员',model='人员3')])
            result,audit=consolidate(original,recognition_selection=configured)
            self.assertEqual('人员3',result['features'][0]['properties']['targetModel'])
            self.assertEqual([],audit['person_selection']['remappings'])

    def test_task_selection_isolated_persistent_atomic_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=save_reporting_selection(tmp,'subject1-first',selection(11,16))
            save_reporting_selection(tmp,'subject1-next',selection(13,18))
            self.assertEqual([11,16],load_reporting_selection(tmp,'subject1-first')['category_ids'])
            self.assertEqual([],load_reporting_selection(tmp,'subject1-missing')['category_ids'])
            longest='subject1-'+'a'*111
            save_reporting_selection(tmp,longest,selection(14))
            self.assertEqual([14],load_reporting_selection(tmp,longest)['category_ids'])
            self.assertFalse(list(Path(tmp).rglob('*.part')))
            for invalid in ('../escape','subject1-../../escape'):
                with self.assertRaises(ValueError):save_reporting_selection(tmp,invalid,selection(11))
            first.write_text('{"mission_id":"subject1-wrong"}',encoding='utf8')
            with self.assertRaises(ValueError):load_reporting_selection(tmp,'subject1-first')

    def test_raw_build_and_freeze_read_same_mission_selection_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);mission='subject1-person-freeze';folder=root/'UAV2'/mission;folder.mkdir(parents=True)
            save_reporting_selection(root,mission,selection(12,17))
            save_reporting_selection(root,'subject1-next',selection(11,15))
            for target,moving in (('static',False),('moving',True)):
                for index in range(3):
                    raw=dict(mission_id=mission,global_id=target,is_moving=moving,
                        category_id=15 if moving else 14,category='人',target_type='dsolider1' if moving else 'solider4',
                        longitude_deg=113.+(index*.0001 if moving else 0),latitude_deg=34.+(0.01 if moving else 0),
                        localization_time=dict(secs=1700000000+index*4,nsecs=0),horizontal_distance_m=10+index,
                        detection_count=index+1,tracking_success=True,score=.8)
                    (folder/('%s-%d.json'%(target,index))).write_text(json.dumps(raw),encoding='utf8')
            original={p.name:p.read_bytes() for p in folder.iterdir()}
            reporter=Subject1Reporter(root,publisher_dedup=True)
            built=reporter.build(mission,TEAM_NAME)
            prepared=reporter.prepare(built,already_deduplicated=True)
            final=json.loads(reporter.draft(prepared['draft_id']).read_text(encoding='utf8'))
            self.assertEqual({'固定':'人员2','移动':'人员3'},{f['properties']['targetCategory']:f['properties']['targetModel'] for f in final['features']})
            audit=json.loads((reporter.root/(prepared['draft_id']+'.format.json')).read_text(encoding='utf8'))
            self.assertEqual([12,17],audit['deduplication']['person_selection']['recognition_selection']['category_ids'])
            self.assertEqual(2,len(audit['deduplication']['person_selection']['remappings']))
            self.assertEqual(original,{p.name:p.read_bytes() for p in folder.iterdir()})

    def test_plan_api_records_per_mission_selection_without_restoring_ui(self):
        from fastapi.testclient import TestClient
        from competition_backend.api import create_app
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,COMPETITION_ADAPTER='sim',COMPETITION_DATA_DIR=tmp,COMPETITION_IMAGE_ROOT=tmp)
            client=TestClient(create_app(env))
            missions=[]
            for chosen in (selection(12,17),selection(14,15)):
                result=client.post('/api/v1/plan',json=dict(subject='subject1',recognition_selection=chosen))
                self.assertEqual(200,result.status_code,result.text[:2000])
                mid=result.json()['mission']['mission_id'];missions.append(mid)
                self.assertEqual(chosen['category_ids'],load_reporting_selection(tmp,mid)['category_ids'])
            self.assertEqual([12,17],load_reporting_selection(tmp,missions[0])['category_ids'])
            self.assertEqual([],client.get('/api/v1/recognition-categories').json()['selection']['category_ids'])
            with patch('competition_shared.reporting_selection.save_reporting_selection',side_effect=OSError('disk unavailable')):
                result=client.post('/api/v1/plan',json=dict(subject='subject1',recognition_selection=selection(12)))
            self.assertEqual(200,result.status_code,result.text[:2000])


if __name__=='__main__':unittest.main()
