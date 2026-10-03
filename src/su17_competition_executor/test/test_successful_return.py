"""成功返航只读事件测试，不执行飞行控制。"""
import json
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import test_executor_autonomy as harness


class SuccessfulReturnTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.node=harness.AutonomyTest().executor(self.temp.name)
        self.node._assignment['controller_mode']='external'
        self.node._assignment['competition_time']=dict(schema_version=1,mission_id='test',running=True,
            synchronized=True,session_id='session',elapsed_seconds=100,revision=0,
            publisher_terminal_id=1,updated_by=1)
        self.node._assignment['assignment_checksum']=harness.assignment_checksum(self.node._assignment)
        self.node._resume_pending=False
        self.node.successful_return_pub=Mock()

    def send(self, **changes):
        n=self.node
        payload=dict(uav_id=3,mission_id='test',phase='return_descent',
                     assignment_checksum=n._assignment['assignment_checksum'])
        payload.update(changes)
        n._external_status_callback(types.SimpleNamespace(data=json.dumps(payload)))

    def test_only_explicit_current_task_descent_can_create_event(self):
        n=self.node
        for phase in ('returning','mission_timeout','completed','landing'):
            self.send(phase=phase)
            self.assertFalse(n._successful_return_snapshot())
        self.send(assignment_checksum='old')
        self.assertFalse(n._successful_return_snapshot())
        self.send(mission_id='old')
        self.assertFalse(n._successful_return_snapshot())
        self.send()
        event=n._successful_return_snapshot()
        self.assertEqual('return_descent',event['phase'])
        self.assertEqual('session',event['competition_session_id'])
        self.assertEqual('external_return_descent',n._progress['phase'])
        n._land.assert_not_called(); n._fly_to.assert_not_called()

    def test_duplicate_and_same_boot_restart_keep_one_event_no_flight_resume(self):
        n=self.node; self.send()
        first=n._successful_return_snapshot()['event_id']
        self.send()
        self.assertEqual(first,n._successful_return_snapshot()['event_id'])
        n._restore_progress()
        self.assertEqual(first,n._successful_return_snapshot()['event_id'])
        self.assertFalse(n._resume_pending)
        n._boot_id='another-boot'
        self.assertFalse(n._successful_return_snapshot())

    def test_before_takeoff_does_not_accept_an_event(self):
        n=self.node; n._progress['phase']='assigned'; n._home=None
        self.send()
        self.assertFalse(n._successful_return_snapshot())
        self.assertEqual('assigned',n._progress['phase'])

    def test_clock_revision_sync_changes_time_only_and_ignores_other_sessions(self):
        n=self.node
        n._competition_time_state=dict(n._assignment['competition_time'])
        n._competition_time_received_monotonic=100
        checksum=n._assignment['assignment_checksum']
        with patch.object(harness.module.time,'monotonic',return_value=110):
            state=dict(n._competition_time_state,elapsed_seconds=80,revision=1)
            n._sync_competition_time(state)
            self.assertEqual(80,n._competition_time_state['elapsed_seconds'])
            n._sync_competition_time(dict(state,elapsed_seconds=50))
            self.assertEqual(80,n._competition_time_state['elapsed_seconds'])
            n._sync_competition_time(dict(state,elapsed_seconds=200,session_id='other'))
            self.assertEqual(80,n._competition_time_state['elapsed_seconds'])
        self.assertEqual(checksum,n._assignment['assignment_checksum'])
        self.assertEqual(100,n._assignment['competition_time']['elapsed_seconds'])
        n._land.assert_not_called(); n._fly_to.assert_not_called()

    def test_clock_snapshot_survives_same_boot_restart(self):
        n=self.node
        n._competition_time_state=dict(n._assignment['competition_time'],elapsed_seconds=1400)
        n._competition_time_received_monotonic=123
        n._checkpoint('external_executing')
        n._competition_time_state=None
        n._restore_progress()
        self.assertEqual(1400,n._competition_time_state['elapsed_seconds'])
        self.assertEqual(123,n._competition_time_received_monotonic)


if __name__=='__main__': unittest.main()
