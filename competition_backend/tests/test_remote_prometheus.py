import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('remote_prometheus_test', ROOT / 'tools/remote_prometheus.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class RemotePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.record = self.home/'competition_development/ground_runtime/programs_uav2/prometheus_connection.json'
        self.flight = dict(connected=True, armed=False, location_source=4, mode='POSCTL')
        self.nodes = ['/communication_bridge', '/uav2/mavros', '/uav_control_main_2']
        self.control = dict(location_source=4, pos_controller=0, maximum_safe_vel_xy=6.0, maximum_safe_vel_z=6.0)
        self.fence = dict(x_min=-1000., x_max=1000., y_min=-1000., y_max=1000., z_min=-5., z_max=80.)
        self.preset = dict(control=dict(location_source=4, maximum_safe_vel_xy=5., maximum_safe_vel_z=4.),
                           px4_params=dict(EKF2_AID_MASK=1, EKF2_HGT_MODE=1, COM_ARM_WO_GPS=1, MC_YAWRATE_MAX=80.))
        self.px4 = dict(EKF2_AID_MASK=1, EKF2_HGT_MODE=1, COM_ARM_WO_GPS=0, MC_YAWRATE_MAX=70.,
                        MPC_Z_VEL_MAX_UP=4., MPC_Z_VEL_MAX_DN=3., MPC_XY_VEL_MAX=5.)
        self.params = {'/communication_bridge': dict(ROBOT_ID=2, uav_id=2, is_simulation=0),
                       '/uav_control_main_2/control': self.control, '/uav_control_main_2/geo_fence': self.fence,
                       '/switch_location_source/Location_Source/GPS': self.preset}
        self.master = Mock()
        self.master.getParam.side_effect = lambda _, name: (1, '', self.params[name]) if name in self.params else (-1, '', 0)
        self.master.getSystemState.side_effect = lambda _: (1, '', [
            [('/uav2/prometheus/state', self.nodes)],
            [('/uav2/prometheus/state', ['/communication_bridge'])], []])
        self.rospy = Mock()
        self.switch = Mock(side_effect=self.apply_switch)
        self.param_get = Mock(side_effect=self.get_parameter)
        def service(name, _):
            if name.endswith('switch_location_source'): return self.switch
            if name.endswith('/param/get'): return self.param_get
            raise AssertionError('Unexpected service: ' + name)
        self.rospy.ServiceProxy.side_effect = service
        self.patches = [patch.object(remote.Path, 'home', return_value=self.home),
            patch.object(remote, 'current_boot_id', return_value='boot-one'),
            patch.object(remote, 'ros_setup', return_value=self.rospy),
            patch.object(remote, 'flight_sample', side_effect=lambda *_: dict(self.flight)),
            patch.object(remote.socket, 'setdefaulttimeout'),
            patch.object(remote.xmlrpc.client, 'ServerProxy', return_value=self.master),
            patch.dict(sys.modules, {'fcntl': SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4),
                'prometheus_msgs.srv': SimpleNamespace(SwitchLocationSource=object),
                'mavros_msgs.srv': SimpleNamespace(ParamGet=object)})]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def get_parameter(self, name):
        value = self.px4.get(name)
        return SimpleNamespace(success=value is not None,
            value=SimpleNamespace(integer=value if type(value) is int else 0,
                                  real=value if type(value) is float else 0.))

    def apply_switch(self, source):
        self.flight['location_source'] = source
        self.control.update(self.preset['control'])
        self.px4.update(self.preset['px4_params'])
        self.fence['z_max'] = 1000.
        return SimpleNamespace(success=True, message='ok')

    def non_gps(self):
        self.flight['location_source'] = self.control['location_source'] = 10

    def run_action(self, action='prometheus_gps', boot='boot-one'):
        return remote.run(dict(uav_id=2, action=action, expected_boot_id=boot))

    def read_record(self):
        return json.loads(self.record.read_text(encoding='utf-8'))

    def test_readonly_probe_does_not_switch_or_create_ready_record(self):
        result = self.run_action('prometheus_probe')
        self.assertFalse(result['prepared'])
        self.assertFalse(self.record.exists())
        self.switch.assert_not_called()
        self.param_get.assert_not_called()

    def test_existing_gps_keeps_tuned_six_mps_fence_and_px4_settings(self):
        original = copy.deepcopy((self.control, self.fence, self.px4))
        result = self.run_action()
        self.assertTrue(result['ready'])
        self.assertEqual((self.control, self.fence, self.px4), original)
        self.assertEqual(result['preparation_audit']['mode'], 'reuse_existing_gps')
        self.assertEqual(result['preparation_audit']['parameter_changes'], {})
        self.assertEqual(result['preparation_audit']['before']['control']['maximum_safe_vel_z'], 6.)
        self.assertIn('未调用', result['detail'])
        self.switch.assert_not_called()
        self.master.setParam.assert_not_called()
        self.assertFalse(self.read_record().get('requested'))

    def test_existing_gps_reconnect_and_backend_restart_do_not_repeat_audit_or_switch(self):
        first = self.run_action()
        self.assertTrue(self.run_action()['ready'])
        probe = self.run_action('prometheus_probe')
        self.assertTrue(probe['prepared'])
        self.assertEqual(probe['preparation_audit'], first['preparation_audit'])
        lines = self.record.with_name('prometheus_connection_audit.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 1)
        self.switch.assert_not_called()

    def test_existing_gps_parameter_mismatch_never_falls_back_to_switch(self):
        self.px4['EKF2_AID_MASK'] = 24
        result = self.run_action()
        self.assertFalse(result['ready'])
        self.assertIn('EKF2_AID_MASK', result['detail'])
        self.assertIn('不自动重新切换', result['detail'])
        self.assertFalse(self.run_action()['ready'])
        self.px4['EKF2_AID_MASK'] = 1
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_existing_gps_read_timeout_is_readonly_and_recovers(self):
        self.param_get.side_effect = TimeoutError('read timeout')
        self.assertFalse(self.run_action()['ready'])
        self.param_get.side_effect = self.get_parameter
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_existing_gps_control_disagreement_does_not_switch(self):
        self.control['location_source'] = 10
        self.assertFalse(self.run_action()['ready'])
        self.control['location_source'] = 4
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_reuse_decision_survives_failure_and_later_non_gps_sample(self):
        self.px4['EKF2_HGT_MODE'] = 0
        self.assertFalse(self.run_action()['ready'])
        self.non_gps()
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_positive_tuned_limits_allowed_but_invalid_values_logged_without_switch(self):
        for bad in (None, 0, -1, float('nan'), float('inf'), '6', True):
            with self.subTest(value=bad):
                self.control['maximum_safe_vel_z'] = bad
                self.assertFalse(self.run_action()['ready'])
        self.control['maximum_safe_vel_z'] = 6.0
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_gps_with_additional_fusion_bits_is_not_reset(self):
        self.px4['EKF2_AID_MASK'] = 5
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_non_gps_call_once_then_verify_and_log_actual_changes(self):
        self.non_gps()
        self.assertFalse(self.run_action()['ready'])
        result = self.run_action()
        self.assertTrue(result['ready'])
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_called_once_with(4)
        changes = result['preparation_audit']['parameter_changes']
        self.assertEqual(changes['control/maximum_safe_vel_z'], dict(before=6., after=4.))
        self.assertEqual(changes['geo_fence/z_max'], dict(before=80., after=1000.))
        self.assertEqual(changes['px4_params/COM_ARM_WO_GPS'], dict(before=0, after=1))

    def test_service_reply_lost_does_not_repeat_switch(self):
        self.non_gps()
        def lose_reply(source):
            self.apply_switch(source)
            raise TimeoutError('lost reply')
        self.switch.side_effect = lose_reply
        result = self.run_action()
        self.assertFalse(result['ready'])
        self.assertIsNone(result['preparation_audit']['after'])
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_called_once_with(4)

    def test_service_rejection_does_not_repeat_switch(self):
        self.non_gps()
        self.switch.side_effect = None
        self.switch.return_value = SimpleNamespace(success=False, message='拒绝')
        self.assertFalse(self.run_action()['ready'])
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_called_once_with(4)

    def test_wrong_boot_armed_and_existing_programs_cannot_switch(self):
        self.non_gps()
        with self.assertRaises(RuntimeError): self.run_action(boot='old')
        self.flight['armed'] = True
        self.assertFalse(self.run_action()['ready'])
        self.flight['armed'] = False
        self.nodes.append('/servo_serial_node')
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_unavailable_source_does_not_trigger_switch(self):
        self.flight.pop('location_source')
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_source_becomes_gps_during_sampling_uses_readonly_branch(self):
        self.non_gps()
        remote.flight_sample.side_effect = [dict(self.flight), dict(self.flight, location_source=4)]
        self.assertFalse(self.run_action()['ready'])
        self.assertEqual(self.read_record()['mode'], 'reuse_existing_gps')
        self.switch.assert_not_called()

    def test_armed_during_sampling_cannot_switch(self):
        self.non_gps()
        remote.flight_sample.side_effect = [dict(self.flight), dict(self.flight, armed=True)]
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_control_already_gps_but_telemetry_not_yet_updated_does_not_reset(self):
        self.flight['location_source'] = 10
        self.assertFalse(self.run_action()['ready'])
        self.assertEqual(self.read_record()['mode'], 'reuse_existing_gps')
        self.switch.assert_not_called()

    def test_business_started_during_sampling_cannot_switch(self):
        self.non_gps()
        def get_parameter(name):
            self.nodes.append('/uav2/trajectory_follower')
            return self.get_parameter(name)
        self.param_get.side_effect = get_parameter
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_boot_change_with_gps_still_reuses(self):
        self.assertTrue(self.run_action()['ready'])
        with patch.object(remote, 'current_boot_id', return_value='boot-two'):
            self.assertFalse(self.run_action('prometheus_probe')['prepared'])
            self.assertTrue(self.run_action(boot='boot-two')['ready'])
        self.switch.assert_not_called()

    def test_old_version_intent_without_snapshot_is_never_resent(self):
        self.record.parent.mkdir(parents=True)
        remote.save_record(self.record, dict(boot_id='boot-one', requested=True, accepted=True))
        self.px4.update(self.preset['px4_params'])
        result = self.run_action()
        self.assertTrue(result['ready'])
        self.assertIsNone(result['preparation_audit']['parameter_changes'])
        self.switch.assert_not_called()

    def test_completed_old_record_is_preserved_without_reapplying(self):
        self.record.parent.mkdir(parents=True)
        remote.save_record(self.record, dict(boot_id='boot-one', requested=True, ready=True))
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_not_called()
        self.param_get.assert_not_called()

    def test_readonly_gps_can_verify_without_switch_service_or_vendor_preset(self):
        self.params.pop('/switch_location_source/Location_Source/GPS')
        self.assertTrue(self.run_action()['ready'])
        self.rospy.wait_for_service.assert_not_called()
        self.switch.assert_not_called()


if __name__ == '__main__': unittest.main()
