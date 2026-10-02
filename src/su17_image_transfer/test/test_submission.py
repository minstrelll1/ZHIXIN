import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from unittest.mock import patch

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

    def test_static_position_and_timestamp_come_from_same_latest_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV1" / "subject1-static"
            mission.mkdir(parents=True)
            for name, seconds, longitude in (("newer", 1_700_000_020, 113.702), ("older", 1_700_000_010, 113.701)):
                metadata = {
                    "mission_id": "subject1-static", "target_id": "fixed-1", "target_type": "工事",
                    "is_moving": False, "target_latitude": 33.86, "target_longitude": longitude,
                    "image_stamp": {"secs": seconds, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            document = json.loads(update_subject1_submission(root, "subject1-static").read_text(encoding="utf-8"))
            feature = document["features"][0]
            self.assertEqual([113.702, 33.86], feature["geometry"]["coordinates"])
            self.assertEqual("2023-11-14T22:13:40.000Z", feature["properties"]["timestamp"])

    def test_concurrent_rebuilds_leave_complete_json_and_no_partial_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV3" / "subject1-concurrent"
            mission.mkdir(parents=True)
            for index in range(12):
                metadata = {
                    "mission_id": "subject1-concurrent", "target_id": "target-%d" % index,
                    "target_type": "车辆", "target_latitude": 33.86,
                    "target_longitude": 113.70 + index / 10000,
                    "image_stamp": {"secs": 1_700_000_000 + index, "nsecs": 0},
                }
                (mission / ("image-%d.json" % index)).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / ("image-%d.jpg" % index)).write_bytes(b"jpeg-%d" % index)
            barrier = threading.Barrier(4)

            def rebuild():
                barrier.wait(timeout=5)
                return update_subject1_submission(root, "subject1-concurrent")

            with ThreadPoolExecutor(max_workers=4) as executor:
                targets = list(executor.map(lambda _: rebuild(), range(4)))
            self.assertEqual(1, len(set(targets)))
            document = json.loads(targets[0].read_text(encoding="utf-8"))
            self.assertEqual(12, len(document["features"]))
            image_dir = targets[0].parent / "images"
            self.assertEqual(12, len(list(image_dir.glob("*.jpg"))))
            self.assertEqual([], list(image_dir.glob("*.part")))

    def test_latest_category_correction_and_static_to_moving_use_one_global_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV2" / "subject1-correction"
            mission.mkdir(parents=True)
            reports = (
                ("a", 1_700_000_010, False, "人员", "人员1", 113.700),
                ("z", 1_700_000_020, True, "车辆", "车辆2", 113.701),
            )
            for name, seconds, moving, target_type, model, longitude in reports:
                metadata = {
                    "mission_id": "subject1-correction", "global_id": "one-global-id",
                    "target_type": target_type, "target_model": model, "is_moving": moving,
                    "target_latitude": 33.86, "target_longitude": longitude,
                    "image_stamp": {"secs": seconds, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            document = json.loads(update_subject1_submission(root, "subject1-correction").read_text(encoding="utf-8"))
            self.assertEqual(1, len(document["features"]))
            feature = document["features"][0]
            self.assertEqual("one-global-id", feature["id"])
            self.assertEqual("LineString", feature["geometry"]["type"])
            self.assertEqual("移动", feature["properties"]["targetCategory"])
            self.assertEqual("车辆", feature["properties"]["targetType"])
            self.assertEqual("车辆2", feature["properties"]["targetModel"])
            self.assertEqual(2, len(feature["properties"]["trackPoints"]))

    def test_same_file_name_from_two_uavs_keeps_both_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for uav_id in (1, 2):
                mission = root / ("UAV%d" % uav_id) / "subject1-peers"
                mission.mkdir(parents=True)
                metadata = {
                    "mission_id": "subject1-peers", "global_id": "same-target",
                    "target_type": "车辆", "is_moving": True,
                    "target_latitude": 33.86, "target_longitude": 113.70 + uav_id / 1000,
                    "image_stamp": {"secs": 1_700_000_000 + uav_id, "nsecs": 0},
                }
                (mission / "capture.json").write_text(json.dumps(metadata), encoding="utf-8")
                (mission / "capture.jpg").write_bytes(("jpeg-%d" % uav_id).encode())
            target = update_subject1_submission(root, "subject1-peers")
            images = list((target.parent / "images").glob("*.jpg"))
            self.assertEqual(2, len(images))
            self.assertEqual({b"jpeg-1", b"jpeg-2"}, {path.read_bytes() for path in images})
            document = json.loads(target.read_text(encoding="utf-8"))
            chosen = target.parent / document["features"][0]["properties"]["imagePath"]
            self.assertEqual(b"jpeg-2", chosen.read_bytes())

    def test_slow_submission_rebuild_does_not_block_new_image_save(self):
        package_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "receiver_for_submission_test", package_root / "ground" / "ground_image_receiver.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            receiver = module.GroundImageReceiver("127.0.0.1", 0, Path(directory), "", status_interval=0)
            first_build_started = threading.Event()
            release_build = threading.Event()

            def slow_rebuild(*_args):
                first_build_started.set()
                release_build.wait(timeout=5)
                result = Path(directory) / "finished.json"
                result.write_text('{"features":[],"metadata":{"indoorTargets":[]}}', encoding="utf-8")
                return result

            metadata = {"uav_id": 1, "mission_id": "subject1-async", "request_id": "first"}
            with patch.object(module, "update_subject1_submission", side_effect=slow_rebuild):
                try:
                    receiver._save_image(metadata, b"jpeg-1")
                    self.assertTrue(first_build_started.wait(timeout=2))
                    second_saved = threading.Event()

                    def save_second():
                        receiver._save_image(dict(metadata, request_id="second"), b"jpeg-2")
                        second_saved.set()

                    worker = threading.Thread(target=save_second)
                    worker.start()
                    self.assertTrue(second_saved.wait(timeout=1), "图片保存被结果整理阻塞")
                    worker.join(timeout=1)
                    self.assertTrue((Path(directory) / "UAV1" / "subject1-async" / "second.json").exists())
                finally:
                    release_build.set()
                    receiver.stop()

    def test_failed_background_rebuild_retries_without_another_image(self):
        package_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "receiver_for_retry_test", package_root / "ground" / "ground_image_receiver.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            receiver = module.GroundImageReceiver("127.0.0.1", 0, Path(directory), "", status_interval=0)
            attempts = []
            recovered = threading.Event()

            def flaky_rebuild(*_args):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("临时文件被占用")
                result = Path(directory) / "finished.json"
                result.write_text('{"features":[],"metadata":{"indoorTargets":[]}}', encoding="utf-8")
                recovered.set()
                return result

            with patch.object(module, "update_subject1_submission", side_effect=flaky_rebuild):
                try:
                    receiver._save_image(
                        {"uav_id": 1, "mission_id": "subject1-retry", "request_id": "only"}, b"jpeg"
                    )
                    self.assertTrue(recovered.wait(timeout=4), "无后续图片时应自动重试整理")
                    self.assertEqual(2, len(attempts))
                finally:
                    receiver.stop()


if __name__ == "__main__":
    unittest.main()
