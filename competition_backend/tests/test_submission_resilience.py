"""Fault injection and repeated multi-aircraft result consolidation, offline only."""
import concurrent.futures
import json
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from competition_backend.subject1_reporting import validate_document
from su17_image_transfer.submission import update_subject1_submission


class SubmissionResilienceTest(unittest.TestCase):
    def record(self,root,uid,name,index=0,moving=False,category=0,east=0):
        folder=root/('UAV%d'%uid)/'subject1-resilience';folder.mkdir(parents=True,exist_ok=True)
        path=folder/(name+'.json')
        data=dict(mission_id='subject1-resilience',target_id=name.split('-sample-')[0],is_moving=moving,
            category_id=category,category='人' if category>=11 else '车',score=.7,
            detection_count=index+1,tracking_success=index>=39,
            longitude_deg=113.+east/92000.,latitude_deg=34.+uid*.001,
            localization_time=dict(secs=1791676800+index*3,nsecs=0),
            detection_received_at_unix_ns=(1791676800+index*3)*10**9)
        temporary=path.with_suffix('.part');temporary.write_text(json.dumps(data),encoding='utf-8');temporary.replace(path)
        return path

    def test_image_copy_failure_does_not_block_json_and_later_rebuild_repairs_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=self.record(root,1,'static');image=source.with_suffix('.jpg')
            image.write_bytes(b'image-for-copy-test')
            original=source.read_bytes()
            with patch('su17_image_transfer.submission.shutil.copy2',side_effect=PermissionError('image held by another process')):
                output=update_subject1_submission(root,'subject1-resilience',publisher_dedup=True)
            document=json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(validate_document(document)['target_count'],1)
            self.assertNotIn('imagePath',document['features'][0]['properties'])
            audit=json.loads(output.with_name('submission-format.json').read_text(encoding='utf-8'))
            self.assertEqual(len(audit['image_copy_errors']),1)
            self.assertIn('static.jpg',audit['image_copy_errors'][0]['source_image'])
            update_subject1_submission(root,'subject1-resilience',publisher_dedup=True)
            document=json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual((output.parent/document['features'][0]['properties']['imagePath']).read_bytes(),image.read_bytes())
            self.assertEqual(source.read_bytes(),original)

    @unittest.skipUnless(os.name == 'nt', 'Windows byte lock')
    def test_windows_lock_contention_retries_but_other_errors_surface(self):
        import msvcrt
        from su17_image_transfer.submission import _submission_lock
        original=msvcrt.locking
        attempts=[]
        def transient(fd,mode,count):
            if mode==msvcrt.LK_NBLCK:
                attempts.append(mode)
                if len(attempts)<3:raise OSError(errno.EACCES,'locked by receiver')
            return original(fd,mode,count)
        with tempfile.TemporaryDirectory() as directory:
            with patch('msvcrt.locking',side_effect=transient), patch('su17_image_transfer.submission.time.sleep'):
                with _submission_lock(Path(directory)):pass
            self.assertEqual(len(attempts),3)
            with patch('msvcrt.locking',side_effect=OSError(errno.EINVAL,'invalid lock')):
                with self.assertRaises(OSError):
                    with _submission_lock(Path(directory)):pass

    def test_repeated_concurrent_six_aircraft_updates_bad_files_and_forty_point_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for uid in range(1,7):
                for number in range(3):self.record(root,uid,'static-%d'%number,east=number*80)
                for index in range(45):self.record(root,uid,'moving-sample-%02d'%index,index,moving=True,
                    category=15 if uid<=3 else 0,east=400+index*6)
            folder=root/'UAV1'/'subject1-resilience'
            (folder/'partial.json').write_text('{"mission_id":',encoding='utf-8')
            (folder/'wrong-shape.json').write_text('[]',encoding='utf-8')
            bad=self.record(root,1,'invalid-position');data=json.loads(bad.read_text(encoding='utf-8'))
            data['longitude_deg']=float('nan');bad.write_text(json.dumps(data),encoding='utf-8')
            count_before=len(list(root.glob('UAV*/*/*.json')))
            def rebuild(_):
                output=update_subject1_submission(root,'subject1-resilience',publisher_dedup=True)
                value=json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(validate_document(value)['target_count'],16)
                json.dumps(value,allow_nan=False)
                moving=[f for f in value['features'] if f['properties']['targetCategory']=='移动']
                self.assertEqual(len(moving),6)
                for feature in moving:self.assertEqual(len(feature['properties']['trackPoints']),40)
                self.assertEqual([f['id'] for f in value['features']],['target-%03d'%i for i in range(1,17)])
            # Repeated rebuilds compete for the real Windows/POSIX mission file lock.
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(rebuild,range(30)))
            # More returns arrive after previous results exist; latest static data must overwrite.
            for index in range(10):
                for uid in range(1,7):self.record(root,uid,'static-0',index=50+index,east=index*.1)
                rebuild(index)
            self.assertEqual(len(list(root.glob('UAV*/*/*.json'))),count_before)
            self.assertFalse(list(root.rglob('*.part')))

if __name__=='__main__':unittest.main()
