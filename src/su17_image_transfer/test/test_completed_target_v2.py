import json
import unittest
from types import SimpleNamespace as NS
import test_timestamped_detection as base
frame = base.frame
from su17_image_transfer.frame_cache import completed_target_metadata, clipped_rectangle


def target(**kwargs):
    stamp = frame().header.stamp
    data = dict(header=NS(stamp=stamp), global_id="42", detection_count=2**63 + 7,
                tracking_success=True, target_type="solider3", timestamp=stamp, image_stamp=stamp,
                localization_time=stamp, cx=.5, cy=.5, w=.25, h=.25, score=.7,
                category_id=13, speed_mps=0., category="人", is_moving=False,
                indoor_position=False, latitude_deg=34., longitude_deg=113., altitude_gps_m=55.)
    data.update(kwargs)
    return NS(**data)


class CompletedTargetV2Test(unittest.TestCase):
    setUp = base.SenderTest.setUp
    process_next = base.SenderTest.process_next

    def test_normalized_box_scales_on_actual_image_and_new_fields_survive(self):
        self.sender.input_mode = "completed_target_array"
        self.sender._camera_cache_callback(frame())
        self.sender._detection_callback(NS(targets=[target()]))
        jpeg, metadata = self.process_next()
        self.assertTrue(jpeg)
        self.assertEqual(metadata["rendered_bbox"], dict(x_min=45, y_min=30, x_max=74, y_max=49))
        self.assertEqual(metadata["bbox"]["units"], "normalized")
        self.assertEqual(metadata["detection_count"], 2**63 + 7)
        self.assertTrue(metadata["tracking_success"])
        json.dumps(metadata, allow_nan=False)

    def test_missing_box_does_not_lose_json_or_image(self):
        self.sender.input_mode = "completed_target_array"
        self.sender._camera_cache_callback(frame())
        self.sender._detection_callback(NS(targets=[target(w=float("nan"))]))
        self.assertEqual(self.sender.result_jobs.qsize(), 1)
        jpeg, metadata = self.process_next()
        self.assertTrue(jpeg)
        self.assertIsNone(metadata["bbox"])
        self.assertNotIn("rendered_bbox", metadata)
        json.dumps(metadata, allow_nan=False)

    def test_edge_box_is_clipped_in_pixels(self):
        _, m = completed_target_metadata(target(cx=.99, cy=.01))
        x1,y1,x2,y2 = clipped_rectangle(m["bbox"],1280,720)
        self.assertEqual((x2,y1),(1279,0))
        self.assertGreater(x1,1000)
        self.assertGreater(y2,80)

    def test_image_matching_keeps_image_time_and_submission_uses_localization(self):
        from su17_image_transfer.submission import _iso_from_metadata
        image_stamp = NS(secs=1800000000, nsecs=123456789)
        localized = NS(secs=1800000003, nsecs=456789123)
        for moving in (False, True):
            key, metadata = completed_target_metadata(target(timestamp=image_stamp,
                image_stamp=image_stamp, localization_time=localized, is_moving=moving))
            self.assertEqual(key, 1800000000123456789)
            self.assertEqual(metadata['image_stamp'],dict(secs=1800000000,nsecs=123456789))
            self.assertEqual(metadata['localization_time'],dict(secs=1800000003,nsecs=456789123))
            self.assertEqual(_iso_from_metadata(metadata),'2027-01-15T16:00:03.456+08:00')
