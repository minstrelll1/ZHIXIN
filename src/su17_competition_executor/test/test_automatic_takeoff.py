import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import test_executor_autonomy as existing
module = existing.module


class AutomaticTakeoffTest(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.node = existing.AutonomyTest().executor(tempfile.gettempdir())
        self.node.enable_motion = True
        self.node.command_rate = 20.0
        self.node._identity_error = ""
        self.node._manual_override = False
        self.node._automatic_control = False
        self.node._command_control_seen = False
        self.node._startup_initial_control = None
        self.node._rc_mode_switch = 0
        self.node._state_received = self.node._control_received = self.node._rc_received = self.clock
        self.node.setup_pub = Mock()
        self.node.command_pub = Mock()
        self.node.setup_pub.get_num_connections.return_value = 1
        self.node.command_pub.get_num_connections.return_value = 1
        self.node._state = types.SimpleNamespace(uav_id=3, connected=True, armed=False, odom_valid=True,
            mode="POSCTL", position=[10.0, 20.0, .4], velocity=[0., 0., 0.], attitude=[0., 0., .2])
        self.node._control = types.SimpleNamespace(uav_id=3, control_state=0, pos_controller=0, failsafe=False)
        self.node._snapshot = lambda: (self.node._state, self.node._control)
        self.node._takeoff_precheck = module.OnboardTaskExecutor._takeoff_precheck.__get__(self.node)
        self.node._ensure_command_control = module.OnboardTaskExecutor._ensure_command_control.__get__(self.node)
        self.node._checkpoint = Mock()
        self.node._publish_takeoff_position = Mock()
        self.commands = []
        self.node._publish_setup = lambda cmd: self.commands.append(cmd)
        self.target = (10., 20., 1.9, .2)
        self.patches = [patch.object(module.time, "monotonic", lambda: self.clock)]
        for name, value in dict(INIT=0, RC_POS_CONTROL=1, COMMAND_CONTROL=2, LAND_CONTROL=3, PX4_ORIGIN=0).items():
            self.patches.append(patch.object(module.UAVControlState, name, value, create=True))
        for name, value in dict(ARMING=0, SET_CONTROL_MODE=3).items():
            self.patches.append(patch.object(module.UAVSetup, name, value, create=True))
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.action = self.advance_controller
        self.node._abort_motion.wait = self.wait

    def wait(self, duration):
        self.clock += duration
        self.node._state_received = self.node._control_received = self.node._rc_received = self.clock
        self.action()

    def advance_controller(self):
        if 0 in self.commands:
            self.node._state.armed = True
        if 3 in self.commands:
            self.node._control.control_state = 2
            self.node._state.mode = "OFFBOARD"

    def test_arm_then_command_then_confirm_offboard_with_exact_target(self):
        ok, _ = self.node._ensure_command_control(self.target)
        self.assertTrue(ok)
        self.assertEqual(self.commands, [0, 3])
        self.assertTrue(self.node._publish_takeoff_position.called)
        self.assertTrue(all(c.args[0] == self.target for c in self.node._publish_takeoff_position.call_args_list))
        self.node.external_path_pub.publish.assert_not_called()

    def test_missing_mavros_speed_limit_keeps_takeoff_locked(self):
        self.node.identity = {'uav_id': 1}
        self.node.local_ros_uav_id = 1
        self.node.flight_speed_limit = None
        ready, reason = self.node._takeoff_precheck()
        self.assertFalse(ready)
        self.assertIn('/uav1/mavros/param/get', reason)
        self.node.flight_speed_limit = 2.0
        ready, _ = self.node._takeoff_precheck()
        self.assertTrue(ready)

    def test_arming_rejected_times_out_without_switching_or_repeated_arm(self):
        self.action = lambda: None
        ok, reason = self.node._ensure_command_control(self.target)
        self.assertFalse(ok)
        self.assertIn("解锁超时", reason)
        self.assertEqual(self.commands, [0])
        self.node._publish_takeoff_position.assert_not_called()

    def test_control_mode_or_offboard_rejected_does_not_release_task(self):
        self.action = lambda: setattr(self.node._state, "armed", True)
        ok, reason = self.node._ensure_command_control(self.target)
        self.assertFalse(ok)
        self.assertIn("切换超时", reason)
        self.assertEqual(self.commands, [0, 3])
        self.node.external_path_pub.publish.assert_not_called()

    def test_precheck_faults_never_issue_arm(self):
        for name, change in [
            ("定位", lambda: setattr(self.node._state, "odom_valid", False)),
            ("保护", lambda: setattr(self.node._control, "failsafe", True)),
            ("断联", lambda: setattr(self.node._state, "connected", False)),
            ("降落", lambda: setattr(self.node._control, "control_state", 3)),
            ("RC超时", lambda: setattr(self.node, "_rc_received", 90)),
            ("控制器", lambda: setattr(self.node._control, "pos_controller", 1)),
            ("无效坐标", lambda: setattr(self.node._state, "position", [0, 0, float('nan')])),
        ]:
            with self.subTest(name=name):
                state, control = vars(self.node._state).copy(), vars(self.node._control).copy()
                change()
                self.assertFalse(self.node._ensure_command_control(self.target)[0])
                self.assertEqual(self.commands, [])
                self.node._state = types.SimpleNamespace(**state)
                self.node._control = types.SimpleNamespace(**control)
                self.node._rc_received = self.clock

    def test_rc_takeover_during_arm_cancels_without_mode_command(self):
        self.action = lambda: self.node._rc_callback(types.SimpleNamespace(channels=[1500]*8))
        ok, _ = self.node._ensure_command_control(self.target)
        self.assertFalse(ok)
        self.assertTrue(self.node._manual_override)
        self.assertEqual(self.commands, [0])
        self.node._run_task = Mock()
        self.node._motion_entry("takeoff", lambda _: True, {"mission_id":"test"})
        self.node._run_task.assert_not_called()

    def test_rc_takeover_after_command_latches_even_if_switched_back(self):
        self.node._automatic_control = True
        self.node._command_control_seen = True
        self.node._startup_initial_control = 0
        for mode in (1, 0, 2):
            self.node._control_callback(types.SimpleNamespace(uav_id=3, control_state=mode, pos_controller=0, failsafe=False))
        self.assertTrue(self.node._manual_override)
        self.assertFalse(self.node._resume_pending)
        self.assertFalse(self.node._ready()[0])
        module.OnboardTaskExecutor._hover(self.node)
        module.OnboardTaskExecutor._publish_velocity(self.node, 0, 0, 1.5, 0)
        module.OnboardTaskExecutor._land(self.node)
        self.node.command_pub.publish.assert_not_called()
        with self.assertRaisesRegex(ValueError, "不向程序 B"):
            self.node._publish_external_mission({})

    def test_existing_armed_command_mode_does_not_rearm(self):
        self.node._state.armed = True
        self.node._state.mode = "OFFBOARD"
        self.node._control.control_state = 2
        self.assertTrue(self.node._ensure_command_control(self.target)[0])
        self.assertEqual(self.commands, [])

    def deliver_control(self, mode, failsafe=False):
        self.node._control_callback(types.SimpleNamespace(
            uav_id=3, control_state=mode, pos_controller=0, failsafe=failsafe))

    def record_startup_phase(self):
        self.node._checkpoint.side_effect = lambda phase, **values: self.node._progress.update(
            phase=phase, **values)

    def test_initial_rc_pos_transition_can_complete_takeoff_handshake(self):
        self.record_startup_phase()
        ticks = []
        def startup():
            ticks.append(self.clock)
            self.node._state.armed = True
            # Separate status frames can report RC_POS while COMMAND is pending.
            if len(ticks) <= 3:
                self.deliver_control(1)
            else:
                self.deliver_control(2)
                self.node._state.mode = 'OFFBOARD'
        self.action = startup
        self.assertTrue(self.node._ensure_command_control(self.target)[0])
        self.assertEqual(self.commands, [0, 3])
        self.assertFalse(self.node._manual_override)
        self.node.external_path_pub.publish.assert_not_called()

    def test_initial_rc_pos_transition_still_times_out_without_command_control(self):
        self.record_startup_phase()
        def startup():
            self.node._state.armed = True
            self.deliver_control(1)
        self.action = startup
        ok, reason = self.node._ensure_command_control(self.target)
        self.assertFalse(ok)
        self.assertIn('切换超时', reason)
        self.assertFalse(self.node._manual_override)
        self.assertEqual(self.commands, [0, 3])

    def test_real_rc_switch_still_cancels_during_initial_rc_pos_transition(self):
        self.record_startup_phase()
        def startup():
            self.node._state.armed = True
            self.deliver_control(1)
            self.node._rc_callback(types.SimpleNamespace(channels=[1500] * 8))
        self.action = startup
        ok, reason = self.node._ensure_command_control(self.target)
        self.assertFalse(ok)
        self.assertTrue(self.node._manual_override)
        self.assertIn('遥控器模式拨杆变动', reason)
        self.assertEqual(self.commands, [0])

    def test_startup_exception_does_not_allow_other_control_exits(self):
        for initial, incoming, failsafe, phase in (
            (1, 0, False, 'entering_command_control'),
            (0, 3, False, 'entering_command_control'),
            (0, 99, False, 'entering_command_control'),
            (0, 1, True, 'entering_command_control'),
            (0, 1, False, 'taking_off'),
            (0, 1, False, 'executing'),
        ):
            with self.subTest(initial=initial, incoming=incoming, failsafe=failsafe, phase=phase):
                self.node._automatic_control = True
                self.node._manual_override = False
                self.node._abort_motion.clear()
                self.node._startup_initial_control = initial
                self.node._command_control_seen = False
                self.node._progress['phase'] = phase
                self.deliver_control(incoming, failsafe)
                self.assertTrue(self.node._manual_override)
                self.assertTrue(self.node._abort_motion.is_set())

    def test_command_exit_still_cancels_before_offboard_confirmation(self):
        self.record_startup_phase()
        self.node._automatic_control = True
        self.node._startup_initial_control = 0
        self.node._progress['phase'] = 'entering_command_control'
        self.deliver_control(2)
        self.deliver_control(1)
        self.assertTrue(self.node._manual_override)
        self.assertTrue(self.node._abort_motion.is_set())

    def test_mode_exit_logs_previous_current_px4_and_rc_context(self):
        self.node._automatic_control = True
        self.node._startup_initial_control = 0
        self.node._progress['phase'] = 'entering_command_control'
        self.node._state.armed = True
        self.deliver_control(2)
        self.deliver_control(1)
        reason = self.node._publish_status.call_args.kwargs['error']
        for value in ('COMMAND_CONTROL(2)', 'RC_POS_CONTROL(1)', 'POSCTL', 'armed=True', 'rc_switch=0'):
            self.assertIn(value, reason)

    def test_repeated_control_state_does_not_spam_transition_log(self):
        self.node._automatic_control = True
        self.node._startup_initial_control = 0
        self.node._progress['phase'] = 'arming'
        with patch.object(module.rospy, 'loginfo') as log:
            self.deliver_control(1)
            self.deliver_control(1)
            self.deliver_control(1)
        self.assertEqual(log.call_count, 1)

    def test_takeoff_failure_does_not_release_program_b_or_internal_task(self):
        for mode in ('external','internal'):
            with self.subTest(mode=mode):
                self.node._assignment['controller_mode'] = mode
                self.node._publish_external_mission = Mock()
                self.node._run_task = Mock()
                self.node._motion_entry('takeoff', lambda _: False, {})
                self.node._publish_external_mission.assert_not_called()
                self.node._run_task.assert_not_called()

    def test_position_message_uses_xyz_and_no_yaw_rate(self):
        command = types.SimpleNamespace(header=types.SimpleNamespace())
        factory=Mock(return_value=command)
        factory.Move=4; factory.DEFAULT_CONTROL=0; factory.XYZ_POS=0
        self.node._next_command_id=Mock(return_value=42)
        with patch.object(module, 'UAVCommand', factory), patch.object(module.rospy, 'Time', types.SimpleNamespace(now=lambda:100), create=True):
            module.OnboardTaskExecutor._publish_takeoff_position(self.node, self.target)
        self.assertEqual(command.position_ref, list(self.target[:3]))
        self.assertEqual(command.yaw_ref, .2)
        self.assertFalse(command.Yaw_Rate_Mode)
        self.node.command_pub.publish.assert_called_once_with(command)


    def test_completed_takeoff_hands_over_to_b_only_after_altitude_success(self):
        node=self.node
        node._assignment['controller_mode']='external'
        node._assignment['coordinate_frame']='ENU'
        node.max_distance_from_home=2000
        order=[]
        node._fly_to=Mock(side_effect=lambda *args: (order.append('altitude_stable') or True, 'reached'))
        node._publish_external_mission=Mock(side_effect=lambda _:order.append('B_path_and_landing'))
        node._publish_recon_start_mode=Mock(side_effect=lambda _:order.append('B_start'))
        node._motion_entry('takeoff',node._run_takeoff,{'mission_id':'test','target_altitude_m':.5})
        self.assertEqual(self.commands,[0,3])
        self.assertEqual(order,['altitude_stable','B_path_and_landing','B_start'])

    def test_cancelled_during_altitude_flight_does_not_publish_b(self):
        node=self.node
        node._assignment['controller_mode']='external'
        node._assignment['coordinate_frame']='ENU'
        node.max_distance_from_home=2000
        def interrupted(*args):
            node._release_automatic_control('遥控器接管')
            return False,'preempted'
        node._fly_to=Mock(side_effect=interrupted)
        node._publish_external_mission=Mock()
        node._motion_entry('takeoff',node._run_takeoff,{'mission_id':'test','target_altitude_m':.5})
        node._publish_external_mission.assert_not_called()

    def test_setup_messages_use_vendor_protocol_without_force_arm(self):
        messages=[]
        def factory():
            return types.SimpleNamespace(header=types.SimpleNamespace())
        factory.ARMING=0;factory.SET_CONTROL_MODE=3
        self.node.setup_pub.publish.side_effect=messages.append
        with patch.object(module,'UAVSetup',factory),patch.object(module.rospy,'Time',types.SimpleNamespace(now=lambda:100),create=True):
            module.OnboardTaskExecutor._publish_setup(self.node,0)
            module.OnboardTaskExecutor._publish_setup(self.node,3)
        self.assertTrue(messages[0].arming)
        self.assertEqual(messages[1].control_state,'COMMAND_CONTROL')

    def test_delayed_setup_after_rc_takeover_is_not_published(self):
        self.node._manual_override = True
        module.OnboardTaskExecutor._publish_setup(self.node, 0)
        module.OnboardTaskExecutor._publish_setup(self.node, 3)
        self.node.setup_pub.publish.assert_not_called()

if __name__ == '__main__':
    unittest.main()
