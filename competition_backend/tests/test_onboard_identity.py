"""机载预检的 ROS 边界模拟；确认飞控编号而不是命令行编号决定身份。"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('onboard_preflight',Path(__file__).resolve().parents[2]/'tools/onboard_preflight.py')
preflight=importlib.util.module_from_spec(spec);spec.loader.exec_module(preflight)


class OnboardIdentityTest(unittest.TestCase):
    def ros(self,model,fc_id=3,state_id=3,second=False,md5=None):
        state_cls=type('UAVState',(),{'_md5sum':md5 or preflight.STATE_MD5[model]})
        control_cls=type('UAVControlState',(),{})
        rospy=types.ModuleType('rospy');rospy.init_node=Mock();rospy.wait_for_service=Mock()
        rospy.get_published_topics=lambda:[('/uav3/prometheus/state','prometheus_msgs/UAVState')]+([('/uav2/prometheus/state','prometheus_msgs/UAVState')] if second else [])
        rospy.ServiceProxy=Mock(return_value=Mock(return_value=types.SimpleNamespace(success=True,value=types.SimpleNamespace(integer=fc_id))))
        rospy.wait_for_message=lambda topic,cls,timeout:types.SimpleNamespace(uav_id=state_id,connected=True)
        msg=types.ModuleType('prometheus_msgs.msg');msg.UAVState=state_cls;msg.UAVControlState=control_cls
        srv=types.ModuleType('mavros_msgs.srv');srv.ParamGet=type('ParamGet',(),{})
        return {'rospy':rospy,'prometheus_msgs':types.ModuleType('prometheus_msgs'),'prometheus_msgs.msg':msg,'mavros_msgs':types.ModuleType('mavros_msgs'),'mavros_msgs.srv':srv}

    def test_both_vendor_message_versions_use_actual_fc_identity(self):
        for model in ('p600','su17'):
            with self.subTest(model=model),patch.dict(sys.modules,self.ros(model)),patch.object(Path,'read_text',return_value='test-machine'):
                result=preflight.check_identity(model)
                self.assertEqual(result['uav_id'],3);self.assertEqual(result['ros_namespace'],'/uav3');self.assertEqual(result['model'],model)
                self.assertTrue(result['identity_verified']);self.assertEqual(len(result['device_id']),24)

    def test_mismatches_fail_before_registration(self):
        for kwargs in ({'fc_id':2},{'state_id':1},{'second':True},{'md5':'wrong-version'}):
            with self.subTest(kwargs=kwargs),patch.dict(sys.modules,self.ros('p600',**kwargs)),self.assertRaises(ValueError):
                preflight.check_identity('p600')
        with patch.dict(sys.modules,self.ros('p600')),self.assertRaises(ValueError):preflight.check_identity('p600',expected_id=6)

if __name__=='__main__':unittest.main()
