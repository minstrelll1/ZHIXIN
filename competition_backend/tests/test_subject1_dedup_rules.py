"""发布端混淆组、首次分类之后的跨机去重和足量上报规则。"""
import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from competition_shared.subject1_dedup import consolidate
from competition_backend.subject1_reporting import Subject1Reporter, validate_document, TEAM_NAME


def point(east, north=0.):
    return [113.+east/92100.,34.+north/111195.]


def feature(identifier, uid=1, broad='车辆', model='车辆1', east=0., north=0., moving=False,
            count=1, score=.5, success=False, track_points=5, reverse=False):
    props=dict(targetCategory='移动' if moving else '固定',targetType=broad,targetModel=model,confidence=score)
    item=dict(type='Feature',id=identifier,properties=props,_source_uav='UAV%d'%uid,
              _quality=dict(tracking_success=success,detection_count=count))
    if moving:
        points=[]
        for i in range(track_points):
            position=point(east+(track_points-1-i if reverse else i)*5,north)
            stamp=datetime.datetime.fromtimestamp(1700000000+i*3,datetime.timezone.utc).isoformat()
            points.append(dict(coordinates=position,timestamp=stamp))
        props.update(trackPoints=points,trackStartTime=points[0]['timestamp'],trackEndTime=points[-1]['timestamp'])
        item['geometry']=dict(type='LineString',coordinates=[p['coordinates'] for p in points])
    else:
        props['timestamp']='2023-11-15T06:13:20+08:00'
        item['geometry']=dict(type='Point',coordinates=point(east,north))
    return item


def document(features):
    return dict(type='FeatureCollection',name=TEAM_NAME,features=features,
                crs=dict(type='lonlat',properties=dict(lonlat='EPSG:4326')),
                metadata=dict(version='1.0',createdAt='2026-10-10T00:00:00Z',coordinateSystem='WGS84'))


class DedupRulesTest(unittest.TestCase):
    def test_static_same_broad_different_models_ten_meters_other_groups_five(self):
        for broad,model,distance,count in [('车辆','车辆7',8,1),('人员','人员2',4,1),
                                            ('工事','工事4',6,2)]:
            with self.subTest(broad=broad,distance=distance):
                result,audit=consolidate(document([feature('a',1),feature('b',2,broad,model,east=distance)]))
                self.assertEqual(count,len(result['features']))
                if count==1:
                    self.assertEqual(10. if broad=='车辆' else 5.,audit['merged'][0]['distance_limit_m'])

    def test_actual_category_overrides_model_for_dedup_only(self):
        a=feature('a',1,broad='车辆');b=feature('b',2,broad='工事',east=8)
        a['_dedup_category']='人';b['_dedup_category']='人员'
        result,audit=consolidate(document([a,b]))
        self.assertEqual(1,len(result['features']))
        self.assertEqual('人员',audit['merged'][0]['target_type'])
        self.assertNotIn('_dedup_category',result['features'][0])
        self.assertEqual('车辆',a['properties']['targetType'])

    def test_pairwise_threshold_and_same_uav_inclusion(self):
        values=[feature('a',1,'人员','人员1',east=0,count=30),
                feature('b',2,'人员','人员4',east=8,count=20),
                feature('c',3,'工事','工事1',east=0,count=10)]
        result,audit=consolidate(document(values))
        self.assertEqual(2,len(result['features']))
        self.assertEqual(1,len(audit['merged']))
        result,audit=consolidate(document([feature('same-plane-a',1),feature('same-plane-b',1,east=1)]))
        self.assertEqual(1,len(result['features']))
        self.assertTrue(audit['merged'][0]['same_uav'])
        result,_=consolidate(document([feature('same-plane-a',1),feature('same-plane-b',1,east=11)]))
        self.assertEqual(2,len(result['features']))

    def test_moving_person_vehicle_confusion_requires_similar_tracks(self):
        a=feature('a',1,'人员','运动的人员1',moving=True)
        for broad,distance,reverse,count in [('车辆',8,False,1),('工事',8,False,2),
                                           ('工事',4,False,1),('车辆',0,True,2)]:
            with self.subTest(broad=broad,distance=distance,reverse=reverse):
                b=feature('b',2,broad,broad+'1',north=distance,moving=True,reverse=reverse)
                result,_=consolidate(document([a,b]));self.assertEqual(count,len(result['features']))
        static=feature('static',2,'人员','人员1')
        result,_=consolidate(document([a,static]));self.assertEqual(2,len(result['features']))

    def test_moving_longest_track_and_earliest_forty_remain(self):
        a=feature('a',1,moving=True,track_points=42,count=2)
        b=feature('b',2,moving=True,track_points=45,count=3)
        result,audit=consolidate(document([a,b]));winner,=result['features']
        self.assertEqual('b',winner['id']);self.assertEqual(40,len(winner['properties']['trackPoints']))
        self.assertEqual(b['properties']['trackPoints'][:40],winner['properties']['trackPoints'])
        self.assertEqual(5,audit['track_truncations'][0]['omitted_track_points'])

    def test_more_than_sixteen_backfills_real_merged_candidates_and_preserves_sources(self):
        candidates=[feature('%d-%d'%(group,uid),uid,east=group*100,count=uid,
                            success=uid==6,score=.1*uid) for group in range(3) for uid in range(1,7)]
        raw=document(candidates);original=copy.deepcopy(raw)
        result,audit=consolidate(raw)
        self.assertEqual(raw,original)
        self.assertEqual(18,audit['raw_count']);self.assertEqual(3,audit['deduplicated_count'])
        self.assertEqual(16,len(result['features']));self.assertEqual(13,len(audit['backfilled']))
        self.assertEqual(16,len({f['id'] for f in result['features']}))
        self.assertTrue({f['id'] for f in result['features']} <= {f['id'] for f in candidates})
        ranks=[(r['tracking_success'],r['detection_count'],r['score']) for r in audit['quality_ranking']]
        self.assertEqual(sorted(ranks,reverse=True),ranks)
        self.assertTrue(all(not any(k.startswith('_') for k in f) for f in result['features']))
        self.assertEqual(16,validate_document(result)['target_count'])

    def test_no_padding_when_fewer_than_sixteen_candidates(self):
        result,audit=consolidate(document([feature('a',1),feature('b',2)]))
        self.assertEqual(1,len(result['features']));self.assertEqual([],audit['backfilled'])

    def test_publisher_raw_collection_build_and_frozen_audit_keep_sixteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);mission='subject1-sixteen'
            for uid in range(1,7):
                folder=root/('UAV%d'%uid)/mission;folder.mkdir(parents=True)
                for group in range(3):
                    raw=dict(mission_id=mission,global_id='target-%d'%group,category='车',
                        category_id=(uid-1)%7,target_type='vehicle%d'%uid,is_moving=False,
                        latitude_deg=34.,longitude_deg=113.+group*.002,
                        localization_time=dict(secs=1700000000+uid,nsecs=0),
                        detection_received_at_unix_ns=(1700000000+uid)*1000000000,
                        tracking_success=uid==6,detection_count=uid,confidence=.1*uid)
                    (folder/('%d.json'%group)).write_text(json.dumps(raw),encoding='utf-8')
            reporter=Subject1Reporter(root,publisher_dedup=True)
            built=reporter.build(mission,TEAM_NAME)
            self.assertEqual(16,len(built['features']))
            prepared=reporter.prepare(built,already_deduplicated=True)
            audit=json.loads((reporter.root/(prepared['draft_id']+'.format.json')).read_text(encoding='utf-8'))
            self.assertEqual(18,audit['deduplication']['raw_count'])
            self.assertEqual(3,audit['deduplication']['deduplicated_count'])
            self.assertEqual(13,audit['deduplication']['backfilled_count'])
            self.assertEqual(18,len(audit['motion_classification']))
            self.assertTrue(all(a['dedup_category']=='车辆' for a in audit['category_records']))
            self.assertEqual(18,len(list(root.glob('UAV*/*/*.json'))))

    def test_manual_import_freeze_and_reimport_do_not_deduplicate_backfill_again(self):
        values=[feature('target-%02d'%i,(i%6)+1,count=i,east=(i//6)*100) for i in range(18)]
        for item in values:
            item.pop('_source_uav');item.pop('_quality')
        with tempfile.TemporaryDirectory() as tmp:
            reporter=Subject1Reporter(tmp,publisher_dedup=True)
            prepared=reporter.prepare(document(values));self.assertEqual(16,prepared['target_count'])
            frozen=json.loads(reporter.draft(prepared['draft_id']).read_text(encoding='utf-8'))
            self.assertEqual(16,reporter.prepare(frozen)['target_count'])
            decisions=json.loads((reporter.root/(prepared['draft_id']+'.dedup.json')).read_text(encoding='utf-8'))
            self.assertEqual(13,decisions['backfilled_count'])
            # 已补齐的旧文件可能在另一台发布端导入，仍应维持16个真实候选。
            with tempfile.TemporaryDirectory() as other:
                imported=Subject1Reporter(other,publisher_dedup=True)
                frozen['name']='重新确认的队名'
                self.assertEqual(16,imported.prepare(frozen)['target_count'])



if __name__=='__main__':
    unittest.main()
