import json
import tempfile
import unittest
from unittest.mock import Mock

from competition_shared.fixed_targets import empty_fixed_targets
from su17_competition_executor.task_protocol import assignment_checksum, load_assignment
import test_recognition_publisher


def target_document():
    return dict(schema_version=1, coordinate_frame="WGS84", source_mission_id="subject1-previous",
                targets=[dict(target_id="target-001", target_category="固定", target_type="工事",
                              target_model="工事2", category_id=8, latitude=33.8691414, longitude=113.7087948),
                         dict(target_id="target-002", target_category="固定", target_type="人员",
                              target_model="人员1", category_id=11, latitude=33.8644, longitude=113.7098)])


class FixedTargetPublisherTest(unittest.TestCase):
    def node(self, directory):
        node, message = test_recognition_publisher.RecognitionPublisherTest().node(directory)
        node.fixed_targets_pub = Mock()
        node.fixed_targets_topic = "/uav3/competition/fixed_targets"
        message["fixed_targets"] = target_document()
        return node, message

    def test_assignment_publishes_before_ack_and_duplicate_republishes_without_flight(self):
        for subject in ("subject1", "subject2"):
            for mode in ("internal", "external"):
                with self.subTest(subject=subject, mode=mode), tempfile.TemporaryDirectory() as directory:
                    node, message = self.node(directory)
                    message.update(subject=subject, controller_mode=mode)
                    message["assignment_checksum"] = assignment_checksum(message)
                    events = []
                    node.fixed_targets_pub.publish.side_effect = lambda m: events.append(("ros", json.loads(m.data)))
                    node._publish_status.side_effect = lambda state, **kw: events.append(("status", state))
                    node._accept_assignment(message)
                    self.assertTrue(node._assignment_acked)
                    self.assertEqual(events[0][0], "ros")
                    result = events[0][1]
                    self.assertEqual(result["targets"], message["fixed_targets"]["targets"])
                    self.assertEqual(result["mission_id"], message["mission_id"])
                    self.assertEqual(result["assignment_checksum"], message["assignment_checksum"])
                    self.assertEqual(events[1], ("status", "task_received"))
                    self.assertEqual(load_assignment(node.cache_path)["fixed_targets"], target_document())
                    node._accept_assignment(message)
                    self.assertEqual(node.fixed_targets_pub.publish.call_count, 2)
                    self.assertIsNone(node._motion_thread)

    def test_empty_legacy_and_startup_messages_clear_previous_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            node, message = self.node(directory)
            message["assignment_checksum"] = assignment_checksum(message)
            node._accept_assignment(message)
            for index, document in enumerate((empty_fixed_targets(), None)):
                message["mission_id"] = "subject2-new-%d" % index
                if document is None:
                    message.pop("fixed_targets")
                else:
                    message["fixed_targets"] = document
                message["assignment_checksum"] = assignment_checksum(message)
                node._accept_assignment(message)
                self.assertTrue(node._assignment_acked)
                self.assertEqual(json.loads(node.fixed_targets_pub.publish.call_args.args[0].data)["targets"], [])
            node._publish_fixed_targets({"uav_id": 3})
            self.assertEqual(json.loads(node.fixed_targets_pub.publish.call_args.args[0].data)["mission_id"], "")

    def test_bad_checksum_or_coordinates_never_publish(self):
        for reason in ("checksum", "coordinates"):
            with tempfile.TemporaryDirectory() as directory:
                node, message = self.node(directory)
                message["assignment_checksum"] = assignment_checksum(message)
                message["fixed_targets"]["targets"][0]["latitude"] = 100
                if reason == "coordinates":
                    message["assignment_checksum"] = assignment_checksum(message)
                node._accept_assignment(message)
                self.assertFalse(node._assignment_acked)
                node.fixed_targets_pub.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
