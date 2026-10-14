import importlib.util
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
        self.params = {'/communication_bridge': dict(ROBOT_ID=2, uav_id=2, is_simulation=0),
                      '/switch_location_source/Location_Source/GPS': dict(control=dict(location_source=4),
                                                                        px4_params=dict(EKF2_AID_MASK=1))}
        self.master = Mock()
        self.master.getParam.side_effect = lambda _, name: (1, '', self.params[name]) if name in self.params else (-1, '', 0)
        self.master.getSystemState.side_effect = lambda _: (1, '', [
            [('/uav2/prometheus/state', self.nodes)],
            [('/uav2/prometheus/state', ['/communication_bridge'])], []])
        self.rospy = Mock()
        self.switch = Mock(return_value=SimpleNamespace(success=True, message='ok'))
        self.param_get = Mock(return_value=SimpleNamespace(success=True, value=SimpleNamespace(integer=1, real=0.0)))
        self.rospy.ServiceProxy.side_effect = lambda name, _: self.switch if name.endswith('switch_location_source') else self.param_get
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

    def run_action(self, action='prometheus_gps', boot='boot-one'):
        return remote.run(dict(uav_id=2, action=action, expected_boot_id=boot))

    def test_readonly_probe_does_not_switch_or_create_ready_record(self):
        result = self.run_action('prometheus_probe')
        self.assertFalse(result['prepared'])
        self.assertFalse(self.record.exists())
        self.switch.assert_not_called()

    def test_call_once_then_verify_actual_state_and_parameters(self):
        self.assertFalse(self.run_action()['ready'])
        self.assertTrue(self.run_action()['ready'])
        self.assertTrue(self.run_action()['ready'])
        self.assertTrue(self.run_action('prometheus_probe')['prepared'])
        self.switch.assert_called_once_with(4)

    def test_service_reply_lost_does_not_repeat_switch(self):
        self.switch.side_effect = TimeoutError('lost reply')
        with self.assertRaises(TimeoutError): self.run_action()
        self.assertTrue(self.run_action()['ready'])
        self.switch.assert_called_once_with(4)

    def test_wrong_boot_armed_and_existing_programs_cannot_switch(self):
        with self.assertRaises(RuntimeError): self.run_action(boot='old')
        self.flight['armed'] = True
        self.assertFalse(self.run_action()['ready'])
        self.flight['armed'] = False
        self.nodes.append('/servo_serial_node')
        self.assertFalse(self.run_action()['ready'])
        self.switch.assert_not_called()

    def test_parameter_mismatch_is_not_ready_even_if_service_succeeds(self):
        self.run_action()
        self.param_get.return_value.value.integer = 24
        result = self.run_action()
        self.assertFalse(result['ready'])
        self.assertIn('EKF2_AID_MASK', result['detail'])

    def test_boot_change_invalidates_old_preparation(self):
        self.run_action(); self.run_action()
        with patch.object(remote, 'current_boot_id', return_value='boot-two'):
            self.assertFalse(self.run_action('prometheus_probe')['prepared'])
            self.run_action(boot='boot-two')
        self.assertEqual(self.switch.call_count, 2)


if __name__ == '__main__': unittest.main()
