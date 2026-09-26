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
    for cls in ("UAVCommand", "UAVControlState", "UAVState", "String", "Float64MultiArray", "Int32", "Bool"):
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
        node.return_speed = .3
        node._publish_status = Mock()
        node._hover = Mock()
        node._fly_to = Mock(return_value=(True, "reached"))
        node._run_return = Mock()
        node._land = Mock()
        node.external_mission_pub = Mock()
        node.external_path_pub = Mock()
        node.external_landing_pub = Mock()
        node.recon_start_mode_pub = Mock()
        node.image_mission_control_pub = Mock()
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

    def test_takeoff_starts_image_mission_with_same_mission_id(self):
        node = self.executor(tempfile.gettempdir())
        original = module.String
        module.String = lambda data: types.SimpleNamespace(data=data)
        try:
            node._publish_image_mission_start({"mission_id": "subject1-test"})
        finally:
            module.String = original
        message = node.image_mission_control_pub.publish.call_args.args[0]
        self.assertEqual(message.data, "start:subject1-test")

    def test_internal_route_returns_to_recorded_takeoff_point_and_lands(self):
        node = self.executor(tempfile.gettempdir())
        node._home = (0.25, -0.35, 0.0, 0.4)
        node._progress = {"phase": "executing", "next_waypoint": 0}
        node._assignment["flight_profile"] = "lab"
        node._assignment["task"].update(
            reconnaissance_mode="hover_scan",
            hover_scan_seconds=10.0,
            speed_mps=0.2,
        )
        node._checkpoint = Mock()
        node._scan_waypoint = Mock(return_value=(True, "scanned"))
        node._run_return = module.OnboardTaskExecutor._run_return.__get__(node)

        self.assertTrue(node._run_task({"deadline_at": 9999999999}))
        self.assertEqual(node._scan_waypoint.call_count, 3)
        self.assertEqual(node._fly_to.call_args_list[-1].args[:4], (0.25, -0.35, 0.5, 0.4))
        node._land.assert_called_once_with()

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

    def test_external_lab_mode_publishes_local_enu_without_gps(self):
        node = self.executor(tempfile.gettempdir())
        node._assignment["controller_mode"] = "external"
        node._assignment["coordinate_frame"] = "ENU"
        node._gps_home = None
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        original = module.String
        module.String = lambda data: types.SimpleNamespace(data=data)
        try:
            node._publish_external_mission({"deadline_at": 123.0})
        finally:
            module.String = original
        payload = json.loads(node.external_mission_pub.publish.call_args.args[0].data)
        self.assertEqual(payload["coordinate_frame"], "ENU")
        self.assertEqual(payload["waypoints"][0], {"x_m": 0.0, "y_m": 0.0, "z_m": 0.5})
        landing = node.external_landing_pub.publish.call_args.args[0]
        self.assertEqual(landing.data, [0.0, 0.0, 0.5])

    def test_external_landing_topic_publishes_xyz_without_sequence(self):
        node = self.executor(tempfile.gettempdir())
        node._assignment["controller_mode"] = "external"
        node._assignment["coordinate_frame"] = "ENU"
        node._assignment["landing_point_m"] = [2.0, 3.0]
        node._assignment["landing_sequence"] = 6
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        original_string = module.String
        original_array = module.Float64MultiArray
        module.String = lambda data: types.SimpleNamespace(data=data)
        module.Float64MultiArray = lambda: types.SimpleNamespace(data=[])
        try:
            node._publish_external_mission({"deadline_at": 123.0})
        finally:
            module.String = original_string
            module.Float64MultiArray = original_array
        message = node.external_landing_pub.publish.call_args.args[0]
        self.assertEqual(message.data, [2.0, 3.0, 0.5])

    def test_lab_internal_scan_uses_timed_hover_without_external_ack(self):
        node = self.executor(tempfile.gettempdir())
        node.vehicle_config = {"scan_mode": "external_ack"}
        node._assignment["flight_profile"] = "lab"
        self.assertFalse(node._scan_requires_ack())
        node._assignment["flight_profile"] = "competition"
        self.assertTrue(node._scan_requires_ack())

    def test_external_task_is_published_before_program_b_start_signal(self):
        node = self.executor(tempfile.gettempdir())
        node._assignment["controller_mode"] = "external"
        order = []
        node._publish_external_mission = Mock(side_effect=lambda _: order.append("mission"))
        node._publish_recon_start_mode = Mock(side_effect=lambda _: order.append("start"))
        node._checkpoint = Mock()
        node._motion_entry("takeoff", lambda _: True, {"mission_id": "test"})
        self.assertEqual(order, ["mission", "start"])
        node._publish_recon_start_mode.assert_called_once_with(0)

    def test_external_return_publishes_single_bool_command(self):
        node = self.executor(tempfile.gettempdir())
        node._assignment["controller_mode"] = "external"
        node.external_return_pub = Mock()
        node.external_return_topic = "/ground_mission_planner/vehicle_3/return_home"
        node._checkpoint = Mock()
        node._run_return = module.OnboardTaskExecutor._run_return.__get__(node)
        original = module.Bool
        module.Bool = lambda data=False: types.SimpleNamespace(data=data)
        try:
            node._run_return({"reason": "manual", "land_after_return": True})
        finally:
            module.Bool = original
        message = node.external_return_pub.publish.call_args.args[0]
        self.assertTrue(message.data)
        node._checkpoint.assert_called_once_with("external_return_requested", reason="manual")
        node._fly_to.assert_not_called()

    def test_global_task_records_anchor_and_uses_relative_altitude(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.executor(directory)
            node._ready = Mock(return_value=(True, "ready"))
            node.max_distance_from_home = 2000
            node.recon_start_mode_pub = Mock()
            node._assignment["target_altitude_m"] = 10
            node._assignment["task"] = {"type":"lawnmower_search", "coordinate_frame":"LOCAL_NORTH_WEST",
                "waypoints_m":[[900,800]], "waypoints_wgs84":[[33,113]]}
            state = types.SimpleNamespace(position=[100,200,5], velocity=[0,0,0], attitude=[0,0,0],
                odom_valid=True,gps_status=6,location_source=5,latitude=33,longitude=113,altitude=80)
            node._snapshot = lambda:(state,None)
            self.assertTrue(node._run_takeoff({"target_altitude_m":10}))
            self.assertEqual(node._fly_to.call_args.args[:3], (100,200,15))
            self.assertEqual(node._progress["resolved_waypoints"],[[100,200]])
            state.position[2] = 15
            node._fly_to.reset_mock()
            self.assertTrue(node._run_task({}))
            self.assertEqual(node._fly_to.call_args.args[:3],(100,200,15))

    def test_failed_scan_does_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            node=self.executor(directory)
            node._assignment['task'].update(reconnaissance_mode='hover_scan',hover_scan_seconds=10)
            node._scan_waypoint=Mock(return_value=(False,'扫描失败或未收到扫描完成确认'))
            self.assertFalse(node._run_task({}))
            self.assertEqual(node._progress['next_waypoint'],1)
            node._run_return.assert_not_called()

    def test_indoor_location_source_rejects_geographic_mission(self):
        state=types.SimpleNamespace(odom_valid=True,gps_status=6,location_source=10,latitude=33,longitude=113,altitude=80)
        with self.assertRaisesRegex(ValueError,'GPS/RTK'):
            module.OnboardTaskExecutor._valid_gps(state)


if __name__ == "__main__":
    unittest.main()
