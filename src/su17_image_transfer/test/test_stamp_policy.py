import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from su17_image_transfer.stamp_policy import StampPolicy

class StampPolicyTest(unittest.TestCase):
    def test_keeps_exact_original_stamp(self):
        stamp=1789000000123456789
        self.assertEqual(StampPolicy().resolve(stamp,stamp+999),(stamp,'source'))
    def test_missing_source_gets_unique_shared_stamp(self):
        policy=StampPolicy()
        self.assertEqual(policy.resolve(0,123),(123,'receive'))
        self.assertEqual(policy.resolve(0,123),(124,'receive'))
        self.assertEqual(policy.resolve(0,100),(125,'receive'))
    def test_strict_mode_and_unavailable_clock(self):
        with self.assertRaises(ValueError):StampPolicy('require_source').resolve(0,123)
        with self.assertRaises(ValueError):StampPolicy().resolve(0,0)

if __name__=='__main__':unittest.main()
