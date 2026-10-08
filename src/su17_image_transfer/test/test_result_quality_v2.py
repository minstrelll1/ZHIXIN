import json
import tempfile
import unittest
from pathlib import Path
from competition_shared.target_quality import category_fields
from su17_image_transfer.submission import update_subject1_submission


class ResultQualityTest(unittest.TestCase):
    def test_all_nineteen_classes(self):
        for cid in range(19):
            kind, model = category_fields({"category_id":cid})
            self.assertIn(kind,["车辆","人员","工事"])
            self.assertNotIn("类别",model)
        self.assertEqual(category_fields({"target_type":"solider3"}), ("人员","人员3"))
        self.assertEqual(category_fields({"target_type":"building3"}), ("工事","工事3"))
        self.assertEqual(category_fields({"target_type":"dsolider4"}), ("人员","运动的人员4"))

    def test_success_then_count_then_score_and_latest_same_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); mission="subject1-quality"
            def add(uid, name, target, success, count, score, lon, sequence=1):
                p=root/('UAV%d'%uid)/mission;p.mkdir(parents=True,exist_ok=True)
                data=dict(mission_id=mission,target_id=target,category_id=13,target_type='solider3',
                          tracking_success=success,detection_count=count,confidence=score,is_moving=False,
                          target_latitude=34.,target_longitude=lon,image_stamp=dict(secs=1700000000,nsecs=0),
                          sequence=sequence,requested_at_unix_ns=1700000000000000000+sequence)
                (p/(name+'.json')).write_text(json.dumps(data),encoding='utf-8')
            add(1,'a','unsuccessful',False,999,.99,113.0)
            add(2,'b','successful',True,10,.1,113.00001)
            add(3,'c','more-observations',True,20,.05,113.00002)
            add(4,'d','separate',False,200,.99,113.001)
            add(4,'e','separate',False,300,.3,113.001,2)
            p=update_subject1_submission(root,mission,publisher_dedup=True)
            d=json.loads(p.read_text(encoding='utf-8'));f=d['features']
            self.assertEqual([v['id'] for v in f],['more-observations','separate'])
            self.assertEqual(f[1]['properties']['confidence'],.3)
            self.assertEqual(f[0]['properties']['targetType'],'人员')
            self.assertEqual(f[0]['properties']['targetModel'],'人员3')
            self.assertTrue(all('_quality' not in v and '_source_uav' not in v for v in f))
            decisions=json.loads((p.parent/'dedup-decisions.json').read_text(encoding='utf-8'))
            self.assertEqual(decisions['quality_ranking'][0]['detection_count'],20)
            self.assertEqual(decisions['quality_ranking'][1]['detection_count'],300)

    def test_moving_fortieth_success_and_location_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);mission='subject1-moving';p=root/'UAV1'/mission;p.mkdir(parents=True)
            for i in range(42):
                d=dict(mission_id=mission,target_id='car',target_type='vehicle7',category_id=6,
                       is_moving=True,tracking_success=i>=39,detection_count=i+1,confidence=.4,
                       target_latitude=34.,target_longitude=113.+i*.00001,
                       image_stamp=dict(secs=1700000000,nsecs=0),
                       localization_time=dict(secs=1700000000+i*3,nsecs=0),sequence=i+1)
                (p/('%03d.json'%i)).write_text(json.dumps(d),encoding='utf-8')
            output=update_subject1_submission(root,mission,publisher_dedup=True)
            d=json.loads(output.read_text(encoding='utf-8'));f=d['features'][0]
            self.assertEqual(len(f['properties']['trackPoints']),40)
            self.assertEqual(f['geometry']['coordinates'][-1][0],113.+39*.00001)
            audit=json.loads((output.parent/'dedup-decisions.json').read_text(encoding='utf-8'))
            self.assertTrue(audit['quality_ranking'][0]['tracking_success'])
