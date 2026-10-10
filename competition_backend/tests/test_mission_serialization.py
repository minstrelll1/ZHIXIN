import json
import unittest
from dataclasses import asdict

from competition_backend.models import MissionPhase, MissionRuntime, UavPhase, UavRuntime


def legacy_mission_dict(mission):
    """The previous public serializer, used as a compatibility oracle."""
    result = asdict(mission)
    result["phase"] = mission.phase.value
    for name in ("uavs", "planned_uavs"):
        result[name] = {
            str(uid): {**asdict(runtime), "phase": runtime.phase.value}
            for uid, runtime in getattr(mission, name).items()
        }
    return result


def make_mission():
    planned = {}
    for uid in range(1, 7):
        task = {
            "type": "search",
            "waypoints_m": [[float(uid), float(index), 10.0] for index in range(12)],
            "transit_routes": {
                "return_paths": [
                    {"waypoint_index": index, "path": [[float(index), 1.0], [0.0, 0.0]]}
                    for index in range(12)
                ],
                "waypoints_by_uav": {
                    str(peer): [[float(peer), 2.0, 10.0]] for peer in range(1, 7)
                },
            },
            "options": {"enabled": True, "limit": None, "labels": ["search", "return"]},
        }
        planned[uid] = UavRuntime(
            uav_id=uid,
            target_altitude_m=10.0,
            task=task,
            phase=UavPhase.READY,
            return_reason="task_complete",
            last_error=None,
            assignment_checksum="checksum-%s" % uid,
            landing_point_m=[float(uid), 0.0, 0.0],
            landing_sequence=uid,
        )
    return MissionRuntime(
        mission_id="four-active-six-planned",
        subject="subject1",
        duration_seconds=600.0,
        phase=MissionPhase.PLANNED,
        planned_at=1000.0,
        flight_profile="competition",
        flight_altitude_plan="around10m",
        controller_mode="internal",
        started_at=1001.0,
        deadline_at=1601.0,
        takeoff_commanded_at=1000.5,
        confirmation_expires_at=1015.0,
        uavs={uid: planned[uid] for uid in (1, 2, 4, 5)},
        planned_uavs=planned,
        search_area={"polygon_m": [[0.0, 0.0], [50.0, 20.0]], "excluded": []},
        prepared_plan={"planned_uavs": {str(uid): {"task": item.task} for uid, item in planned.items()}},
        events=[{"kind": "planned", "details": {"uav_ids": [1, 2, 4, 5]}}],
    )


class MissionSerializationTest(unittest.TestCase):
    def test_values_and_json_field_order_match_legacy_for_every_phase(self):
        mission = make_mission()
        for phase in MissionPhase:
            for uav_phase in UavPhase:
                with self.subTest(phase=phase, uav_phase=uav_phase):
                    mission.phase = phase
                    mission.uavs[1].phase = uav_phase
                    actual = mission.to_dict()
                    expected = legacy_mission_dict(mission)
                    self.assertEqual(actual, expected)
                    self.assertEqual(json.dumps(actual), json.dumps(expected))
                    self.assertIs(type(actual["phase"]), str)
                    self.assertIs(type(actual["uavs"]["1"]["phase"]), str)
                    self.assertEqual(list(actual["uavs"]), ["1", "2", "4", "5"])

    def test_empty_mission_and_optional_fields_match_legacy(self):
        mission = MissionRuntime("empty", "subject1", 600.0, MissionPhase.IDLE, 1000.0)
        self.assertEqual(mission.to_dict(), legacy_mission_dict(mission))
        self.assertEqual(json.dumps(mission.to_dict()), json.dumps(legacy_mission_dict(mission)))

    def test_snapshot_mutation_isolated_from_runtime_and_other_snapshots(self):
        mission = make_mission()
        before = legacy_mission_dict(mission)
        snapshot = mission.to_dict()
        other_snapshot = mission.to_dict()
        snapshot["uavs"]["1"]["task"]["transit_routes"]["return_paths"][0]["path"][0][0] = -100.0
        self.assertEqual(snapshot["planned_uavs"]["1"], before["planned_uavs"]["1"])
        self.assertEqual(snapshot["prepared_plan"], before["prepared_plan"])
        snapshot["planned_uavs"]["2"]["landing_point_m"][0] = -200.0
        snapshot["prepared_plan"]["planned_uavs"]["3"]["task"]["waypoints_m"][0][0] = -300.0
        snapshot["search_area"]["polygon_m"][0][0] = -400.0
        snapshot["events"][0]["details"]["uav_ids"].append(6)
        self.assertEqual(legacy_mission_dict(mission), before)
        self.assertEqual(other_snapshot, before)

    def test_runtime_mutation_does_not_change_snapshot(self):
        mission = make_mission()
        snapshot = mission.to_dict()
        before = legacy_mission_dict(mission)
        mission.uavs[1].task["transit_routes"]["return_paths"][0]["path"][0][0] = -100.0
        mission.planned_uavs[3].landing_point_m.append(3.0)
        mission.prepared_plan["planned_uavs"]["6"]["task"]["options"]["labels"].append("changed")
        mission.search_area["excluded"].append([1.0, 2.0])
        mission.events[0]["details"]["uav_ids"].clear()
        self.assertEqual(snapshot, before)


if __name__ == "__main__":
    unittest.main()
