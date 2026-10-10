import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from competition_shared.localization_clock import SourceClockTracker
from competition_shared.target_time import load_time_references, map_localization_time
from su17_image_transfer.submission import update_subject1_submission

BOOT = '11111111-1111-4111-8111-111111111111'
GROUND = 1_800_000_000
NS = 1_000_000_000


def evidence(uid=1, boot=BOOT, source_epoch=1_400_000, event=100., received=101.):
    tracker = SourceClockTracker(boot)
    tracker.observe(90*NS, (source_epoch+90)*NS, (source_epoch+90)*NS)
    tracker.observe(round(received*NS), round((source_epoch+received)*NS), round((source_epoch+received)*NS))
    loc = dict(secs=source_epoch+int(event), nsecs=round((event-int(event))*NS))
    return dict(uav_id=uid, localization_time=loc, localization_clock=tracker.context(loc))


def reference(uid=1, boot=BOOT, mono=110., epoch=GROUND):
    return dict(boot_id=boot, uav_id=uid, ground_epoch=epoch, remote_monotonic=mono,
                uncertainty_sec=.01, sampled_at=epoch, source='publisher_windows', publisher_terminal_id=1)


class TargetTimeTest(unittest.TestCase):
    def test_maps_wrong_1970_rtc_preserving_raw_event_and_delayed_receipt(self):
        raw = evidence(event=100.125, received=200.)
        original = copy.deepcopy(raw)
        corrected, audit = map_localization_time(raw, 1, {BOOT: reference()})
        self.assertEqual(raw, original)
        self.assertEqual(dict(secs=GROUND-10,nsecs=125000000), corrected['localization_time'])
        self.assertEqual((GROUND+90)*NS, corrected['detection_received_at_unix_ns'])
        self.assertEqual('corrected', audit['status'])
        self.assertEqual(-9.875, audit['reference_distance_sec'])

    def test_reboot_simulation_jump_tamper_slow_and_missing_references_never_fabricate_time(self):
        base = evidence()
        bad_contexts = [dict(mapping_valid=False,reason='ros_clock_jump'),dict(use_sim_time=True),
                        dict(event_monotonic_ns=999*NS),dict(boot_id='other-boot'),
                        dict(sampling_uncertainty_ns=60_000_000),dict(source_ros_ns=True)]
        for change in bad_contexts:
            raw=copy.deepcopy(base);raw['localization_clock'].update(change)
            with self.subTest(change=change):
                result,audit=map_localization_time(raw,1,{BOOT:reference()})
                self.assertIsNone(result['localization_time']);self.assertEqual('pending',audit['status'])
        for ref in (reference(uid=2),reference(epoch=float('nan')),reference(mono=200000)):
            result,audit=map_localization_time(base,1,{BOOT:ref})
            self.assertIsNone(result['localization_time'])
        result,audit=map_localization_time(base,1,{})
        self.assertEqual('missing_boot_reference',audit['reason'])
        result,audit=map_localization_time(dict(base,uav_id=2),1,{BOOT:reference()})
        self.assertEqual('source_uav_mismatch',audit['reason'])
        legacy=dict(localization_time=dict(secs=GROUND,nsecs=0))
        result,audit=map_localization_time(legacy,1,{})
        self.assertEqual(legacy,result);self.assertEqual('unverified_legacy',audit['status'])

    def test_rebuild_after_reference_arrives_corrects_and_preserves_original_json_and_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'UAV1'/'subject1-clock';folder.mkdir(parents=True)
            raw=dict(evidence(),mission_id='subject1-clock',target_id='fixed',category_id=0,
                     latitude_deg=34.,longitude_deg=113.)
            source=folder/'capture.json';source.write_text(json.dumps(raw),encoding='utf-8')
            (folder/'capture.jpg').write_bytes(b'original-image')
            before=source.read_bytes()
            target=update_subject1_submission(root,'subject1-clock',publisher_dedup=True)
            self.assertEqual([],json.loads(target.read_text(encoding='utf-8'))['features'])
            (root/'time_references.json').write_text(json.dumps(dict(schema_version=1,references={BOOT:reference()})),encoding='utf-8')
            update_subject1_submission(root,'subject1-clock',publisher_dedup=True)
            doc=json.loads(target.read_text(encoding='utf-8'));feature,=doc['features']
            self.assertEqual('2027-01-15T15:59:50.000+08:00',feature['properties']['timestamp'])
            self.assertEqual(before,source.read_bytes())
            self.assertEqual(b'original-image',(target.parent/feature['properties']['imagePath']).read_bytes())
            audit=json.loads(target.with_name('submission-format.json').read_text(encoding='utf-8'))
            self.assertEqual(1,audit['target_time']['corrected_count'])
            self.assertEqual(raw['localization_time'],audit['target_time']['records'][0]['original_localization_time'])
            # 冻结上报仍保留绑定当前正文的时差证据。
            from competition_backend.subject1_reporting import Subject1Reporter
            reporter=Subject1Reporter(root,publisher_dedup=True)
            frozen=reporter.prepare(doc)
            saved=json.loads((reporter.root/(frozen['draft_id']+'.format.json')).read_text(encoding='utf-8'))
            self.assertEqual(audit['target_time'],saved['target_time'])

    def test_two_unsynchronized_uavs_are_mapped_before_moving_dedup_and_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);refs={}
            for uid,clock_epoch,confidence in ((1,1_400_000,.5),(2,GROUND+9999,.9)):
                boot=BOOT if uid==1 else '22222222-2222-4222-8222-222222222222'
                refs[boot]=reference(uid,boot)
                folder=root/('UAV%d'%uid)/'subject1-moving';folder.mkdir(parents=True)
                for i in range(3):
                    raw=dict(evidence(uid,boot,clock_epoch,event=100+i*3,received=107),
                        mission_id='subject1-moving',target_id='moving-%d'%uid,category_id=0,
                        is_moving=True,score=confidence,latitude_deg=34.,longitude_deg=113.+i*.0001)
                    (folder/('%d.json'%i)).write_text(json.dumps(raw),encoding='utf-8')
            (root/'time_references.json').write_text(json.dumps(dict(schema_version=1,references=refs)),encoding='utf-8')
            target=update_subject1_submission(root,'subject1-moving',publisher_dedup=True)
            doc=json.loads(target.read_text(encoding='utf-8'));feature,=doc['features']
            points=feature['properties']['trackPoints']
            self.assertEqual(3,len(points))
            epochs=[datetime.datetime.fromisoformat(p['timestamp']).timestamp() for p in points]
            self.assertEqual([GROUND-10,GROUND-7,GROUND-4],epochs)
            self.assertEqual(1,len(json.loads(target.with_name('dedup-decisions.json').read_text(encoding='utf-8'))['merged']))

    def test_static_latest_order_is_not_changed_by_onboard_rtc_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'UAV1'/'subject1-latest';folder.mkdir(parents=True)
            for i,clock_epoch in enumerate((GROUND,1_400_000)):
                raw=dict(evidence(source_epoch=clock_epoch,event=100+i,received=105+i),
                    detection_received_at_unix_ns=(clock_epoch+105+i)*NS,
                    mission_id='subject1-latest',target_id='fixed',category_id=0,
                    latitude_deg=34.,longitude_deg=113.+i*.001)
                (folder/('%d.json'%i)).write_text(json.dumps(raw),encoding='utf-8')
            (root/'time_references.json').write_text(json.dumps(dict(schema_version=1,references={BOOT:reference()})),encoding='utf-8')
            target=update_subject1_submission(root,'subject1-latest',publisher_dedup=True)
            feature,=json.loads(target.read_text(encoding='utf-8'))['features']
            self.assertEqual([113.001,34.],feature['geometry']['coordinates'])

    def test_latest_invalid_static_feedback_does_not_fall_back_to_old_corrected_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'UAV1'/'subject1-invalid-latest';folder.mkdir(parents=True)
            for i in range(2):
                raw=dict(evidence(event=100+i,received=105+i),
                    detection_received_at_unix_ns=(1_400_000+105+i)*NS,
                    mission_id='subject1-invalid-latest',target_id='fixed',category_id=0,
                    latitude_deg=34.,longitude_deg=113.+i*.001)
                if i:
                    raw['localization_clock'].update(mapping_valid=False,reason='ambiguous_previous_segment')
                (folder/('%d.json'%i)).write_text(json.dumps(raw),encoding='utf-8')
            (root/'time_references.json').write_text(json.dumps(dict(schema_version=1,references={BOOT:reference()})),encoding='utf-8')
            target=update_subject1_submission(root,'subject1-invalid-latest',publisher_dedup=True)
            self.assertEqual([],json.loads(target.read_text(encoding='utf-8'))['features'])
            audit=json.loads(target.with_name('submission-format.json').read_text(encoding='utf-8'))
            self.assertEqual(1,audit['target_time']['pending_count'])
            self.assertEqual(2,len(list(folder.glob('*.json'))))

    def test_invalid_reference_file_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual('missing_reference_file',load_time_references(root)[1])
            (root/'time_references.json').write_text('[]',encoding='utf-8')
            self.assertEqual('invalid_reference_file',load_time_references(root)[1])


if __name__ == '__main__':
    unittest.main()
