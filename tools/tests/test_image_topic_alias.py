"""只测试订阅契约，不加载 ROS 或连接真实无人机。"""
from pathlib import Path
from types import SimpleNamespace
import textwrap
import unittest


class ImageTopicAliasTest(unittest.TestCase):
    def subscribe(self, primary, mode='completed_target_array'):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'src/su17_image_transfer/scripts/onboard_image_sender.py').read_text(encoding='utf-8')
        begin = source.index('            self.detection_subscriber = rospy.Subscriber(')
        end = source.index('            self.detection_worker = threading.Thread', begin)
        calls = []
        ros = SimpleNamespace(Subscriber=lambda topic, *a, **kw: calls.append(topic),
                              resolve_name=lambda topic: '/' + topic.lstrip('/'), loginfo=lambda *a: None)
        node = SimpleNamespace(input_mode=mode, local_ros_uav_id=3, _detection_callback=lambda msg: None)
        exec(compile(textwrap.dedent(source[begin:end]), '<subscription-contract>', 'exec'),
             dict(self=node, rospy=ros, detection_topic=primary, detection_type=object,
                  CompletedTargetArray=object, queue_size=10))
        return calls

    def test_legacy_and_new_program_b_both_subscribed(self):
        self.assertEqual(self.subscribe('/target_stcheduler/complemend_targets'),
                         ['/target_stcheduler/complemend_targets', '/uav3/target_scheduler/completed_targets'])

    def test_explicit_new_topic_not_subscribed_twice(self):
        self.assertEqual(self.subscribe('/uav3/target_scheduler/completed_targets'),
                         ['/uav3/target_scheduler/completed_targets'])

    def test_other_detection_mode_unchanged(self):
        self.assertEqual(self.subscribe('/custom_detection', 'timestamped_detection'), ['/custom_detection'])


if __name__ == '__main__':
    unittest.main()
