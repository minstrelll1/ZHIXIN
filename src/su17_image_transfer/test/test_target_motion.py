"""首次动静标记锁定、15秒低速复核及赛事副本可追溯性。"""
import datetime as dt
import json
import math
from pathlib import Path
import tempfile
import unittest

from competition_shared.target_motion import classify_motion, MIN_MOVING_SPEED_MPS
from su17_image_transfer.submission import update_subject1_submission


BASE = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)


def point(seconds, east=0):
    return dict(timestamp=(BASE + dt.timedelta(seconds=seconds)).isoformat(),
                coordinates=[113 + math.degrees(east / 6371008.8), 0.])


class TargetMotionTest(unittest.TestCase):
    def moving(self, points):
        return classify_motion(dict(is_moving=True, target_type="dsolider1", category_id=15), points)

    def test_first_flag_is_not_overridden_by_target_name_or_category(self):
        for fields in (dict(target_type="solider3"), dict(target_type="soldier3"),
                       dict(target_type="人员3"), dict(category_id=13),
                       dict(target_type="dsolider3"), dict(target_type="运动的人员3")):
            with self.subTest(fields=fields):
                moving, audit = classify_motion(dict(is_moving=True, **fields), [])
                self.assertTrue(moving)
                self.assertEqual("first_onboard_receipt", audit["rule"])
                self.assertFalse(audit["converted_to_static"])
                self.assertTrue(classify_motion(dict(is_moving=True, **fields),
                                               [point(0), point(20, 40)])[0])
                self.assertFalse(classify_motion(dict(is_moving=True, **fields),
                                                [point(0), point(15, .1)])[0])
                self.assertFalse(classify_motion(dict(is_moving=False, **fields),
                                                [point(0), point(20, 40)])[0])

    def test_exact_duration_and_speed_thresholds(self):
        for seconds, speed, expected in ((14.999, 0., True), (15., 0., False),
                                         (15., 1., False), (15., MIN_MOVING_SPEED_MPS, True),
                                         (30., 2., True)):
            with self.subTest(seconds=seconds, speed=speed):
                moving, _ = self.moving([point(0), point(seconds, seconds * speed)])
                self.assertEqual(expected, moving)

    def test_uses_segment_motion_instead_of_start_end_displacement(self):
        moving, _ = self.moving([point(0), point(15, 30), point(30, 0)])
        self.assertTrue(moving)
        moving, audit = self.moving([point(0), point(3, 9), point(6, 9), point(18, 9)])
        self.assertFalse(moving)
        self.assertEqual(15., audit["low_speed_duration_seconds"])
        self.assertTrue(self.moving([point(0), point(3, 9), point(6, 9), point(18, 9), point(21, 18)])[0])

    def test_sparse_observations_are_audited_as_observations_not_continuous_proof(self):
        moving, audit = self.moving([point(0), point(498, 1.4)])
        self.assertFalse(moving)
        self.assertTrue(audit["sparse_observations"])
        self.assertEqual(498., audit["max_observation_gap_seconds"])
        self.assertAlmostEqual(1.4 / 498, audit["low_speed_average_mps"], places=6)

    def test_duplicate_and_invalid_timestamps_do_not_accumulate_time(self):
        moving, audit = self.moving([point(0), point(0), point(0, 1),
                                     dict(timestamp="bad", coordinates=[113., 0.]),
                                     dict(timestamp=point(20)["timestamp"], coordinates=[math.nan, 0.])])
        self.assertTrue(moving)
        self.assertEqual(1, audit["valid_position_count"])

    def test_geographic_boundaries_and_out_of_order_points(self):
        points = [point(30, 0.5), point(0), point(15, 0.3)]
        self.assertFalse(self.moving(points)[0])
        self.assertTrue(self.moving([point(0), dict(timestamp=point(20)["timestamp"], coordinates=[181., 0.])])[0])

    def test_submission_conversion_keeps_last_entire_report_and_raw_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); mission = "subject1-low-speed"
            folder = root / "UAV2" / mission; folder.mkdir(parents=True)
            for i, seconds, success, score in ((1, 0, True, .99), (2, 15, False, .2)):
                metadata = dict(mission_id=mission, target_id="raw-13", category_id=15,
                    category="人", target_type="dsolider1", is_moving=True,
                    localization_time=dict(secs=int(BASE.timestamp()) + seconds, nsecs=0),
                    detection_received_at_unix_ns=1800000000000000000 + i,
                    target_latitude=0., target_longitude=point(seconds, i * .5)["coordinates"][0],
                    detection_count=i, tracking_success=success, confidence=score)
                (folder / (str(i) + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                if i == 1:
                    (folder / "1.jpg").write_bytes(b"old-image")
            original = {p.name: p.read_bytes() for p in folder.iterdir()}
            path = update_subject1_submission(root, mission, publisher_dedup=True)
            feature, = json.loads(path.read_text(encoding="utf-8"))["features"]
            props = feature["properties"]
            self.assertEqual("Point", feature["geometry"]["type"])
            self.assertEqual("固定", props["targetCategory"])
            self.assertEqual("人员1", props["targetModel"])
            self.assertEqual(.2, props["confidence"])
            self.assertEqual(point(15, 1)["coordinates"], feature["geometry"]["coordinates"])
            self.assertEqual("2026-10-10T08:00:15.000+08:00", props["timestamp"])
            self.assertNotIn("trackPoints", props)
            self.assertNotIn("imagePath", props)
            audit = json.loads(path.with_name("submission-format.json").read_text(encoding="utf-8"))
            row, = audit["motion_classification"]
            self.assertEqual("sustained_low_speed", row["rule"])
            self.assertFalse(row["reported_is_moving"])
            decisions = json.loads(path.with_name("dedup-decisions.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(decisions["motion_reclassifications"]))
            self.assertEqual(2, decisions["quality_ranking"][0]["detection_count"])
            self.assertFalse(decisions["quality_ranking"][0]["tracking_success"])
            self.assertEqual(original, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_recent_full_track_is_checked_before_earliest_forty_point_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); mission = "subject1-forty-tail"
            folder = root / "UAV1" / mission; folder.mkdir(parents=True)
            for i in range(46):
                p = point(i * 3, min(i, 40) * 6)
                metadata = dict(mission_id=mission, target_id="car", target_type="vehicle1",
                    is_moving=True, target_longitude=p["coordinates"][0], target_latitude=0.,
                    localization_time=dict(secs=int(BASE.timestamp()) + i * 3, nsecs=0))
                (folder / ("%02d.json" % i)).write_text(json.dumps(metadata), encoding="utf-8")
            path = update_subject1_submission(root, mission, publisher_dedup=True)
            feature, = json.loads(path.read_text(encoding="utf-8"))["features"]
            self.assertEqual("固定", feature["properties"]["targetCategory"])
            audit = json.loads(path.with_name("submission-format.json").read_text(encoding="utf-8"))
            self.assertEqual(46, audit["motion_classification"][0]["valid_position_count"])


if __name__ == "__main__":
    unittest.main()
