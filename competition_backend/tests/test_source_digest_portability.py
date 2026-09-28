import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from competition_backend import polygon_coverage as coverage


class SourceDigestPortabilityTest(unittest.TestCase):
    def test_git_lf_and_windows_crlf_have_the_same_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ('polygon_coverage.py', 'competition_area.json', 'competition_landcover.json')
            with patch.object(coverage, '__file__', str(root / names[0])):
                for name in names:
                    (root / name).write_bytes(b'first line\nsecond line\n')
                lf = coverage.source_digest()
                for name in names:
                    (root / name).write_bytes(b'first line\r\nsecond line\r\n')
                self.assertEqual(coverage.source_digest(), lf)
                (root / names[1]).write_bytes(b'changed boundary\n')
                self.assertNotEqual(coverage.source_digest(), lf)


if __name__ == '__main__':
    unittest.main()
