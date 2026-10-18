"""距离择优只更新定位来源；保存原始证据、分类和累计识别质量。"""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

from test_target_first_kind import FirstKindTest


class NearestPositionTest(unittest.TestCase):
    add = FirstKindTest.add
    build = FirstKindTest.build

    def test_ros_distance_survives_metadata_tcp_and_rejects_invalid_values(self):
        import io
        from su17_image_transfer.frame_cache import completed_target_metadata
        from su17_image_transfer.protocol import encode_frame, receive_frame
        stamp=NS(secs=1700000000,nsecs=0)
        target=NS(header=NS(stamp=stamp),global_id='target_001',detection_count=8,tracking_success=True,
            target_type='vehicle1',timestamp=stamp,image_stamp=stamp,localization_time=stamp,
            cx=.5,cy=.4,w=.18,h=.22,score=.96,category_id=0,speed_mps=0.,horizontal_distance_m=18.7,
            category='车',is_moving=False,indoor_position=False,latitude_deg=34.,longitude_deg=113.,altitude_gps_m=105.2)
        class Socket:
            def __init__(self,raw): self.stream=io.BytesIO(raw)
            def recv(self,n):return self.stream.read(n)
        _,metadata=completed_target_metadata(target)
        transferred,_=receive_frame(Socket(encode_frame(metadata,b'jpeg')))
        self.assertEqual(transferred['horizontal_distance_m'],18.7)
        self.assertEqual(transferred['target_id'],'target_001')
        for value in (None,-1.,float('nan'),float('inf'),True,'bad'):
            target.horizontal_distance_m=value
            _,metadata=completed_target_metadata(target)
            self.assertNotIn('horizontal_distance_m',metadata)
            json.dumps(metadata,allow_nan=False)
        del target.horizontal_distance_m
        self.assertNotIn('horizontal_distance_m',completed_target_metadata(target)[1])
        target.horizontal_distance_m=0.
        self.assertEqual(0.,completed_target_metadata(target)[1]['horizontal_distance_m'])

    def test_reporter_freeze_preserves_selected_position_audit(self):
        from competition_backend.subject1_reporting import Subject1Reporter, TEAM_NAME
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.add(root,'near',receipt=1,horizontal_distance_m=5.)
            self.add(root,'far',receipt=2,horizontal_distance_m=20.,longitude=113.0001)
            reporter=Subject1Reporter(root,publisher_dedup=True)
            doc=reporter.build('subject1-first-kind',TEAM_NAME)
            frozen=reporter.prepare(doc,already_deduplicated=True)
            audit=json.loads((reporter.root/(frozen['draft_id']+'.format.json')).read_text(encoding='utf8'))
            self.assertEqual(5.,audit['static_position_selection'][0]['horizontal_distance_m'])
            self.assertEqual('same-id',audit['static_position_selection'][0]['target_id'])

    def test_static_and_converted_static_use_nearest_position_but_latest_quality(self):
        for moving in (False, True):
            with self.subTest(moving=moving), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                self.add(root,'near',moving=moving,receipt=1,stamp=1700000000,
                    horizontal_distance_m=5.,detection_count=1,tracking_success=False,confidence=.3)
                self.add(root,'middle',moving=moving,receipt=2,stamp=1700000005,
                    longitude=113.000001,horizontal_distance_m=10.,detection_count=4)
                self.add(root,'latest',moving=moving,receipt=3,stamp=1700000010,
                    longitude=113.000002,horizontal_distance_m=20.,detection_count=8,tracking_success=True,confidence=.9)
                files={p:p.read_bytes() for p in (root/'UAV1').rglob('*') if p.is_file()}
                path,doc,audit=self.build(root)
                feature,=doc['features'];props=feature['properties']
                self.assertEqual('固定',props['targetCategory'])
                self.assertEqual([113.,34.],feature['geometry']['coordinates'])
                self.assertEqual('2023-11-15T06:13:20.000+08:00',props['timestamp'])
                self.assertTrue(props['imagePath'].endswith('_near.jpg'))
                self.assertEqual(.9,props['confidence'])
                decision=json.loads(path.with_name('dedup-decisions.json').read_text(encoding='utf8'))
                self.assertEqual(8,decision['quality_ranking'][0]['detection_count'])
                self.assertTrue(decision['quality_ranking'][0]['tracking_success'])
                self.assertEqual(5.,audit['static_position_selection'][0]['horizontal_distance_m'])
                self.assertEqual(moving,audit['static_position_selection'][0]['converted_to_static'])
                self.assertEqual(files,{p:p.read_bytes() for p in files})

    def test_equal_distance_uses_latest_receipt_not_arrival_and_missing_image_stays_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.add(root,'z-new',receipt=2,stamp=1700000010,longitude=113.0001,horizontal_distance_m=0.,image=False)
            self.add(root,'a-delayed',receipt=1,stamp=1700000000,horizontal_distance_m=0.)
            _,doc,audit=self.build(root)
            feature,=doc['features']
            self.assertEqual([113.0001,34.],feature['geometry']['coordinates'])
            self.assertNotIn('imagePath',feature['properties'])
            self.assertEqual('minimum_horizontal_distance',audit['static_position_selection'][0]['rule'])

    def test_invalid_closest_and_missing_distance_do_not_override_valid_nearest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.add(root,'valid',receipt=1,horizontal_distance_m=5.)
            for index,fields in enumerate((dict(horizontal_distance_m=-1.),dict(horizontal_distance_m=float('nan')),
                    dict(horizontal_distance_m=True),dict(horizontal_distance_m=1.,target_longitude=181.),
                    dict(horizontal_distance_m=1.,localization_time=dict(secs=0,nsecs=0)),{}),2):
                self.add(root,'invalid'+str(index),receipt=index,longitude=113.001,**fields)
            _,doc,_=self.build(root)
            self.assertEqual([113.,34.],doc['features'][0]['geometry']['coordinates'])

    def test_late_nearer_record_rebuilds_selection_and_other_uav_same_id_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.add(root,'first',uid=1,receipt=1,horizontal_distance_m=8.)
            self.add(root,'other',uid=2,receipt=1,longitude=114.,horizontal_distance_m=1.)
            self.build(root)
            self.add(root,'delayed',uid=1,receipt=2,stamp=1700000005,longitude=113.00001,horizontal_distance_m=2.)
            _,doc,audit=self.build(root)
            self.assertEqual({113.00001,114.},{f['geometry']['coordinates'][0] for f in doc['features']})
            self.assertEqual(2,len(audit['static_position_selection']))

    def test_still_moving_retains_full_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index in range(3):
                self.add(root,str(index),moving=True,receipt=index+1,stamp=1700000000+index*5,
                    longitude=113.+index*.001,horizontal_distance_m=10.-index)
            _,doc,audit=self.build(root)
            self.assertEqual('LineString',doc['features'][0]['geometry']['type'])
            self.assertEqual(3,len(doc['features'][0]['geometry']['coordinates']))
            self.assertEqual([],audit['static_position_selection'])


if __name__=='__main__':unittest.main()
