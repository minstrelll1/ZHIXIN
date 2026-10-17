import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from competition_backend.adapter import RecordingAdapter
from competition_backend.fixed_targets import FixedTargetStore, FixedTargetRecorder
from competition_backend.models import MissionPhase
from competition_backend.orchestrator import CompetitionOrchestrator
from competition_shared.fixed_targets import empty_fixed_targets, fixed_targets_from_submission, validate_fixed_targets
from competition_shared.subject1_dedup import consolidate
from competition_shared.submission_format import format_submission
from test_orchestrator import make_config


def feature(identifier, model="工事1", longitude=113.7, category="固定", count=1):
    return dict(type="Feature", id=identifier,
                geometry=dict(type="Point", coordinates=[longitude, 33.86]),
                properties=dict(targetCategory=category, targetType=("工事" if model.startswith("工事") else
                                "车辆" if model.startswith("车辆") else "人员"), targetModel=model,
                                timestamp="2026-10-17T10:00:00+08:00", confidence=.8),
                _quality=dict(detection_count=count, tracking_success=True), _source_uav="UAV1")


def targets(mission_id="subject1-source", longitude=113.7):
    return fixed_targets_from_submission(dict(type="FeatureCollection", features=[feature("target-001", longitude=longitude)]), mission_id)


class FixedTargetsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = FixedTargetStore(self.root / "config/fixed_targets.json")
        self.adapter = RecordingAdapter()
        self.backend = CompetitionOrchestrator(make_config(), self.adapter)
        self.backend.plan("subject1")
        self.mission = self.backend._mission
        self.publisher = True
        self.recorder = FixedTargetRecorder(self.store, self.root, self.backend.completed_flight_snapshot,
                                            lambda: self.publisher, audit=Mock())

    def write_results(self, features):
        document, decisions = consolidate(dict(type="FeatureCollection", name="test", features=features))
        document, audit = format_submission(document)
        audit["deduplication"] = decisions
        folder = self.root / "subject1_submissions" / self.mission.mission_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "target-submission.json").write_text(json.dumps(document), encoding="utf-8")
        (folder / "submission-format.json").write_text(json.dumps(audit), encoding="utf-8")
        return folder, document

    def complete(self):
        self.mission.takeoff_commanded_at = 100
        self.mission.phase = MissionPhase.COMPLETED

    def test_completed_publisher_saves_final_fixed_order_and_overwrites_previous_flight(self):
        self.store.replace(targets())
        _, document = self.write_results([feature("low", longitude=113.71), feature("high", count=10),
                                          feature("duplicate", longitude=113.700001, count=3)])
        self.complete()
        self.assertTrue(self.recorder.tick())
        saved = self.store.read()
        self.assertEqual(saved["source_mission_id"], self.mission.mission_id)
        self.assertEqual([t["target_id"] for t in saved["targets"]], [f["id"] for f in document["features"]])
        self.assertEqual(len(saved["targets"]), 2)
        self.assertEqual(saved["targets"][0]["longitude"], 113.7)
        self.assertEqual(saved["targets"][0]["category_id"], 7)
        self.assertFalse(self.recorder.tick())
        self.assertEqual([p.name for p in self.store.path.parent.iterdir()], ["fixed_targets.json"])

    def test_only_finished_subject_one_flight_on_publisher_can_write(self):
        self.store.replace(targets())
        self.write_results([feature("new", longitude=113.72)])
        for phase in (MissionPhase.PLANNED, MissionPhase.PREFLIGHT_READY, MissionPhase.TAKING_OFF,
                      MissionPhase.RUNNING, MissionPhase.RETURNING, MissionPhase.COMPLETED):
            self.mission.phase = phase
            self.assertFalse(self.recorder.tick())  # No actual takeoff, even if completion is reported.
        self.complete()
        self.publisher = False
        self.assertFalse(self.recorder.tick())
        self.publisher = True
        self.mission.subject = "subject2"
        self.assertFalse(self.recorder.tick())
        self.assertEqual(self.store.read(), targets())
        self.mission.subject = "subject1"
        self.mission.phase = MissionPhase.RUNNING
        self.assertFalse(self.recorder.tick())
        self.mission.phase = MissionPhase.COMPLETED
        self.assertTrue(self.recorder.tick())

    def test_late_results_retry_and_empty_final_result_clears_previous_targets(self):
        self.store.replace(targets())
        self.complete()
        self.assertFalse(self.recorder.tick())
        folder, _ = self.write_results([feature("new")])
        audit_path = folder / "submission-format.json"
        audit = json.loads(audit_path.read_text())
        audit["document_sha256"] = "incomplete-generation"
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        self.assertFalse(self.recorder.tick())
        self.assertEqual(self.store.read(), targets())
        self.write_results([feature("new", longitude=113.72)])
        self.assertTrue(self.recorder.tick())
        self.write_results([])
        self.assertTrue(self.recorder.tick())
        self.assertEqual(self.store.read()["targets"], [])
        self.assertEqual(self.store.read()["source_mission_id"], self.mission.mission_id)

    def test_all_fixed_types_keep_submission_order_and_exclude_moving(self):
        moving_vehicle = feature("moving-vehicle", model="车辆5", category="移动")
        moving_vehicle["geometry"] = dict(type="LineString", coordinates=[[113.70, 33.86], [113.71, 33.87]])
        moving_person = feature("moving-person", model="人员1", category="移动")
        moving_person["geometry"] = dict(type="LineString", coordinates=[[113.71, 33.86], [113.72, 33.87]])
        other = feature("other", model="未知型号")
        other["properties"]["targetType"] = "其他"
        document = dict(type="FeatureCollection", features=[moving_vehicle,
                        feature("building", model="工事2", longitude=113.7123),
                        feature("vehicle", model="车辆1"), feature("person", model="人员1"),
                        moving_person, other])
        data = fixed_targets_from_submission(document, "subject1-a")
        self.assertEqual([entry["target_id"] for entry in data["targets"]],
                         ["building", "vehicle", "person", "other"])
        self.assertEqual([entry["category_id"] for entry in data["targets"]], [8, 0, 11, None])
        self.assertTrue(all(entry["target_category"] == "固定" for entry in data["targets"]))
        self.assertEqual(data["targets"][0]["longitude"], 113.7123)
        self.assertEqual(data["targets"][0]["latitude"], 33.86)
        self.store.replace(data)
        self.assertEqual(self.store.read(), data)

    def test_finished_flight_records_only_fixed_from_final_submission(self):
        moving = feature("moving", model="车辆5", category="移动")
        moving["geometry"] = dict(type="LineString", coordinates=[[113.70, 33.86], [113.71, 33.87]])
        moving["properties"]["trackPoints"] = [
            dict(coordinates=[113.70, 33.86], timestamp="2026-10-17T10:00:00+08:00"),
            dict(coordinates=[113.71, 33.87], timestamp="2026-10-17T10:00:05+08:00"),
        ]
        _, document = self.write_results([moving, feature("building", model="工事2")])
        self.complete()
        self.assertTrue(self.recorder.tick())
        saved = self.store.read()
        self.assertEqual(len(saved["targets"]), 1)
        self.assertEqual([item["target_id"] for item in saved["targets"]],
                         [item["id"] for item in document["features"]
                          if item["properties"]["targetCategory"] == "固定"])
        self.assertEqual(saved["targets"][0]["category_id"], 8)

    def test_manual_edits_survive_late_results_until_next_completed_flight(self):
        self.complete()
        self.write_results([feature("initial")])
        self.assertTrue(self.recorder.tick())
        edited = self.store.read()
        edited["targets"][0]["latitude"] = 33.87
        self.store.path.write_text(json.dumps(edited), encoding="utf-8")
        self.write_results([feature("late", longitude=113.72)])
        self.assertFalse(self.recorder.tick())
        self.assertEqual(self.store.read(), edited)
        self.assertFalse(self.recorder.tick())
        self.backend.plan("subject1")
        self.mission = self.backend._mission
        self.complete()
        self.write_results([feature("next", longitude=113.74)])
        self.assertTrue(self.recorder.tick())
        self.assertEqual(self.store.read()["targets"][0]["longitude"], 113.74)

    def test_known_models_validate_category_ids_and_legacy_buildings_remain_readable(self):
        for index in range(1, 5):
            data = fixed_targets_from_submission(dict(type="FeatureCollection", features=[feature("one", model="工事%d" % index)]), "subject1-a")
            self.assertEqual(data["targets"][0]["category_id"], index + 6)
        bad = targets()
        bad["targets"][0]["target_model"] = "车辆1"
        with self.assertRaises(ValueError):
            self.store.replace(bad)
        old = targets()
        old["targets"][0].pop("target_category")
        self.store.replace(old)
        self.assertEqual(self.store.read(), old)
        moving = targets()
        moving["targets"][0]["target_category"] = "移动"
        with self.assertRaises(ValueError):
            self.store.replace(moving)

    def test_manual_edit_during_result_processing_is_not_overwritten(self):
        self.store.replace(targets())
        self.complete()
        self.write_results([feature("new", longitude=113.72)])
        edited = targets(longitude=113.75)
        def edit_while_processing(document, mission_id):
            self.store.path.write_text(json.dumps(edited), encoding="utf-8")
            return fixed_targets_from_submission(document, mission_id)
        with patch("competition_backend.fixed_targets.fixed_targets_from_submission", side_effect=edit_while_processing):
            self.assertFalse(self.recorder.tick())
        self.assertFalse(self.recorder.tick())
        self.assertEqual(self.store.read(), edited)

    def test_invalid_or_failed_write_preserves_last_file(self):
        self.store.replace(targets())
        before = self.store.path.read_bytes()
        bad = targets()
        bad["targets"][0]["latitude"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.replace(bad)
        with patch("competition_backend.fixed_targets.os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.store.replace(targets(longitude=113.72))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(list(self.store.path.parent.iterdir())), 1)
        with self.assertRaises(ValueError):
            validate_fixed_targets(dict(empty_fixed_targets(), source_mission_id="../escape"))


if __name__ == "__main__":
    unittest.main()
