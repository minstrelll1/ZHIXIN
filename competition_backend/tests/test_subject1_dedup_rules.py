"""发布端混淆组、首次分类之后的跨机去重和足量上报规则。"""
import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from competition_shared.subject1_dedup import consolidate, _pair_match
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
                self.assertEqual(count == 1, _pair_match(a, b)[0])
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
        ranks=[(r['detection_count'],r['tracking_success'],r['score']) for r in audit['quality_ranking']]
        self.assertEqual(sorted(ranks,reverse=True),ranks)
        self.assertTrue(all(not any(k.startswith('_') for k in f) for f in result['features']))
        self.assertEqual(16,validate_document(result)['target_count'])

    def test_static_final_order_is_count_then_success_then_confidence(self):
        values = [feature('count', east=100, count=100, success=False, score=.1),
                  feature('success', east=200, count=10, success=True, score=.2),
                  feature('score', east=300, count=10, success=False, score=.99),
                  feature('tie-high', east=400, count=5, success=True, score=.9),
                  feature('tie-low', east=500, count=5, success=True, score=.4)]
        result, audit = consolidate(document(list(reversed(values))))
        self.assertEqual([v['id'] for v in values], [v['id'] for v in result['features']])
        self.assertEqual(['detection_count', 'tracking_success', 'confidence'], audit['static_quality_order'])

    def test_selection_reserves_three_moving_people_three_cars_and_ten_static(self):
        values = [feature('s%d'%i, east=100*i, count=i, success=True) for i in range(12)]
        for kind in ('人员', '车辆'):
            values += [feature(kind+str(i), broad=kind, model=kind+'1', moving=True,
                               north=1000+i*100+(1000 if kind=='车辆' else 0), count=i,
                               score=.1, success=False) for i in range(5)]
        result, audit = consolidate(document(values))
        self.assertEqual(16, validate_document(result)['target_count'])
        self.assertEqual({'人员':3, '车辆':3}, audit['selection']['moving_selected_counts'])
        self.assertEqual(10, audit['selection']['static_selected_count'])
        self.assertEqual(0, audit['selection']['shortfall'])
        selected = {v['id'] for v in result['features']}
        self.assertTrue({'人员2', '人员3', '人员4', '车辆2', '车辆3', '车辆4'} <= selected)
        self.assertNotIn('s0', selected)
        self.assertNotIn('s1', selected)

    def test_missing_moving_slots_go_to_static_not_extra_cars(self):
        for people, cars, expected_people, expected_cars in ((0,2,0,2),(1,7,1,3),(0,0,0,0)):
            with self.subTest(people=people, cars=cars):
                values = [feature('s%d'%i, east=100*i) for i in range(16)]
                for kind, count in (('人员',people),('车辆',cars)):
                    values += [feature(kind+str(i), broad=kind, model=kind+'1', moving=True,
                                       north=1000+i*100+(1000 if kind=='车辆' else 0)) for i in range(count)]
                result, audit = consolidate(document(values))
                self.assertEqual(16, len(result['features']))
                self.assertEqual({'人员':expected_people, '车辆':expected_cars}, audit['selection']['moving_selected_counts'])
                self.assertEqual(16-expected_people-expected_cars, audit['selection']['static_selected_count'])

    def test_merged_moving_duplicates_do_not_fill_quota_or_static_slots(self):
        values = [feature('duplicate-car%d'%i, uid=i+1, moving=True, east=2000) for i in range(12)]
        values += [feature('s%d'%i, east=100*i) for i in range(4)]
        result, audit = consolidate(document(values))
        self.assertEqual(5, len(result['features']))
        self.assertEqual({'人员':0,'车辆':1}, audit['selection']['moving_selected_counts'])
        self.assertEqual(11, audit['selection']['shortfall'])
        self.assertEqual([], audit['backfilled'])

    def test_static_backfill_never_exceeds_moving_caps(self):
        values = [feature('s%d'%i, count=i) for i in range(18)]
        values += [feature('c%d'%i, moving=True, north=1000+i*100) for i in range(4)]
        result, audit = consolidate(document(values))
        self.assertEqual(16, len(result['features']))
        self.assertEqual(3, audit['selection']['moving_selected_counts']['车辆'])
        self.assertEqual(13, audit['selection']['static_selected_count'])
        self.assertEqual(12, audit['backfilled_count'])

    def test_quota_uses_submitted_type_not_internal_confusion_category(self):
        values = [feature('c%d'%i, moving=True, north=100*i) for i in range(5)]
        for i, value in enumerate(values):
            value['_dedup_category'] = '人员' if i%2 else '车辆'
        result, audit = consolidate(document(values))
        self.assertEqual(3, len(result['features']))
        self.assertEqual({'人员':0,'车辆':3}, audit['selection']['moving_selected_counts'])

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
            self.assertEqual(16, audit['deduplication']['selection']['static_selected_count'])
            self.assertEqual(18,len(audit['motion_classification']))
            self.assertTrue(all(a['dedup_category']=='车辆' for a in audit['category_records']))
            self.assertEqual(18,len(list(root.glob('UAV*/*/*.json'))))

    def test_publisher_motion_conversion_quota_format_and_freeze_use_same_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); mission = 'subject1-quota-pipeline'
            folder = root/'UAV2'/mission; folder.mkdir(parents=True)
            specs = [(str(i), False, 0, i*100, 0, i+1) for i in range(12)]
            specs += [('person%d'%i, True, 15, i*100, 1000, i+1) for i in range(4)]
            specs += [('car%d'%i, True, 0, i*100, 2000, i+1) for i in range(5)]
            for target, moving, cid, east, north, count in specs:
                for seconds in ((0,12,15) if moving else (0,)):
                    # 第4个人员目标80%的时间低速，转换后成为最高识别次数的静目标。
                    offset = (0 if seconds < 15 else 6) if target=='person3' else seconds*2
                    coords = point(east+offset, north)
                    raw = dict(mission_id=mission, global_id=target, category_id=cid,
                               category='人' if cid==15 else '车',
                               target_type='dsolider1' if cid==15 else 'vehicle1',
                               is_moving=moving, longitude_deg=coords[0], latitude_deg=coords[1],
                               detection_count=100 if target=='person3' else count,
                               tracking_success=False, confidence=.5,
                               detection_received_at_unix_ns=(1700000000+seconds)*1000000000,
                               localization_time=dict(secs=1700000000+seconds,nsecs=0))
                    (folder/('%s-%02d.json'%(target,seconds))).write_text(json.dumps(raw),encoding='utf-8')
            original = {p.name:p.read_bytes() for p in folder.iterdir()}
            reporter = Subject1Reporter(root,publisher_dedup=True)
            built = reporter.build(mission,TEAM_NAME)
            self.assertEqual(dict(target_count=16,fixed=10,moving=6),validate_document(built))
            self.assertEqual(['target-%03d'%i for i in range(1,17)],[f['id'] for f in built['features']])
            prepared = reporter.prepare(built,already_deduplicated=True)
            frozen = json.loads(reporter.draft(prepared['draft_id']).read_text(encoding='utf-8'))
            self.assertEqual(built['features'],frozen['features'])
            audit = json.loads((reporter.root/(prepared['draft_id']+'.format.json')).read_text(encoding='utf-8'))
            self.assertEqual({'人员':3,'车辆':3},audit['deduplication']['selection']['moving_selected_counts'])
            self.assertEqual('person3',audit['id_mapping'][0]['source_id'])
            self.assertEqual('固定',frozen['features'][0]['properties']['targetCategory'])
            self.assertEqual('人员1',frozen['features'][0]['properties']['targetModel'])
            converted, = [r for r in audit['motion_classification'] if r['converted_to_static']]
            self.assertEqual(.8,converted['low_speed_time_fraction'])
            self.assertEqual(original,{p.name:p.read_bytes() for p in folder.iterdir()})

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
