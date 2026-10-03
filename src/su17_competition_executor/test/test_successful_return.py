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
        self.node.external_follower_node='/uav3/trajectory_follower'
        self.addCleanup(patch.stopall)
        patch.object(harness.module.rospy, 'Time', types.SimpleNamespace(
            now=lambda:types.SimpleNamespace(to_sec=lambda:101.0)), create=True).start()
        patch.object(harness.module.time, 'monotonic', return_value=100.0).start()
        patch.object(harness.module.UAVCommand, 'Land', 3, create=True).start()
        patch.object(harness.module.UAVControlState, 'LAND_CONTROL', 3, create=True).start()

    def arm_observer(self):
        n=self.node
        n._progress['phase']='external_waiting'
        n._progress['external_return_watch']=dict(boot_id=n._boot_id, mission_id='test',
            assignment_checksum=n._assignment['assignment_checksum'], started_ros=100.0)
        n._state=types.SimpleNamespace(connected=True,armed=True,uav_id=3)
        n._state_received=n._control_received=100.0
        n._control=types.SimpleNamespace(control_state=2,failsafe=False,uav_id=3)

    def land_command(self, caller='/uav3/trajectory_follower', stamp=101.0, command=3, latch='0'):
        return types.SimpleNamespace(Agent_CMD=command,Command_ID=456,
            _connection_header=dict(callerid=caller,latching=latch),
            header=types.SimpleNamespace(stamp=types.SimpleNamespace(to_sec=lambda:stamp)))

    def confirm_landing(self):
        self.node._control_callback(types.SimpleNamespace(control_state=3,failsafe=False,uav_id=3))

    def test_b_land_then_flight_control_confirmation_creates_one_read_only_event(self):
        self.arm_observer(); n=self.node
        n._external_return_command_callback(self.land_command())
        self.assertFalse(n._successful_return_snapshot())
        self.confirm_landing()
        event=n._successful_return_snapshot()
        self.assertEqual('external_program_b_land', event['source'])
        self.assertEqual('/uav3/trajectory_follower',event['evidence']['command_node'])
        self.assertEqual('LAND_CONTROL',event['evidence']['confirmed_control_state'])
        self.assertEqual(n._assignment['assignment_checksum'],event['assignment_checksum'])
        self.assertEqual('session',event['competition_session_id'])
        n._external_return_command_callback(self.land_command())
        self.confirm_landing()
        self.assertEqual(event['event_id'], n._successful_return_snapshot()['event_id'])
        n.successful_return_pub.publish.assert_called_once()
        n._land.assert_not_called(); n._fly_to.assert_not_called(); n._hover.assert_not_called()

    def test_control_state_before_command_is_supported_but_not_sufficient(self):
        self.arm_observer(); n=self.node
        self.confirm_landing()
        self.assertFalse(n._successful_return_snapshot())
        n._external_return_command_callback(self.land_command())
        self.assertTrue(n._successful_return_snapshot())

    def test_other_publishers_old_future_latched_and_non_land_are_ignored(self):
        self.arm_observer(); n=self.node; self.confirm_landing()
        commands=[self.land_command(caller=caller) for caller in
                  ('/communication_bridge','/su17_competition_executor','/uav2/trajectory_follower',
                   '/uav3/target_maneuver_gx40_position_pid')]
        commands += [self.land_command(stamp=stamp) for stamp in (0,99.9,102,float('nan'),float('inf'))]
        commands += [self.land_command(latch='1'), self.land_command(command=4)]
        for cmd in commands:
            n._external_return_command_callback(cmd)
            self.assertFalse(n._successful_return_snapshot())

    def test_without_handoff_internal_or_other_task_never_accepts_land(self):
        self.arm_observer(); n=self.node; self.confirm_landing()
        watch=n._progress.pop('external_return_watch')
        n._external_return_command_callback(self.land_command())
        self.assertFalse(n._successful_return_snapshot())
        for overrides in ({'mission_id':'old'},{'assignment_checksum':'old'},{'boot_id':'old'}):
            n._progress['external_return_watch']=dict(watch,**overrides)
            n._external_return_command_callback(self.land_command())
            self.assertFalse(n._successful_return_snapshot())
        n._progress['external_return_watch']=watch
        n._assignment['controller_mode']='internal'
        n._external_return_command_callback(self.land_command())
        self.assertFalse(n._successful_return_snapshot())

    def test_expired_candidate_stale_feedback_disarmed_and_failsafe_do_not_confirm(self):
        self.arm_observer(); n=self.node
        n._external_return_command_callback(self.land_command())
        n._control.control_state=3
        for obj, attr, value in ((n._state,'armed',False),(n._state,'connected',False),
                                 (n._control,'failsafe',True),(n,'_state_received',90),
                                 (n,'_control_received',90)):
            old=getattr(obj,attr);setattr(obj,attr,value)
            n._confirm_external_return()
            self.assertFalse(n._successful_return_snapshot())
            setattr(obj,attr,old)
        with patch.object(harness.module.time,'monotonic',return_value=106):
            n._state_received=n._control_received=106
            n._confirm_external_return()
            self.assertFalse(n._successful_return_snapshot())

    def test_candidate_survives_same_boot_restart_and_is_bound_to_assignment(self):
        self.arm_observer(); n=self.node
        n._external_return_command_callback(self.land_command())
        n._progress={};n._restore_progress()
        self.confirm_landing()
        self.assertTrue(n._successful_return_snapshot())
        self.assertFalse(n._resume_pending)

    def test_handoff_written_when_external_task_published_and_not_reset_by_republish(self):
        n=self.node
        n._publish_external_mission({})
        watch=dict(n._progress['external_return_watch'])
        self.assertEqual(101,watch['started_ros'])
        n._publish_external_mission({})
        self.assertEqual(watch,n._progress['external_return_watch'])

    def test_observer_is_compatible_with_all_scenes_and_coordinates(self):
        self.arm_observer(); n=self.node
        for profile in ('lab','outdoor5','lab10','outdoor100','outdoor200','competition','dalian_nanshan'):
            for frame in ('ENU','WGS84'):
                with self.subTest(profile=profile,frame=frame):
                    n._progress.pop('successful_return',None)
                    n._progress.pop('external_return_candidate',None)
                    n._assignment.update(flight_profile=profile,coordinate_frame=frame)
                    n._external_return_command_callback(self.land_command())
                    self.confirm_landing()
                    self.assertTrue(n._successful_return_snapshot())

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
