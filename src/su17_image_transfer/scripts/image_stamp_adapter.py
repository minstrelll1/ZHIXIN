#!/usr/bin/env python3
import copy
import json
import time
import rospy
from sensor_msgs.msg import Image
from std_msgs.msg import String
from su17_image_transfer.stamp_policy import StampPolicy

class ImageStampAdapter:
    def __init__(self):
        uid = int(rospy.get_param('~uav_id'))
        prefix = '/uav%d' % uid
        self.policy = StampPolicy(rospy.get_param('~mode', 'preserve_or_receive'))
        self.output = rospy.Publisher(prefix + '/competition/image_stamped', Image, queue_size=2)
        self.health = rospy.Publisher(prefix + '/competition/image_health', String, queue_size=1, latch=True)
        self.last_report = 0
        self.frames = 0
        self.dropped = 0
        self.sub = rospy.Subscriber(prefix + '/gimbal/image_original', Image, self.callback, queue_size=2, buff_size=67108864)
        rospy.loginfo('图像时间戳适配已启动：算法与回传缓存请共用 %s', prefix + '/competition/image_stamped')

    def callback(self, message):
        try:
            stamp, basis = self.policy.resolve(message.header.stamp.to_nsec(), rospy.Time.now().to_nsec())
        except ValueError as error:
            self.dropped += 1
            rospy.logwarn_throttle(5, str(error))
            return
        output = copy.copy(message)
        output.header = copy.copy(message.header)
        output.header.stamp = rospy.Time(stamp // 1000000000, stamp % 1000000000)
        self.output.publish(output)
        self.frames += 1
        if time.monotonic() - self.last_report >= 1:
            self.health.publish(String(data=json.dumps({'frames': self.frames, 'dropped': self.dropped, 'stamp_basis': basis, 'stamp_ns': stamp})))
            self.last_report = time.monotonic()

if __name__ == '__main__':
    rospy.init_node('competition_image_stamp_adapter')
    ImageStampAdapter()
    rospy.spin()
