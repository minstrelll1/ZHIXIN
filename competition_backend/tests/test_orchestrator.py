import unittest

from competition_backend.adapter import RecordingAdapter
from competition_backend.models import (
    BackendConfig,
    MissionPhase,
    SafetyConfig,
    Telemetry,
)
from competition_backend.orchestrator import CompetitionOrchestrator
from competition_backend.search_planner import _partition_lanes


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make_config():
    return BackendConfig(
        uav_ids=[1, 2, 3, 4, 5, 6],
        takeoff_altitudes_m={uav_id: 0.7 + 0.3 * uav_id for uav_id in range(1, 7)},
        safety=SafetyConfig(
            production_config_confirmed=True,
            require_armed_for_takeoff=True,
            required_control_state=2,
            preflight_battery_min=0.6,
            return_battery_threshold=0.3,
            telemetry_max_age_seconds=2.0,
            altitude_tolerance_m=0.15,
            vertical_speed_tolerance_mps=0.15,
            takeoff_timeout_seconds=60.0,
            confirmation_ttl_seconds=15.0,
        ),
        subjects={
            "subject1": {
                "duration_seconds": 100.0,
                "tasks_by_uav": {
                    str(uav_id): {"type": "search", "sector": uav_id}
                    for uav_id in range(1, 7)
                },
            }
        },
    )


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.adapter = RecordingAdapter()
        self.config = make_config()
        self.backend = CompetitionOrchestrator(
            self.config, self.adapter, live_mode=True, clock=self.clock
        )

    def test_partial_assignment_failure_records_which_uav_was_sent(self):
        original = self.adapter.command_assign_task

        def send(uav_id, payload):
            if uav_id == 2:
                raise TimeoutError("机载链路超时")
            original(uav_id, payload)

        self.adapter.command_assign_task = send
        with self.assertRaisesRegex(RuntimeError, "UAV2 任务发送失败；已发往 \\[1\\]"):
            self.backend.plan("subject1")
        mission = self.backend.snapshot()["mission"]
        self.assertEqual([command["uav_id"] for command in self.adapter.snapshot()], [1])
        self.assertEqual(mission["events"][-1]["kind"], "task_assignment_failed")
        self.assertEqual(mission["events"][-1]["sent_uav_ids"], [1])

    def test_selected_return_does_not_change_other_uavs(self):
        self.launch_and_start_tasks()
        before = self.backend.snapshot()['mission']
        result = self.backend.request_return_selected([1, 3], mission_id=before['mission_id'])
        returns = [c['uav_id'] for c in self.adapter.commands if c['type'] == 'return_home']
        self.assertEqual(returns, [1, 3])
        self.assertTrue(result['return_results']['1']['ok'])
        self.assertEqual(result['mission']['uavs']['2'], before['uavs']['2'])
        self.assertEqual(result['mission']['phase'], before['phase'])

    def test_selected_return_continues_after_failure_and_can_retry_failed_uav(self):
        self.launch_and_start_tasks()
        original = self.adapter.command_return
        def send(uid, payload):
            if uid == 1:
                raise TimeoutError('链路超时')
            original(uid, payload)
        self.adapter.command_return = send
        result = self.backend.request_return_selected([1, 3])
        self.assertFalse(result['return_results']['1']['ok'])
        self.assertTrue(result['return_results']['3']['ok'])
        self.assertNotEqual(result['mission']['uavs']['1']['phase'], 'return_commanded')
        self.adapter.command_return = original
        self.backend.request_return_selected([1])
        self.assertEqual([c['uav_id'] for c in self.adapter.commands if c['type'] == 'return_home'], [3, 1])

    def test_stale_return_dialog_cannot_control_new_mission(self):
        self.launch_and_start_tasks()
        with self.assertRaisesRegex(RuntimeError, '任务已变化'):
            self.backend.request_return_selected([1], mission_id='old-task')
        self.assertFalse(any(c['type'] == 'return_home' for c in self.adapter.commands))

    def telemetry(self, uav_id, **overrides):
        mission = self.backend.snapshot().get("mission")
        values = {
            "uav_id": uav_id,
            "received_at": self.clock(),
            "connected": True,
            "armed": True,
            "odom_valid": True,
            "failsafe": False,
            "control_state": 2,
            "battery_percentage": 0.9,
            "position": [0.0, 0.0, 0.0],
            "velocity": [0.0, 0.0, 0.0],
            "task_complete": False,
            "task_assignment_acked": True,
            "task_assignment_mission_id": mission["mission_id"] if mission else "",
            "task_assignment_checksum": (
                mission["uavs"][str(uav_id)]["assignment_checksum"]
                if mission else ""
            ),
        }
        values.update(overrides)
        self.backend.update_telemetry(Telemetry(**values))

    def plan_and_prepare(self):
        self.backend.plan("subject1")
        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id)
        prepared = self.backend.prepare_takeoff()
        self.assertTrue(prepared["ready"])
        return prepared

    def launch_and_start_tasks(self):
        prepared = self.plan_and_prepare()
        self.backend.confirm_takeoff(prepared["confirmation_token"])
        self.assertEqual(
            len([command for command in self.adapter.commands if command["type"] == "takeoff"]),
            6,
        )
        for uav_id in self.config.uav_ids:
            altitude = self.config.takeoff_altitudes_m[uav_id]
            self.telemetry(
                uav_id,
                position=[0.0, 0.0, altitude],
                velocity=[0.0, 0.0, 0.0],
            )
        self.backend.tick()

    def test_plan_assigns_six_unique_altitudes(self):
        snapshot = self.backend.plan("subject1")
        uavs = snapshot["mission"]["uavs"]
        self.assertEqual(set(uavs), set(str(value) for value in range(1, 7)))
        self.assertEqual(len({item["target_altitude_m"] for item in uavs.values()}), 6)
        assignments = [
            command for command in self.adapter.commands
            if command["type"] == "assign_task"
        ]
        self.assertEqual([command["uav_id"] for command in assignments], list(range(1, 7)))
        self.assertTrue(all(command["payload"]["task"] for command in assignments))

    def test_planning_only_without_connected_uavs_keeps_six_routes_and_sends_nothing(self):
        self.backend.set_active_uav_ids([])
        snapshot = self.backend.plan("subject1")
        mission = snapshot["mission"]
        self.assertEqual(snapshot["active_uav_ids"], [])
        self.assertEqual(mission["uavs"], {})
        self.assertEqual(set(mission["planned_uavs"]), {str(value) for value in range(1, 7)})
        self.assertEqual(
            [command for command in self.adapter.commands if command["type"] == "assign_task"],
            [],
        )
        self.assertEqual(snapshot["dispatch_status"]["mode"], "planning_only")
        prepared = self.backend.prepare_takeoff()
        self.assertFalse(prepared["ready"])
        self.assertIn(
            "no UAV is connected; this mission is planning-only",
            prepared["preflight"]["failures"]["system"],
        )

    def test_assignment_is_frozen_after_one_click_takeoff(self):
        prepared = self.plan_and_prepare()
        self.backend.confirm_takeoff(prepared["confirmation_token"])
        before = len([command for command in self.adapter.commands if command["type"] == "assign_task"])
        # 起飞后即使遥测显示未确认，也不能用回调重新覆盖机载任务。
        self.telemetry(1, task_assignment_acked=False, task_assignment_mission_id="", task_assignment_checksum="")
        after = len([command for command in self.adapter.commands if command["type"] == "assign_task"])
        self.assertEqual(after, before)

    def test_external_mode_is_stored_and_sent_with_assignment(self):
        snapshot = self.backend.plan(
            "subject1",
            controller_mode="external",
            gps_origin={"latitude": 30.0, "longitude": 103.0, "altitude_m": 500.0},
        )
        self.assertEqual(snapshot["mission"]["controller_mode"], "external")
        assignments = [
            command for command in self.adapter.commands
            if command["type"] == "assign_task"
        ]
        self.assertTrue(all(item["payload"]["controller_mode"] == "external" for item in assignments))
        self.assertTrue(all(item["payload"]["flight_profile"] == "lab" for item in assignments))
        self.assertTrue(all(item["payload"]["coordinate_frame"] == "WGS84" for item in assignments))
        self.assertTrue(all(item["payload"]["task"].get("waypoints_wgs84") for item in assignments))

    def test_external_lab_mode_uses_local_enu_without_gps_origin(self):
        tasks = {
            uav_id: {
                "type": "lawnmower_search",
                "coordinate_frame": "ENU",
                "waypoints_m": [[0.5, 0.5]],
                "speed_mps": 0.2,
                "reconnaissance_mode": "hover_scan",
                "hover_scan_seconds": 10.0,
            }
            for uav_id in self.config.uav_ids
        }
        snapshot = self.backend.plan("subject1", tasks_by_uav=tasks, controller_mode="external")
        self.assertEqual(snapshot["mission"]["controller_mode"], "external")
        assignments = [command for command in self.adapter.commands if command["type"] == "assign_task"]
        self.assertEqual(len(assignments), 6)
        self.assertTrue(all(item["payload"]["coordinate_frame"] == "ENU" for item in assignments))

    def test_explicit_gps_origin_uses_relative_route_and_landing_heights(self):
        for origin_altitude in (None, 44.0):
            for uav1_altitude in (0.5, 1.0):
                with self.subTest(origin_altitude=origin_altitude, uav1_altitude=uav1_altitude):
                    self.config.takeoff_altitudes_m[1] = uav1_altitude
                    self.adapter.commands.clear()
                    gps_origin = {"latitude": 30.0, "longitude": 103.0}
                    if origin_altitude is not None:
                        gps_origin["altitude_m"] = origin_altitude
                    mission = self.backend.plan(
                        "subject1", controller_mode="external", gps_origin=gps_origin
                    )["mission"]
                    assignments = [
                        item for item in self.adapter.commands if item["type"] == "assign_task"
                    ]
                    self.assertEqual(len(assignments), 6)
                    for assignment in assignments:
                        uav_id = assignment["uav_id"]
                        payload = assignment["payload"]
                        expected_height = self.config.takeoff_altitudes_m[uav_id]
                        self.assertEqual(payload["coordinate_frame"], "WGS84")
                        self.assertEqual(payload["target_altitude_m"], expected_height)
                        self.assertTrue(payload["task"]["waypoints_wgs84"])
                        self.assertTrue(all(
                            point[2] == expected_height
                            for point in payload["task"]["waypoints_wgs84"]
                        ))
                        self.assertEqual(payload["landing_point_wgs84"][2], expected_height)
                        self.assertEqual(
                            payload["task"]["waypoints_wgs84"],
                            mission["uavs"][str(uav_id)]["task"]["waypoints_wgs84"],
                        )
                        first_latitude, first_longitude = payload["task"]["waypoints_wgs84"][0][:2]
                        self.assertAlmostEqual(first_latitude, 30.0, delta=0.001)
                        self.assertAlmostEqual(first_longitude, 103.0, delta=0.001)
                        self.assertAlmostEqual(payload["landing_point_wgs84"][0], 30.0, delta=0.001)
                        self.assertAlmostEqual(payload["landing_point_wgs84"][1], 103.0, delta=0.001)

    def test_existing_gps_routes_replace_absolute_heights_in_plan_and_assignment(self):
        self.config.takeoff_altitudes_m[1] = 0.5
        supplied_points = [[30.0, 103.0, 44.598],
                           [30.0001, 103.0002, 500.0], [30.0003, 103.0004]]
        tasks = {
            uav_id: {
                "type": "lawnmower_search",
                "waypoints_m": [[0.0, 0.0]],
                "waypoints_wgs84": [list(point) for point in supplied_points],
            }
            for uav_id in self.config.uav_ids
        }
        mission = self.backend.plan(
            "subject1", tasks_by_uav=tasks, controller_mode="external",
            gps_origin={"latitude": 30.0, "longitude": 103.0, "altitude_m": 44.0},
        )["mission"]
        assignments = [
            item for item in self.adapter.commands if item["type"] == "assign_task"
        ]
        self.assertEqual(len(assignments), 6)
        for assignment in assignments:
            uav_id = assignment["uav_id"]
            expected_height = self.config.takeoff_altitudes_m[uav_id]
            expected_points = [[point[0], point[1], expected_height] for point in supplied_points]
            self.assertEqual(assignment["payload"]["task"]["waypoints_wgs84"], expected_points)
            self.assertEqual(mission["uavs"][str(uav_id)]["task"]["waypoints_wgs84"], expected_points)
            self.assertEqual(mission["planned_uavs"][str(uav_id)]["task"]["waypoints_wgs84"], expected_points)

    def test_gps_quadrilateral_heights_do_not_depend_on_origin_altitude(self):
        for origin_altitude in (None, 44.0):
            for uav1_altitude in (0.5, 1.0):
                with self.subTest(origin_altitude=origin_altitude, uav1_altitude=uav1_altitude):
                    self.config.takeoff_altitudes_m[1] = uav1_altitude
                    self.adapter.commands.clear()
                    self.backend.plan(
                        "subject1",
                        controller_mode="external",
                        search_area={
                            "coordinate_mode": "gps",
                            "points": [[31.2304, 121.4737], [31.2304, 121.4837],
                                       [31.2358, 121.4854], [31.2350, 121.4737]],
                            "lane_spacing_m": 150,
                            "turn_radius_m": 5,
                        },
                        landing_area={
                            "coordinate_mode": "gps",
                            "points": [[31.2307, 121.4740], [31.2314, 121.4740],
                                       [31.2314, 121.4747], [31.2307, 121.4747]],
                        },
                        gps_origin=({"altitude_m": origin_altitude}
                                    if origin_altitude is not None else None),
                    )
                    assignments = [
                        item for item in self.adapter.commands if item["type"] == "assign_task"
                    ]
                    self.assertEqual(len(assignments), 6)
                    for assignment in assignments:
                        payload = assignment["payload"]
                        expected_height = self.config.takeoff_altitudes_m[assignment["uav_id"]]
                        self.assertEqual(payload["coordinate_frame"], "WGS84")
                        self.assertTrue(payload["task"]["waypoints_wgs84"])
                        self.assertTrue(all(
                            point[2] == expected_height
                            for point in payload["task"]["waypoints_wgs84"]
                        ))
                        landing_point = payload["landing_point_wgs84"]
                        self.assertEqual(landing_point[2], expected_height)
                        self.assertGreaterEqual(landing_point[0], 31.2307)
                        self.assertLessEqual(landing_point[0], 31.2314)
                        self.assertGreaterEqual(landing_point[1], 121.4740)
                        self.assertLessEqual(landing_point[1], 121.4747)

    def test_preflight_rejects_missing_telemetry(self):
        self.backend.plan("subject1")
        result = self.backend.prepare_takeoff()
        self.assertFalse(result["ready"])
        self.assertIn("no telemetry", result["preflight"]["failures"]["1"])

    def test_lab_internal_hover_scan_does_not_require_external_scan_node(self):
        tasks = {
            uav_id: {
                "type": "lawnmower_search",
                "coordinate_frame": "ENU",
                "waypoints_m": [[0.2, 0.2]],
                "speed_mps": 0.2,
                "reconnaissance_mode": "hover_scan",
                "hover_scan_seconds": 10.0,
            }
            for uav_id in self.config.uav_ids
        }
        self.backend.plan("subject1", tasks_by_uav=tasks, flight_profile="lab")
        for uav_id in self.config.uav_ids:
            self.telemetry(
                uav_id,
                identity={"identity_verified": True},
                capabilities={
                    "motion_enabled": True,
                    "max_speed_mps": 1.0,
                    "flight_speed_limit_mps": 1.0,
                    "scan_available": False,
                },
            )
        self.assertTrue(self.backend.prepare_takeoff()["ready"])

    def test_external_program_b_route_speed_does_not_block_ground_preflight(self):
        tasks = {
            uav_id: {
                "type": "lawnmower_search", "coordinate_frame": "ENU",
                "waypoints_m": [[0.2, 0.2]], "speed_mps": 5.0,
            }
            for uav_id in self.config.uav_ids
        }
        self.backend.plan("subject1", tasks_by_uav=tasks, controller_mode="external")
        for uav_id in self.config.uav_ids:
            self.telemetry(
                uav_id, identity={"identity_verified": True},
                capabilities={"motion_enabled": True, "max_speed_mps": 1.0,
                              "flight_speed_limit_mps": 1.0},
            )
        failures = self.backend.preflight_report()["failures"]
        for uav_id in self.config.uav_ids:
            self.assertNotIn("规划航速超过当前机载或飞控速度上限", failures[str(uav_id)])

    def test_preflight_requires_task_assignment_ack(self):
        self.backend.plan("subject1")
        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id, task_assignment_acked=(uav_id != 3))
        result = self.backend.prepare_takeoff()
        self.assertFalse(result["ready"])
        self.assertIn(
            "task assignment is not acknowledged",
            result["preflight"]["failures"]["3"],
        )

    def test_preflight_rejects_ack_for_a_different_assignment_checksum(self):
        self.backend.plan("subject1")
        for uav_id in self.config.uav_ids:
            if uav_id == 3:
                self.telemetry(uav_id, task_assignment_checksum="wrong-checksum")
            else:
                self.telemetry(uav_id)
        result = self.backend.prepare_takeoff()
        self.assertFalse(result["ready"])
        self.assertIn(
            "task assignment is not acknowledged",
            result["preflight"]["failures"]["3"],
        )

    def test_lab_profile_skips_preflight_battery_threshold(self):
        self.backend.plan("subject1", flight_profile="lab")
        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id, battery_percentage=0.1)
        result = self.backend.prepare_takeoff()
        self.assertTrue(result["ready"])
        self.assertFalse(result["preflight"]["battery_threshold_enforced"])

    def test_competition_profile_keeps_preflight_battery_threshold(self):
        self.backend.plan("subject1", flight_profile="competition")
        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id, battery_percentage=0.59)
        result = self.backend.prepare_takeoff()
        self.assertFalse(result["ready"])
        self.assertTrue(result["preflight"]["battery_threshold_enforced"])
        self.assertIn(
            "battery is below preflight threshold",
            result["preflight"]["failures"]["1"],
        )

    def test_each_uav_starts_immediately_after_reaching_altitude(self):
        prepared = self.plan_and_prepare()
        self.backend.confirm_takeoff(prepared["confirmation_token"])
        self.telemetry(1, position=[0, 0, self.config.takeoff_altitudes_m[1]])
        self.backend.tick()
        tasks = [c for c in self.adapter.commands if c["type"] == "execute_task"]
        self.assertEqual(tasks, [])
        self.assertEqual(self.backend.snapshot()["mission"]["uavs"]["1"]["phase"], "executing")
        self.assertEqual(self.backend.snapshot()["mission"]["phase"], "running")

        self.telemetry(2, position=[0, 0, self.config.takeoff_altitudes_m[2]])
        self.backend.tick()
        tasks = [c for c in self.adapter.commands if c["type"] == "execute_task"]
        self.assertEqual(tasks, [])
        self.assertEqual(self.backend.snapshot()["mission"]["uavs"]["2"]["phase"], "executing")

    def test_subject1_plan_contains_six_lawnmower_routes(self):
        self.config.subjects["subject1"]["search_area"] = {
            "width_m": 3.0,
            "height_m": 3.0,
            "lane_spacing_m": 0.5,
            "sector_by_uav": {1: 1, 2: 5, 3: 3, 4: 4, 5: 2, 6: 6},
        }
        self.config.subjects["subject1"]["takeoff_altitudes_m_by_profile"] = {
            "lab": {1: 0.5, 2: 1.5, 3: 0.5, 4: 1.0, 5: 1.5, 6: 1.0},
            "competition": {1: 40, 2: 50, 3: 40, 4: 45, 5: 50, 6: 45},
        }
        mission = self.backend.plan("subject1")["mission"]
        self.assertEqual(mission["search_area"]["columns"], 3)
        self.assertEqual(mission["search_area"]["rows"], 2)
        self.assertEqual(
            mission["search_area"]["coordinate_layout"]["origin_corner"],
            "bottom_right",
        )
        for runtime in mission["uavs"].values():
            self.assertEqual(runtime["task"]["type"], "lawnmower_search")
            self.assertGreaterEqual(len(runtime["task"]["waypoints_m"]), 4)
        expected_sector = {"1": 1, "2": 5, "3": 3, "4": 4, "5": 2, "6": 6}
        expected_lab_height = {"1": 0.5, "2": 1.5, "3": 0.5, "4": 1.0, "5": 1.5, "6": 1.0}
        for uav_id, runtime in mission["uavs"].items():
            self.assertEqual(runtime["task"]["sector"], expected_sector[uav_id])
            self.assertEqual(runtime["target_altitude_m"], expected_lab_height[uav_id])
        self.assertEqual(
            mission["uavs"]["1"]["task"]["bounds_m"],
            {"x_min": 1.5, "x_max": 3.0, "y_min": 2.0, "y_max": 3.0},
        )
        self.assertEqual(
            mission["uavs"]["1"]["task"]["waypoints_m"][:2],
            [[1.5, 3.0], [1.5, 2.0]],
        )
        self.assertEqual(
            mission["uavs"]["6"]["task"]["bounds_m"],
            {"x_min": 0.0, "x_max": 1.5, "y_min": 0.0, "y_max": 1.0},
        )

    def test_competition_profile_uses_requested_heights(self):
        self.config.subjects["subject1"]["search_area"] = {
            "width_m": 1000.0,
            "height_m": 1000.0,
            "lane_spacing_m": 20.0,
            "sector_by_uav": {1: 1, 2: 5, 3: 3, 4: 4, 5: 2, 6: 6},
        }
        self.config.subjects["subject1"]["takeoff_altitudes_m_by_profile"] = {
            "lab": {1: 0.5, 2: 1.5, 3: 0.5, 4: 1.0, 5: 1.5, 6: 1.0},
            "competition": {1: 40, 2: 50, 3: 40, 4: 45, 5: 50, 6: 45},
        }
        mission = self.backend.plan("subject1", flight_profile="competition")["mission"]
        self.assertEqual(
            [mission["uavs"][str(uav_id)]["target_altitude_m"] for uav_id in range(1, 7)],
            [40.0, 50.0, 40.0, 45.0, 50.0, 45.0],
        )

    def test_plan_can_be_repeated_before_takeoff(self):
        first = self.backend.plan("subject1")["mission"]["mission_id"]
        second = self.backend.plan("subject1")["mission"]["mission_id"]
        self.assertNotEqual(first, second)
        assignments = [
            command for command in self.adapter.commands
            if command["type"] == "assign_task"
        ]
        self.assertEqual(len(assignments), 12)

        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id)
        prepared = self.backend.prepare_takeoff()
        self.assertTrue(prepared["ready"])
        third = self.backend.plan("subject1")["mission"]
        self.assertNotEqual(second, third["mission_id"])
        self.assertEqual(third["phase"], "planned")

    def test_explicit_single_uav_mode_checks_and_commands_only_uav1(self):
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(
            self.config,
            adapter,
            live_mode=True,
            clock=self.clock,
            active_uav_ids=[1],
        )
        mission = backend.plan("subject1")["mission"]
        self.assertEqual(set(mission["uavs"]), {"1"})
        self.assertEqual(set(mission["planned_uavs"]), {"1", "2", "3", "4", "5", "6"})
        self.assertEqual(
            mission["planned_uavs"]["1"]["task"]["sector"], 1
        )
        assignments = [
            item for item in adapter.commands if item["type"] == "assign_task"
        ]
        self.assertEqual([item["uav_id"] for item in assignments], [1])
        runtime = mission["uavs"]["1"]
        backend.update_telemetry(
            Telemetry(
                uav_id=1,
                received_at=self.clock(),
                connected=True,
                armed=True,
                odom_valid=True,
                control_state=2,
                battery_percentage=0.9,
                position=[0, 0, 0],
                velocity=[0, 0, 0],
                task_assignment_acked=True,
                task_assignment_mission_id=mission["mission_id"],
                task_assignment_checksum=runtime["assignment_checksum"],
            )
        )
        prepared = backend.prepare_takeoff()
        self.assertTrue(prepared["ready"])
        backend.confirm_takeoff(prepared["confirmation_token"])
        takeoffs = [item for item in adapter.commands if item["type"] == "takeoff"]
        self.assertEqual([item["uav_id"] for item in takeoffs], [1])
        snapshot = backend.snapshot()
        self.assertEqual(snapshot["active_uav_ids"], [1])
        self.assertEqual(snapshot["mission"]["planned_uavs"]["1"]["phase"], "takeoff_commanded")
        self.assertEqual(snapshot["mission"]["planned_uavs"]["2"]["phase"], "ready")

    def test_task_completion_returns_only_that_uav(self):
        self.launch_and_start_tasks()
        altitude = self.config.takeoff_altitudes_m[2]
        self.telemetry(2, position=[0, 0, altitude], task_complete=True)
        self.backend.tick()
        returns = [c for c in self.adapter.commands if c["type"] == "return_home"]
        self.assertEqual(returns, [])
        self.assertEqual(self.backend.snapshot()["mission"]["uavs"]["2"]["phase"], "return_commanded")

    def test_low_battery_marks_onboard_autonomous_return(self):
        self.launch_and_start_tasks()
        altitude = self.config.takeoff_altitudes_m[4]
        self.telemetry(4, position=[0, 0, altitude], battery_percentage=0.29)
        self.backend.tick()
        returns = [c for c in self.adapter.commands if c["type"] == "return_home"]
        self.assertEqual(returns, [])
        runtime = self.backend.snapshot()["mission"]["uavs"]["4"]
        self.assertEqual(runtime["phase"], "return_commanded")
        self.assertEqual(runtime["return_reason"], "low_battery")

    def test_time_limit_marks_all_onboard_autonomous_return(self):
        self.launch_and_start_tasks()
        self.clock.advance(101.0)
        self.backend.tick()
        returns = [c for c in self.adapter.commands if c["type"] == "return_home"]
        self.assertEqual(returns, [])
        self.assertTrue(all(
            runtime["return_reason"] == "time_limit"
            for runtime in self.backend.snapshot()["mission"]["uavs"].values()
        ))
        self.assertEqual(self.backend.snapshot()["mission"]["phase"], "returning")

    def test_mission_completes_after_all_returned_uavs_disarm(self):
        self.launch_and_start_tasks()
        self.backend.request_return_all()
        for uav_id in self.config.uav_ids:
            self.telemetry(uav_id, armed=False)
        self.backend.tick()
        self.assertEqual(
            self.backend.snapshot()["mission"]["phase"],
            MissionPhase.COMPLETED.value,
        )

    def test_xyz_quadrilateral_plan_has_balanced_six_uav_routes(self):
        mission = self.backend.plan(
            "subject1",
            search_area={
                "coordinate_mode": "xyz",
                "points": [[0, 0], [900, 0], [1050, 600], [100, 500]],
                "lane_spacing_m": 150,
                "turn_radius_m": 5,
            },
            landing_area={
                "coordinate_mode": "xyz",
                "points": [[30, -30], [180, -30], [180, 120], [30, 120]],
            },
        )["mission"]
        area = mission["search_area"]
        self.assertEqual(area["coordinate_mode"], "xyz")
        self.assertEqual(len(area["points_m"]), 4)
        self.assertGreater(area["coverage"]["total_distance_m"], 0)
        self.assertGreater(area["coverage"]["maximum_aircraft_distance_m"], 0)
        self.assertTrue(all(runtime["task"]["waypoints_m"] for runtime in mission["uavs"].values()))
        self.assertTrue(all(runtime["task"]["turn_radius_m"] == 5 for runtime in mission["uavs"].values()))

    def test_lab_quadrilateral_plan_uses_lab_spacing_and_turn_radius(self):
        mission = self.backend.plan(
            "subject1",
            flight_profile="lab",
            search_area={
                "coordinate_mode": "xyz",
                "points": [[0, 0], [3, 0], [3, 3], [0, 3]],
                "lane_spacing_m": 0.5,
                "turn_radius_m": 0.1,
            },
            landing_area={
                "coordinate_mode": "xyz",
                "points": [[0.4, -0.4], [1.4, -0.4], [1.4, 0.6], [0.4, 0.6]],
            },
        )["mission"]
        self.assertEqual(mission["search_area"]["lane_spacing_m"], 0.5)
        self.assertEqual(mission["search_area"]["turn_radius_m"], 0.1)
        self.assertTrue(
            all(runtime["task"]["turn_radius_m"] == 0.1 for runtime in mission["uavs"].values())
        )
        self.assertTrue(
            all(runtime["task"]["lane_spacing_m"] == 0.5 for runtime in mission["uavs"].values())
        )

    def test_gps_quadrilateral_plan_converts_routes_for_external_program(self):
        mission = self.backend.plan(
            "subject1",
            controller_mode="external",
            search_area={
                "coordinate_mode": "gps",
                "points": [[31.2304, 121.4737], [31.2304, 121.4837],
                           [31.2358, 121.4854], [31.2350, 121.4737]],
                "lane_spacing_m": 150,
                "turn_radius_m": 5,
            },
            landing_area={
                "coordinate_mode": "gps",
                "points": [[31.2307, 121.4740], [31.2314, 121.4740],
                           [31.2314, 121.4747], [31.2307, 121.4747]],
            },
            gps_origin={"altitude_m": 30},
        )["mission"]
        self.assertEqual(mission["search_area"]["coordinate_mode"], "gps")
        self.assertEqual(mission["search_area"]["gps_origin"]["latitude"], 31.2304)
        self.assertTrue(all(runtime["task"].get("waypoints_wgs84") for runtime in mission["uavs"].values()))

    def test_lane_partition_can_match_uavs_to_non_id_strip_order(self):
        lanes = [((0.0, float(index)), (100.0, float(index))) for index in range(6)]
        landing_points = {
            uav_id: (0.0, float(6 - uav_id)) for uav_id in self.config.uav_ids
        }
        _, _, assignments = _partition_lanes(
            lanes, self.config.uav_ids, landing_points, turn_radius_m=5.0
        )
        ranges_by_uav = {
            uav_id: (start, end) for uav_id, start, end in assignments
        }
        self.assertEqual(
            ranges_by_uav,
            {uav_id: (6 - uav_id, 7 - uav_id) for uav_id in self.config.uav_ids},
        )


if __name__ == "__main__":
    unittest.main()
