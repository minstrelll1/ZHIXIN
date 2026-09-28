import importlib.util
import json
import struct
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from su17_competition_executor.task_protocol import assignment_checksum

rospy = types.ModuleType("rospy")
rospy.is_shutdown = lambda: False
rospy.loginfo = Mock()
rospy.logerr = Mock()
rospy.logwarn = Mock()
sys.modules.setdefault("rospy", rospy)
for name in ("prometheus_msgs", "std_msgs", "sensor_msgs", "mavros_msgs"):
    sys.modules.setdefault(name, types.ModuleType(name))
    module = types.ModuleType(name + ".msg")
    for cls in ("UAVCommand", "UAVControlState", "UAVState", "UAVSetup", "RCIn", "String", "Float64MultiArray", "Int32", "Bool", "NavSatFix"):
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
        node._state = None
        node._gps_fix = None
        node._gps_fix_received = None
        node._ground_origin_z = None
        node._ground_origin_source = ""
        node._progress = {"phase": "executing", "next_waypoint": 1}
        node._assignment = {"type": "assign_task", "uav_id": 3, "mission_id": "test", "target_altitude_m": .5, "task": {"type": "lawnmower_search", "coordinate_frame": "ENU", "waypoints_m": [[0, 0], [1, 0], [1, 1]]}}
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        node._snapshot = lambda: (types.SimpleNamespace(position=[0, 0, .5], velocity=[0, 0, 0], attitude=[0, 0, 0]), None)
        node.altitude_tolerance = .12
        node.velocity_tolerance = .15
        node.search_speed = .3
        node.return_speed = .3
        node._takeoff_precheck = Mock(return_value=(True, "ready"))
        node._ensure_command_control = Mock(return_value=(True, "ready"))
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
        self.assertEqual(payload["target_altitude_m"], 0.5)
        self.assertEqual(payload["altitude_frame"], "RELATIVE_TO_TAKEOFF")
        self.assertTrue(all(point["altitude_m"] == 0.5 for point in payload["waypoints"]))
        self.assertEqual(node.external_path_pub.publish.call_args.args[0].data[2::3], [0.5] * 3)
        self.assertEqual(node.external_landing_pub.publish.call_args.args[0].data, [103.0, 30.0, 0.5])
        self.assertEqual(payload["return_home"]["altitude_m"], 0.5)
        self.assertEqual(len(payload["waypoints"]), 3)

    def test_external_gps_uses_selected_relative_height_for_direct_and_legacy_points(self):
        original = module.String
        module.String = lambda data: types.SimpleNamespace(data=data)
        try:
            for key, points in (
                ("waypoints_wgs84", [[30.123, 103.456]]),
                ("waypoints_wgs84", [[30.123, 103.456, 44.598]]),
                ("waypoints_gps", [{"latitude": 30.123, "longitude": 103.456, "altitude_m": 44.598}]),
            ):
                for height in (0.5, 1.5, 4.5, 40.0):
                    with self.subTest(key=key, points=points, height=height):
                        node = self.executor(tempfile.gettempdir())
                        node._gps_home = (30.0, 103.0, 44.098)
                        node._assignment.update(controller_mode="external", coordinate_frame="WGS84",
                                                target_altitude_m=height, landing_point_wgs84=[30.0, 103.0, 44.598])
                        node._assignment["task"][key] = points
                        node._publish_external_mission({})
                        payload = json.loads(node.external_mission_pub.publish.call_args.args[0].data)
                        self.assertEqual(node.external_path_pub.publish.call_args.args[0].data,
                                         [103.456, 30.123, height])
                        self.assertEqual(node.external_landing_pub.publish.call_args.args[0].data,
                                         [103.0, 30.0, height])
                        self.assertEqual(payload["target_altitude_m"], height)
                        self.assertEqual(payload["relative_altitude_m"], height)
                        self.assertEqual(payload["return_home"]["altitude_m"], height)
                        self.assertEqual(payload["waypoints"][0], {"latitude": 30.123, "longitude": 103.456, "altitude_m": height})
        finally:
            module.String = original

    def test_external_gps_converts_local_landing_without_adding_sea_level_altitude(self):
        node = self.executor(tempfile.gettempdir())
        node._gps_home = (30.0, 103.0, 44.098)
        node._assignment.update(controller_mode="external", coordinate_frame="WGS84", landing_point_m=[2.0, 3.0])
        original = module.String
        module.String = lambda data: types.SimpleNamespace(data=data)
        try:
            node._publish_external_mission({})
        finally:
            module.String = original
        landing = node.external_landing_pub.publish.call_args.args[0].data
        self.assertAlmostEqual(landing[0], 103.0 - 3.0 / (111111.0 * module.math.cos(module.math.radians(30.0))))
        self.assertAlmostEqual(landing[1], 30.0 + 2.0 / 111111.0)
        self.assertEqual(landing[2], 0.5)

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

    def gps_height_executor(self, directory, controller="external"):
        node = self.executor(directory)
        node._ready = Mock(return_value=(True, "ready"))
        node.max_distance_from_home = 2000
        node._assignment.update(target_altitude_m=1.5, controller_mode=controller, coordinate_frame="WGS84")
        node._assignment["task"].update(coordinate_frame="LOCAL_NORTH_WEST", waypoints_m=[[0, 0]],
                                         waypoints_wgs84=[[33, 113]])
        node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
        node._snapshot = lambda: (node._state, None)
        return node

    def gps_state(self, z, armed, rel_alt):
        return types.SimpleNamespace(uav_id=3, connected=True, position=[100, 200, z],
                                     velocity=[0, 0, 0], attitude=[0, 0, 0], armed=armed,
                                     odom_valid=True, gps_status=6, location_source=5,
                                     latitude=33, longitude=113, altitude=40 + rel_alt, rel_alt=rel_alt)

    def test_gps_takeoff_does_not_add_height_to_existing_command_control_hover(self):
        for controller in ("internal", "external"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(controller=controller):
                node = self.gps_height_executor(directory, controller)
                node._state_callback(self.gps_state(0.410475, False, 0.0))
                node._state_callback(self.gps_state(0.410475, True, 0.0))
                node._state_callback(self.gps_state(1.907061, True, 1.496586))
                self.assertTrue(node._run_takeoff({"target_altitude_m": 1.5}))
                self.assertAlmostEqual(node._home[2], 0.410475)
                self.assertAlmostEqual(node._fly_to.call_args.args[2], 1.910475)
                self.assertAlmostEqual(node._target_z(node._assignment), 1.910475)
                status = node._publish_status.call_args.kwargs
                self.assertEqual(status["target_altitude_m"], 1.5)
                self.assertAlmostEqual(status["target_local_z_m"], 1.910475)
                self.assertAlmostEqual(status["ground_origin_z_m"], 0.410475)

    def test_gps_starting_in_air_recovers_ground_origin_from_relative_altitude(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.gps_height_executor(directory)
            node._state_callback(self.gps_state(1.907061, True, 1.496586))
            self.assertIsNone(node._ground_origin_z)
            self.assertTrue(node._run_takeoff({"target_altitude_m": 1.5}))
            self.assertAlmostEqual(node._home[2], 0.410475)
            self.assertAlmostEqual(node._fly_to.call_args.args[2], 1.910475)
            self.assertEqual(node._publish_status.call_args.kwargs["altitude_reference_source"], "GPS 相对高度反推")

    def test_gps_airborne_without_any_ground_reference_does_not_issue_a_takeoff_target(self):
        for relative in (None, float("nan")):
            with tempfile.TemporaryDirectory() as directory, self.subTest(relative=relative):
                node = self.gps_height_executor(directory)
                node._state = self.gps_state(1.9, True, 1.5)
                node._state.rel_alt = relative
                self.assertFalse(node._run_takeoff({"target_altitude_m": 1.5}))
                node._fly_to.assert_not_called()
                self.assertEqual(node._publish_status.call_args.args[0], "takeoff_failed")

    def test_gps_external_manual_enu_route_still_uses_relative_takeoff_height(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.gps_height_executor(directory)
            node._assignment["task"]["coordinate_frame"] = "ENU"
            node._state = self.gps_state(1.907061, True, 1.496586)
            self.assertTrue(node._run_takeoff({"target_altitude_m": 1.5}))
            self.assertAlmostEqual(node._fly_to.call_args.args[2], 1.910475)
            self.assertAlmostEqual(node._target_z(node._assignment), 1.910475)

    def test_saved_ground_origin_survives_same_boot_task_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.gps_height_executor(directory)
            node._state_callback(self.gps_state(0.410475, False, 0.0))
            node._state_callback(self.gps_state(1.907061, True, 1.496586))
            self.assertTrue(node._run_takeoff({"target_altitude_m": 1.5}))
            restored = self.gps_height_executor(directory)
            restored._restore_progress()
            self.assertAlmostEqual(restored._ground_origin_z, 0.410475)
            self.assertAlmostEqual(restored._target_z(restored._assignment), 1.910475)

    def test_old_gps_cache_cannot_resume_with_airborne_home_height(self):
        for frame in ("LOCAL_NORTH_WEST", "ENU"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(frame=frame):
                node = self.gps_height_executor(directory)
                node._assignment["task"]["coordinate_frame"] = frame
                node._assignment["assignment_checksum"] = assignment_checksum(node._assignment)
                node._home = (100, 200, 1.907061, 0)
                node._progress["resolved_waypoints"] = [[100, 200]]
                node._checkpoint("taking_off", execution={"target_altitude_m": 1.5})
                restored = self.gps_height_executor(directory)
                restored._restore_progress()
                self.assertFalse(restored._resume_pending)
                self.assertIsNone(restored._home)
                self.assertIsNone(restored._gps_home)
                self.assertNotIn("resolved_waypoints", restored._progress)
                self.assertFalse(restored._run_task({}))
                restored._fly_to.assert_not_called()

    def test_gps_cache_restores_home_height_from_saved_ground_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.gps_height_executor(directory)
            node._home = (100, 200, 1.907061, 0)
            node._ground_origin_z = 0.410475
            node._checkpoint("taking_off", execution={"target_altitude_m": 1.5})
            restored = self.gps_height_executor(directory)
            restored._restore_progress()
            self.assertTrue(restored._resume_pending)
            self.assertAlmostEqual(restored._home[2], 0.410475)
            self.assertAlmostEqual(restored._target_z(restored._assignment), 1.910475)

    def test_ground_origin_is_not_restored_from_another_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.gps_height_executor(directory)
            node._ground_origin_z = 10.0
            node._checkpoint("executing")
            restored = self.gps_height_executor(directory)
            restored._boot_id = "new-boot"
            restored._restore_progress()
            self.assertIsNone(restored._ground_origin_z)
            self.assertFalse(restored._resume_pending)

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


class PreciseGpsTelemetryTest(unittest.TestCase):
    @staticmethod
    def fix(**values):
        fields = {
            "latitude": 33.37994425249266,
            "longitude": 113.52849160864327,
            "altitude": 44.09814762078591,
            "status": types.SimpleNamespace(status=0),
            # 卫星时间与本机时间可能不同；新鲜度只使用本机接收时间。
            "header": types.SimpleNamespace(stamp=-1000000),
        }
        fields.update(values)
        return types.SimpleNamespace(**fields)

    def test_gps_payload_retains_float64_precision_through_json(self):
        fix = self.fix()
        payload = module.OnboardTaskExecutor._gps_position_payload(fix, 100.0, 100.25)
        wire = json.loads(json.dumps(payload, allow_nan=False))
        self.assertEqual(wire, {
            "latitude": fix.latitude,
            "longitude": fix.longitude,
            "altitude": fix.altitude,
            "source": "mavros_global",
            "age_seconds": 0.25,
        })
        for coordinate in ("latitude", "longitude"):
            quantized = struct.unpack("f", struct.pack("f", getattr(fix, coordinate)))[0]
            self.assertNotEqual(wire[coordinate], quantized)

    def test_gps_payload_freshness_includes_two_seconds_boundary(self):
        fix = self.fix()
        for now in (100.0, 101.5, 102.0):
            with self.subTest(now=now):
                self.assertIsNotNone(module.OnboardTaskExecutor._gps_position_payload(fix, 100.0, now))
        for now in (99.9, 102.000001, float("nan"), float("inf")):
            with self.subTest(now=now):
                self.assertIsNone(module.OnboardTaskExecutor._gps_position_payload(fix, 100.0, now))
        self.assertIsNone(module.OnboardTaskExecutor._gps_position_payload(None, 100.0, 100.0))
        self.assertIsNone(module.OnboardTaskExecutor._gps_position_payload(fix, None, 100.0))
        self.assertIsNotNone(module.OnboardTaskExecutor._gps_position_payload(fix, 0.0, 0.5))

    def test_gps_payload_rejects_no_fix_and_invalid_coordinates(self):
        invalid = (
            {"status": types.SimpleNamespace(status=-1)},
            {"latitude": float("nan")}, {"latitude": float("inf")},
            {"longitude": float("nan")}, {"longitude": float("-inf")},
            {"latitude": -90.000001}, {"latitude": 90.000001},
            {"longitude": -180.000001}, {"longitude": 180.000001},
            {"latitude": None}, {"longitude": "invalid"}, {"status": None},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertIsNone(module.OnboardTaskExecutor._gps_position_payload(self.fix(**fields), 100.0, 100.1))
        for latitude, longitude in ((0.0, 0.0), (-90.0, -180.0), (90.0, 180.0)):
            for status in (0, 1, 2):
                with self.subTest(latitude=latitude, longitude=longitude, status=status):
                    self.assertIsNotNone(module.OnboardTaskExecutor._gps_position_payload(
                        self.fix(latitude=latitude, longitude=longitude, status=types.SimpleNamespace(status=status)), 100.0, 100.1))

    def test_gps_payload_converts_invalid_altitude_to_null(self):
        for altitude in (None, float("nan"), float("inf"), float("-inf"), "invalid"):
            with self.subTest(altitude=altitude):
                payload = module.OnboardTaskExecutor._gps_position_payload(self.fix(altitude=altitude), 100.0, 100.1)
                self.assertIsNone(payload["altitude"])
                json.dumps(payload, allow_nan=False)
        fix = self.fix()
        del fix.altitude
        self.assertIsNone(module.OnboardTaskExecutor._gps_position_payload(fix, 100.0, 100.1)["altitude"])

    def test_fix_callback_only_updates_map_cache_with_monotonic_time(self):
        node = AutonomyTest().executor(tempfile.gettempdir())
        previous = (node._state, node._home, node._gps_home, node._assignment.copy())
        fix = self.fix()
        with patch.object(module.time, "monotonic", return_value=123.5):
            node._gps_fix_callback(fix)
        self.assertIs(node._gps_fix, fix)
        self.assertEqual(node._gps_fix_received, 123.5)
        self.assertEqual((node._state, node._home, node._gps_home, node._assignment), previous)
        invalid = self.fix(status=types.SimpleNamespace(status=-1))
        with patch.object(module.time, "monotonic", return_value=124.0):
            node._gps_fix_callback(invalid)
        self.assertIsNone(node._gps_position_payload(node._gps_fix, node._gps_fix_received, 124.1))

    def test_subscription_uses_local_ros_id_and_is_read_only(self):
        parameters = {"~uav_id": 3, "~local_ros_uav_id": 7, "~cache_path": "unused.json", "~transport": "ros"}
        subscribe = Mock()
        publish = Mock()
        with patch.multiple(rospy, create=True,
                            get_param=Mock(side_effect=lambda key, default=None: parameters.get(key, default)),
                            Subscriber=subscribe, Publisher=publish, Timer=Mock(), Duration=Mock(),
                            Time=types.SimpleNamespace(now=lambda: types.SimpleNamespace(to_sec=lambda: 123.0))), \
             patch.dict(module.os.environ, {"COMPETITION_ONBOARD_IDENTITY": "{}", "COMPETITION_ONBOARD_VEHICLE": "{}"}), \
             patch.object(module.Path, "read_text", return_value="test-boot"), \
             patch.object(module.OnboardTaskExecutor, "_restore_progress"), \
             patch.object(module.OnboardTaskExecutor, "_publish_status"):
            node = module.OnboardTaskExecutor()
        gps_calls = [call for call in subscribe.call_args_list if "mavros/global_position/global" in call.args[0]]
        self.assertEqual(len(gps_calls), 1)
        self.assertEqual(gps_calls[0].args, ("/uav7/mavros/global_position/global", module.NavSatFix, node._gps_fix_callback))
        self.assertEqual(gps_calls[0].kwargs, {"queue_size": 1})
        self.assertFalse(any("mavros/global_position/global" in call.args[0] for call in publish.call_args_list))

    def test_tcp_telemetry_adds_precise_gps_and_preserves_uavstate_fields(self):
        node = AutonomyTest().executor(tempfile.gettempdir())
        state = types.SimpleNamespace(connected=True, armed=False, odom_valid=True,
                                      battery_percetage=0.9, position=[1.0, 2.0, 3.0], velocity=[0.0, 0.0, 0.0],
                                      latitude=33.37994384765625, longitude=113.52848815917969, altitude=44.0, rel_alt=0.5)
        control = types.SimpleNamespace(failsafe=False, control_state=1)
        node._snapshot = lambda: (state, control)
        node.tcp_link = Mock()
        node._identity_error = ""
        node._state_received = node._control_received = 100.0
        node.enable_motion = False
        node.max_speed = 2.0
        node.flight_speed_limit = None
        node.scan_pub = Mock()
        node.scan_pub.get_num_connections.return_value = 0
        node.vehicle_config = {}
        node._image_health_received = 0.0
        node._image_health = {}
        node._assignment = None
        node._assignment_acked = False
        node._gps_fix = self.fix()
        node._gps_fix_received = 100.0
        for now, has_gps in ((100.5, True), (102.000001, False)):
            with self.subTest(now=now), patch.object(module.time, "monotonic", return_value=now):
                node._tcp_telemetry_timer(None)
                telemetry = node.tcp_link.update_telemetry.call_args.args[0]
                for field in ("latitude", "longitude", "altitude", "rel_alt"):
                    self.assertEqual(telemetry[field], getattr(state, field))
                self.assertEqual(telemetry["gps_position"] is not None, has_gps)
                if has_gps:
                    self.assertEqual(telemetry["gps_position"]["latitude"], node._gps_fix.latitude)
                    self.assertEqual(telemetry["gps_position"]["longitude"], node._gps_fix.longitude)
                    self.assertEqual(telemetry["gps_position"]["source"], "mavros_global")


if __name__ == "__main__":
    unittest.main()
