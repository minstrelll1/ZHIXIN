import json
import tempfile
import unittest
from pathlib import Path

from competition_shared.target_quality import category_fields, dedup_category
from su17_image_transfer.submission import update_subject1_submission


class FirstKindTest(unittest.TestCase):
    def add(self, root, name, *, uid=1, target="same-id", moving=False, receipt=1,
            stamp=1700000000, longitude=113., image=True, **fields):
        folder = root / ("UAV%d" % uid) / "subject1-first-kind"
        folder.mkdir(parents=True, exist_ok=True)
        metadata = dict(mission_id="subject1-first-kind", target_id=target,
                        is_moving=moving, category_id=0, category="车", target_type="vehicle1",
                        target_latitude=34., target_longitude=longitude,
                        detection_received_at_unix_ns=1700001000000000000 + receipt,
                        localization_time=dict(secs=stamp, nsecs=0), confidence=.8)
        metadata.update(fields)
        (folder / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
        if image:
            (folder / (name + ".jpg")).write_bytes(name.encode())

    def build(self, root, publisher=True):
        path = update_subject1_submission(root, "subject1-first-kind", publisher_dedup=publisher)
        return (path, json.loads(path.read_text(encoding="utf-8")),
                json.loads(path.with_name("submission-format.json").read_text(encoding="utf-8")))

    def test_static_first_uses_last_complete_report_even_if_lower_quality_or_old_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.add(root, "z-first", moving=False, receipt=1, stamp=1700000020,
                     tracking_success=True, detection_count=100)
            self.add(root, "a-last", moving=True, receipt=2, stamp=1700000010,
                     longitude=113.001, image=False, confidence=.1, tracking_success=False,
                     detection_count=2, category_id=11, category="人", target_type="solider1")
            path, document, audit = self.build(root)
            feature, = document["features"]
            self.assertEqual("固定", feature["properties"]["targetCategory"])
            self.assertEqual("Point", feature["geometry"]["type"])
            self.assertEqual([113.001, 34.], feature["geometry"]["coordinates"])
            self.assertEqual("人员1", feature["properties"]["targetModel"])
            self.assertEqual(.1, feature["properties"]["confidence"])
            self.assertNotIn("imagePath", feature["properties"])
            self.assertFalse(audit["motion_classification"][0]["locked_is_moving"])
            self.assertEqual(1, audit["motion_classification"][0]["contradictory_report_count"])
            decisions = json.loads(path.with_name("dedup-decisions.json").read_text(encoding="utf-8"))
            self.assertFalse(decisions["quality_ranking"][0]["tracking_success"])
            self.assertEqual(2, decisions["quality_ranking"][0]["detection_count"])
            # 同一原始报告的延迟重建不改变首次分类和覆盖结果。
            _, repeated, _ = self.build(root)
            self.assertEqual(document["features"], repeated["features"])

    def test_moving_first_keeps_later_static_points_and_receipt_order_wins_duplicate_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.add(root, "z-first", moving=True, receipt=1, stamp=1700000000)
            self.add(root, "b-next", moving=False, receipt=2, stamp=1700000003, longitude=113.001)
            self.add(root, "a-last", moving=False, receipt=3, stamp=1700000003, longitude=113.002)
            self.add(root, "c-end", moving=False, receipt=4, stamp=1700000006, longitude=113.003)
            _, document, audit = self.build(root)
            feature, = document["features"]
            self.assertEqual("移动", feature["properties"]["targetCategory"])
            self.assertEqual("LineString", feature["geometry"]["type"])
            self.assertEqual([[113., 34.], [113.002, 34.], [113.003, 34.]], feature["geometry"]["coordinates"])
            self.assertTrue(audit["motion_classification"][0]["locked_is_moving"])
            self.assertEqual(3, audit["motion_classification"][0]["contradictory_report_count"])

    def test_delayed_earliest_report_corrects_first_kind_without_changing_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.add(root, "later-1", moving=True, receipt=2, stamp=1700000003, longitude=113.001)
            self.add(root, "later-2", moving=True, receipt=3, stamp=1700000006, longitude=113.002)
            _, before, _ = self.build(root)
            self.assertEqual("移动", before["features"][0]["properties"]["targetCategory"])
            self.add(root, "z-offline-first", moving=False, receipt=1, stamp=1700000000)
            _, after, _ = self.build(root)
            feature, = after["features"]
            self.assertEqual("固定", feature["properties"]["targetCategory"])
            self.assertEqual([113.002, 34.], feature["geometry"]["coordinates"])

    def test_first_kind_uses_corrected_receipt_order_after_onboard_clock_rollback(self):
        from competition_shared.localization_clock import SourceClockTracker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot, ns, ground = "11111111-1111-4111-8111-111111111111", 1000000000, 1800000000
            for index, rtc in enumerate((ground, 1400000)):
                tracker = SourceClockTracker(boot)
                tracker.observe(90 * ns, (rtc + 90) * ns, (rtc + 90) * ns)
                tracker.observe((105 + index) * ns, (rtc + 105 + index) * ns, (rtc + 105 + index) * ns)
                loc = dict(secs=rtc + 100 + index, nsecs=0)
                self.add(root, "z-first" if index == 0 else "a-last", moving=bool(index),
                         receipt=1 + index, longitude=113. + index * .001,
                         localization_time=loc, localization_clock=tracker.context(loc),
                         detection_received_at_unix_ns=(rtc + 105 + index) * ns)
            reference = dict(boot_id=boot, uav_id=1, ground_epoch=ground, remote_monotonic=110.,
                             uncertainty_sec=.01, sampled_at=ground, source="publisher_windows")
            (root / "time_references.json").write_text(
                json.dumps(dict(schema_version=1, references={boot: reference})), encoding="utf-8")
            _, document, audit = self.build(root)
            feature, = document["features"]
            self.assertEqual("固定", feature["properties"]["targetCategory"])
            self.assertEqual([113.001, 34.], feature["geometry"]["coordinates"])
            self.assertEqual(2, audit["target_time"]["corrected_count"])
            self.assertEqual(1, audit["deduplication"]["raw_count"])
            self.assertEqual(0, audit["deduplication"]["backfilled_count"])

    def test_same_id_different_uavs_have_independent_first_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.add(root, "first", uid=1, moving=False, receipt=1)
            self.add(root, "first", uid=2, moving=True, receipt=2, longitude=114.)
            self.add(root, "next", uid=2, moving=False, receipt=3, stamp=1700000003, longitude=114.001)
            for publisher in (False, True):
                _, document, audit = self.build(root, publisher)
                self.assertEqual(["固定", "移动"], sorted(f["properties"]["targetCategory"] for f in document["features"]))
                self.assertEqual(2, len(audit["motion_classification"]))

    def test_explicit_category_selects_dedup_group_without_rewriting_formal_model(self):
        for category, expected, cid in (("人", "人员", 11), ("车", "车辆", 0), ("建筑物", "工事", 7)):
            with self.subTest(category=category):
                self.assertEqual(expected, dedup_category(dict(category=category, category_id=cid)))
                self.assertEqual(expected, dedup_category(dict(category=category, category_id=0)))
        self.assertEqual("人员", dedup_category(dict(target_type="solider4")))
        self.assertEqual(("车辆", "车辆1"), category_fields(dict(category="人", category_id=0)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.add(root, "conflict", category="人", category_id=0)
            path, document, audit = self.build(root)
            self.assertTrue(audit["category_records"][0]["category_conflict"])
            raw = json.loads(path.with_name("raw-targets.json").read_text(encoding="utf-8"))
            self.assertEqual("人员", raw["features"][0]["_dedup_category"])
            self.assertEqual("车辆1", document["features"][0]["properties"]["targetModel"])
            self.assertNotIn("_dedup_category", document["features"][0])


if __name__ == "__main__":
    unittest.main()
