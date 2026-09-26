import json
from pathlib import Path
import tempfile
import unittest

from su17_image_transfer.submission import update_subject1_submission


class SubmissionTest(unittest.TestCase):
    def test_outdoor_static_and_moving_are_exported_in_template_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV3" / "subject1-run"
            mission.mkdir(parents=True)
            for index, moving in enumerate((False, True, True)):
                name = "target-%s-%d" % ("move" if moving else "fixed", index)
                metadata = {
                    "mission_id": "subject1-run", "target_id": "move" if moving else "fixed",
                    "target_type": "车辆", "target_model": "车辆2", "is_moving": moving,
                    "confidence": 0.9, "target_latitude": 33.86, "target_longitude": 113.70,
                    "target_altitude": 100.0, "image_stamp": {"secs": 1_700_000_000 + index, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            target = update_subject1_submission(root, "subject1-run", "测试队")
            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("FeatureCollection", document["type"])
            self.assertEqual("测试队", document["name"])
            self.assertEqual(2, len(document["features"]))
            moving = next(item for item in document["features"] if item["id"] == "move")
            self.assertEqual("LineString", moving["geometry"]["type"])
            self.assertEqual(2, len(moving["properties"]["trackPoints"]))
            self.assertTrue(any((target.parent / "images").glob("*.jpg")))

    def test_indoor_xyz_is_retained_but_not_written_as_wgs84(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV1" / "subject1-indoor"
            mission.mkdir(parents=True)
            metadata = {
                "mission_id": "subject1-indoor", "target_id": "lab-1", "target_type": "人员",
                "indoor_position": True, "east_m": 1.2, "north_m": 2.3, "up_m": 0.4,
                "image_stamp": {"secs": 1_700_000_000, "nsecs": 0},
            }
            (mission / "lab.json").write_text(json.dumps(metadata), encoding="utf-8")
            (mission / "lab.jpg").write_bytes(b"jpeg")
            document = json.loads(update_subject1_submission(root, "subject1-indoor").read_text(encoding="utf-8"))
            self.assertEqual([], document["features"])
            self.assertEqual(1.2, document["metadata"]["indoorTargets"][0]["localPosition"][0]["east_m"])
            self.assertEqual("室内 XYZ 或无效 WGS84 坐标不能写入 EPSG:4326 几何", document["metadata"]["indoorTargets"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
