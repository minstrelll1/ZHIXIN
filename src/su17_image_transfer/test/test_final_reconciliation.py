"""最终补传门控测试：不访问飞机，不需要 ROS/OpenCV。"""
import ast
import json
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
from competition_shared.return_events import matches_return_event, competition_elapsed

ROOT = Path(__file__).resolve().parents[1]


def sender_type():
    names = {'_start_mission', '_competition_time_callback', '_successful_return_callback', '_save_final_reconciliation_locked',
             '_reconcile_timer_callback', '_schedule_reconciliation', '_reconcile_worker',
             '_mark_image_acked', '_retry_pending_delivery', '_schedule_pending_delivery',
             '_load_cached_metadata'}
    tree = ast.parse((ROOT/'scripts/onboard_image_sender.py').read_text(encoding='utf8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    scope = dict(json=json, os=os, time=time, threading=threading, String=object, rospy=Mock(), re=re,
                 safe_component=lambda value,fallback:value or fallback,
                 matches_return_event=matches_return_event, competition_elapsed=competition_elapsed,
                 encode_frame=lambda metadata, jpeg: (metadata, jpeg))
    exec(compile(ast.Module(body=[f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name in names],
                            type_ignores=[]), '<image-final>', 'exec'), scope)
    return type('Sender', (), {n: scope[n] for n in names})


class FinalReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sender = n = sender_type()()
        n.mission_lock=threading.Lock()
        n.active_mission_id='subject1-a'; n.uav_id=1; n.auth_token=''
        n.enable_offline_recovery=True
        n.final_reconcile_trigger=''; n.final_reconcile_done=False
        n.reconcile_in_progress=False; n.reconcile_done=False
        n.recovery_pending=False; n.recovery_revision=0
        n._successful_return={}; n._competition_time={}; n._competition_time_received=0
        n._recovery_in_progress=False; n._last_recovery_attempt=0; n._announce_pending=False
        n.last_reconcile_attempt=0; n.reconcile_retry_sec=30; n.reconcile_after_minutes=23.5
        n.live_retry_interval=5; n.stop_event=threading.Event()
        n.mission_started_at_unix_ns=123
        n._mission_cache_dir=lambda mission:self.root
        n._publish_status=Mock(); n._schedule_pending_delivery=Mock()
        n._schedule_reconciliation=Mock()

    def event(self, **changes):
        return dict(dict(phase='return_descent', mission_id='subject1-a',uav_id=1,event_id='unique'), **changes)

    def clock(self, elapsed):
        self.sender._competition_time_callback(types.SimpleNamespace(data=json.dumps(dict(
            running=True,mission_id='subject1-a',session_id='session',elapsed_seconds=elapsed))))

    def test_each_aircraft_and_mission_isolated_no_ordinary_landing_trigger(self):
        n=self.sender
        for event in (self.event(uav_id=2),self.event(mission_id='old'),self.event(phase='landing')):
            n._successful_return_callback(types.SimpleNamespace(data=json.dumps(event)))
        n._schedule_reconciliation.assert_not_called()
        n._successful_return_callback(types.SimpleNamespace(data=json.dumps(self.event())))
        self.assertEqual('successful_return',n.final_reconcile_trigger)
        n._schedule_reconciliation.assert_called_once_with(force=False,final=True)

    def test_clock_is_competition_time_and_or_gate_persists_only_one(self):
        n=self.sender
        self.clock(1400); n._reconcile_timer_callback(None)
        n._schedule_reconciliation.assert_not_called()
        self.clock(1410); n._reconcile_timer_callback(None)
        self.assertEqual('competition_23m30s',n.final_reconcile_trigger)
        n.final_reconcile_done=True; n._save_final_reconciliation_locked()
        n._schedule_reconciliation.reset_mock()
        n._successful_return_callback(types.SimpleNamespace(data=json.dumps(self.event())))
        self.clock(1500); n._reconcile_timer_callback(None)
        n._schedule_reconciliation.assert_not_called()
        stored=json.loads((self.root/'.final_reconciliation.json').read_text())
        self.assertTrue(stored['done'])
        self.assertEqual('competition_23m30s',stored['trigger'])

    def test_unknown_clock_no_image_mission_time_fallback_and_failure_retries_same_job(self):
        n=self.sender
        n.mission_started_monotonic=-999999
        n._reconcile_timer_callback(None)
        n._schedule_reconciliation.assert_not_called()
        n.final_reconcile_trigger='successful_return'
        n._reconcile_once=Mock(side_effect=TimeoutError('断连'))
        n._reconcile_worker('subject1-a',0,True,'successful_return')
        self.assertFalse(n.final_reconcile_done)
        n._reconcile_once=Mock(return_value=(3,1))
        n._reconcile_worker('subject1-a',0,True,'successful_return')
        self.assertTrue(n.final_reconcile_done)
        self.assertEqual('successful_return',n.final_reconcile_trigger)

    def test_node_restart_restores_completed_final_job(self):
        n=self.sender
        n.final_reconcile_trigger='successful_return';n.final_reconcile_done=True
        n._save_final_reconciliation_locked()
        n.active_mission_id='';n.final_reconcile_trigger='';n.final_reconcile_done=False
        n.cache_root=self.root.parent;n._new_mission_id=lambda:'fallback';n._announce_mission=Mock()
        n._start_mission('subject1-a')
        self.assertEqual('successful_return',n.final_reconcile_trigger)
        self.assertTrue(n.final_reconcile_done)
        self.clock(1450);n._reconcile_timer_callback(None)
        n._schedule_reconciliation.assert_not_called()

    def test_final_job_waits_for_rendered_images_and_new_failure_revision(self):
        n=self.sender; n.final_reconcile_trigger='successful_return'
        n.detection_jobs=queue.Queue(); n.detection_jobs.put('rendering')
        n._reconcile_once=Mock(return_value=(3,1))
        n._reconcile_worker('subject1-a',0,True,'successful_return')
        n._reconcile_once.assert_not_called(); self.assertFalse(n.final_reconcile_done)
        n.detection_jobs.get(); n.detection_jobs.task_done()
        n.recovery_revision=1
        n._reconcile_worker('subject1-a',0,True,'successful_return')
        self.assertFalse(n.final_reconcile_done)
        n._reconcile_worker('subject1-a',1,True,'successful_return')
        self.assertTrue(n.final_reconcile_done)

    def test_network_recovery_retries_unacknowledged_files_without_manifest(self):
        n=self.sender
        for name in ('old','missing'):
            (self.root/(name+'.jpg')).write_bytes(b'image')
            (self.root/(name+'.json')).write_text(json.dumps(dict(mission_id='subject1-a',file_name=name+'.jpg')))
        (self.root/'old.acked').touch()
        n._send_packet_with_ack=Mock(); n._exchange_control=Mock()
        n._retry_pending_delivery('subject1-a',0)
        n._send_packet_with_ack.assert_called_once()
        self.assertEqual('missing.jpg',n._send_packet_with_ack.call_args.args[0][0]['file_name'])
        self.assertTrue((self.root/'missing.acked').exists())
        n._exchange_control.assert_not_called()


if __name__=='__main__':unittest.main()
