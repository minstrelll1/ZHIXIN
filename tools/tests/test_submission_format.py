"""赛事提交副本格式转换测试；无需 ROS 或联网。"""
import copy
import unittest

from competition_shared.submission_format import format_submission, submission_content_hash


class SubmissionFormatTests(unittest.TestCase):
    def feature(self, source_id="raw-uav4-8", model="运动的人员4"):
        return {
            "type": "Feature", "id": source_id,
            "geometry": {"type": "LineString", "coordinates": [[113.91, 34.138], [113.911, 34.139]]},
            "properties": {
                "targetCategory": "移动", "targetType": "人员", "targetModel": model,
                "timestamp": "2026-10-09T11:22:33.123456+08:00",
                "imagePath": "./images/uav4/raw-uav4-8.jpg",
                "trackStartTime": "2026-10-09T11:22:30+08:00",
                "trackEndTime": "2026-10-09T11:22:33+08:00",
                "trackPoints": [{"coordinates": [113.91, 34.138], "timestamp": "2026-10-09T11:22:30+08:00"}],
            },
        }

    def test_sequential_ids_preserve_duplicate_non_contiguous_source_ids(self):
        source = {"features": [self.feature("81"), self.feature("3"), self.feature("81")]}
        result, audit = format_submission(source)
        self.assertEqual([f["id"] for f in result["features"]], ["target-001", "target-002", "target-003"])
        self.assertEqual(audit["id_mapping"], [
            {"source_id": source_id, "submission_id": "target-%03d" % i}
            for i, source_id in enumerate(["81", "3", "81"], 1)
        ])
        self.assertNotIn("source_id", result["features"][0])

    def test_person_models_only_exact_match_and_category_stays_moving(self):
        models = ["运动的人员%d" % i for i in range(1, 5)] + ["运动的人员5", "运动的人员1 ", "人员4", "车辆1"]
        result, _ = format_submission({"features": [self.feature(model=m) for m in models]})
        self.assertEqual([f["properties"]["targetModel"] for f in result["features"]],
                         ["人员1", "人员2", "人员3", "人员4"] + models[4:])
        self.assertTrue(all(f["properties"]["targetCategory"] == "移动" for f in result["features"]))

    def test_source_unchanged_coordinates_time_and_image_unchanged(self):
        source = {"features": [self.feature()], "metadata": {"createdAt": "2026-10-09T12:00:00+08:00"}}
        original = copy.deepcopy(source)
        result, audit = format_submission(source)
        self.assertEqual(source, original)
        self.assertEqual(result["features"][0]["geometry"], original["features"][0]["geometry"])
        for key in ("timestamp", "imagePath", "trackStartTime", "trackEndTime", "trackPoints"):
            self.assertEqual(result["features"][0]["properties"][key], original["features"][0]["properties"][key])
        result["features"][0]["properties"]["trackPoints"][0]["timestamp"] = "changed"
        self.assertEqual(source, original)
        self.assertEqual(audit["excluded_targets"], [])

    def test_indoor_omissions_preserved_in_separate_audit(self):
        omitted = [{"id": "uav4-x", "reason": "室内 XYZ 或无效 WGS84 坐标不能写入 EPSG:4326 几何"},
                   {"id": "uav5-y", "reason": "动态目标轨迹少于两个有效 WGS84 点"}]
        source = {"features": [], "metadata": {"version": "1.0", "indoorTargets": omitted, "custom": {"v": 1}}}
        result, audit = format_submission(source)
        self.assertNotIn("indoorTargets", result["metadata"])
        self.assertEqual(result["metadata"], {"version": "1.0", "custom": {"v": 1}})
        self.assertEqual(audit["excluded_targets"], omitted)
        self.assertEqual(audit["id_mapping"], [])
        audit["excluded_targets"][0]["reason"] = "changed"
        self.assertNotEqual(omitted[0]["reason"], "changed")

    def test_formatted_document_is_idempotent_and_absent_lists_are_empty(self):
        source = {"features": [self.feature()], "metadata": {"indoorTargets": []}}
        first, _ = format_submission(source)
        second, _ = format_submission(first)
        self.assertEqual(first, second)
        empty, audit = format_submission({"features": []})
        self.assertEqual(audit["id_mapping"], [])
        self.assertEqual(audit["excluded_targets"], [])
        self.assertEqual(audit["document_sha256"], submission_content_hash(empty))

    def test_content_hash_ignores_only_metadata_created_at_and_object_key_order(self):
        source = {"features": [self.feature()], "metadata": {"createdAt": "2020-01-01T00:00:00Z", "version": "1.0"}}
        original = copy.deepcopy(source)
        changed = copy.deepcopy(source)
        changed["metadata"]["createdAt"] = "2026-10-09T11:22:33+08:00"
        reordered = {key: changed[key] for key in reversed(list(changed))}
        self.assertEqual(submission_content_hash(source), submission_content_hash(reordered))
        del changed["metadata"]["createdAt"]
        self.assertEqual(submission_content_hash(source), submission_content_hash(changed))
        self.assertEqual(source, original)

    def test_content_hash_changes_with_actual_content_or_feature_order(self):
        source = {"features": [self.feature("first"), self.feature("second")], "metadata": {"version": "1.0"}}
        mutations = [
            lambda d: d["features"].reverse(),
            lambda d: d["features"][0]["properties"].update(timestamp="2026-10-09T11:22:34+08:00"),
            lambda d: d["features"][0]["geometry"]["coordinates"].reverse(),
            lambda d: d["metadata"].update(version="2.0"),
            lambda d: d["features"][0]["properties"].update(imagePath="./images/another.jpg"),
        ]
        for mutation in mutations:
            changed = copy.deepcopy(source)
            mutation(changed)
            self.assertNotEqual(submission_content_hash(source), submission_content_hash(changed))

    def test_format_audit_hash_binds_final_not_original_document(self):
        source = {"features": [self.feature()], "metadata": {"indoorTargets": [{"id": "skip", "reason": "无效定位"}]}}
        result, audit = format_submission(source)
        self.assertEqual(audit["document_sha256"], submission_content_hash(result))
        self.assertNotEqual(audit["document_sha256"], submission_content_hash(source))

    def test_ids_remain_unique_past_three_digits(self):
        result, _ = format_submission({"features": [{"id": "same"} for _ in range(1001)]})
        self.assertEqual(result["features"][999]["id"], "target-1000")
        self.assertEqual(len({f["id"] for f in result["features"]}), 1001)


if __name__ == "__main__":
    unittest.main()
