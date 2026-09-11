import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from su17_competition_executor.task_protocol import assignment_checksum

rospy = types.ModuleType("rospy")
rospy.is_shutdown = lambda: False
rospy.loginfo = Mock()
rospy.logerr = Mock()
sys.modules.setdefault("rospy", rospy)
for name in ("prometheus_msgs", "std_msgs"):
    sys.modules.setdefault(name, types.ModuleType(name))
    module = types.ModuleType(name + ".msg")
    for cls in ("UAVCommand", "UAVControlState", "UAVState", "String", "Float64MultiArray", "Int32"):
        setattr(module, cls, type(cls, (), {}))
    sys.modules.setdefault(name + ".msg", module)
spec = importlib.util.spec_from_file_location("executor", Path(__file__).resolve().parents[1] / "scripts" / "onboard_task_executor.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AutonomyTest(unittest.TestCase):
    def executor(self, directory):
        node = module.OnboardTaskExecutor.__new__(module.OnboardTaskExecutor)
        node.uav_id = 3
        node._lock = threading.RLock()
        node._abort_motion = threading.Event()
        node._pending_execute = None
        node._motion_thread = None
        node._boot_id = "same-boot"
        node.cache_path = str(Path(directory) / "task.json")
        node._home = (0., 0., 0., 0.)
        node._gps_home = (30.0, 103.0, 500.0)
        node._progress = {"phase": "executing", "next_waypoint": 1}
        node._assignment = {"type": "assign_task", "uav_id": 3, "mission_id": "test", "target_altitude_m": .5, "task": {"type": "lawnmower_search", "coordinate_frame": "ENU", "waypoints_m": [[0, 0], [1, 0], [1, 1]]}}
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        node._snapshot = lambda: (types.SimpleNamespace(position=[0, 0, .5], velocity=[0, 0, 0], attitude=[0, 0, 0]), None)
        node.altitude_tolerance = .12
        node.velocity_tolerance = .15
        node.search_speed = .3
        node._publish_status = Mock()
        node._hover = Mock()
        node._fly_to = Mock(return_value=(True, "reached"))
        node._run_return = Mock()
        node.external_mission_pub = Mock()
        node.external_path_pub = Mock()
        node.external_landing_pub = Mock()
        return node

    def test_resume_remaining_waypoints_and_return_without_ground(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.executor(directory)
            node._checkpoint("executing", execution={"deadline_at": 9999999999})
            node._assignment = None
            node._restore_progress()
            self.assertTrue(node._resume_pending)
            self.assertTrue(node._run_task(node._progress["execution"]))
            self.assertEqual([c.args[:2] for c in node._fly_to.call_args_list], [(1, 0), (1, 1)])
            node._run_return.assert_called_once_with({"reason": "task_complete", "land_after_return": True})

    def test_takeoff_chains_to_task_without_execute_message(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.executor(directory)
            node._run_task = Mock(return_value=True)
            payload = {"mission_id": "test", "deadline_at": 9999999999}
            node._motion_entry("takeoff", lambda _: True, payload)
            node._run_task.assert_called_once_with(payload)

    def test_changed_boot_does_not_resume_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.executor(directory)
            node._checkpoint("executing")
            node._boot_id = "new-boot"
            node._restore_progress()
            self.assertFalse(node._resume_pending)

    def test_external_mode_publishes_wgs84_without_prometheus_waypoints(self):
        node = self.executor(tempfile.gettempdir())
        node._assignment["controller_mode"] = "external"
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        original = module.String
        module.String = lambda data: types.SimpleNamespace(data=data)
        try:
            node._publish_external_mission({"deadline_at": 123.0})
        finally:
            module.String = original
        payload = json.loads(node.external_mission_pub.publish.call_args.args[0].data)
        self.assertEqual(payload["coordinate_frame"], "WGS84")
        self.assertEqual(payload["relative_altitude_m"], 0.5)
        self.assertEqual(len(payload["waypoints"]), 3)


if __name__ == "__main__":
    unittest.main()
