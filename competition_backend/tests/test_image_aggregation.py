import hashlib
import tempfile
import unittest
from pathlib import Path

from competition_backend.image_aggregation import (
    local_image_manifest,
    resolve_image_file,
)


class ImageAggregationTest(unittest.TestCase):
    def test_manifest_is_scoped_to_one_uav_and_contains_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "UAV2" / "mission-a" / "target.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"image-data")
            other = root / "UAV3" / "mission-a" / "other.jpg"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"must-not-leak")

            result = local_image_manifest(root, 2)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["relative_path"], "UAV2/mission-a/target.jpg")
            self.assertEqual(
                result[0]["sha256"], hashlib.sha256(b"image-data").hexdigest()
            )
            self.assertEqual(
                resolve_image_file(root, 2, result[0]["relative_path"]),
                path.resolve(),
            )
            with self.assertRaises(ValueError):
                resolve_image_file(root, 2, "UAV3/mission-a/other.jpg")


if __name__ == "__main__":
    unittest.main()
