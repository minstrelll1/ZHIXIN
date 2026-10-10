"""发布清单必须来自不可变提交，不能夹带本机文件或未提交修改。"""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("manifest_builder", ROOT / "tools/build_ground_update_manifest.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class GroundUpdateManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Manifest Test")
        self.git("config", "user.email", "manifest-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.root / "code.py").write_bytes(b"print('committed')\n")
        (self.root / "中文说明.txt").write_bytes("已提交版本\n".encode("utf-8"))
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.addCleanup(patch.stopall)
        patch.object(builder, "git", self.git).start()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE)

    def test_manifest_uses_git_objects_and_pins_version(self):
        (self.root / "code.py").write_bytes(b"uncommitted changes")
        (self.root / "local_tokens.ps1").write_text("private secret")
        m = builder.build_manifest("fixture/repo", "feature/a")
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), m["commit"])
        self.assertEqual(2, m["file_count"])
        files = {f["path"]: f for f in m["files"]}
        self.assertNotIn("local_tokens.ps1", files)
        for name, f in files.items():
            raw = self.git("show", m["commit"] + ":" + name)
            self.assertEqual(len(raw), f["size"])
            self.assertEqual(hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(), f["sha"])

    def test_rejects_invalid_identity_and_link_objects(self):
        for repo, branch in (("bad", "main"), ("fixture/repo", "../main"), ("fixture/repo", "a\\b")):
            with self.assertRaises(ValueError):
                builder.build_manifest(repo, branch)
        sha = self.git("rev-parse", "HEAD:code.py").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000," + sha + ",link")
        self.git("commit", "-qm", "link fixture")
        with self.assertRaises(ValueError):
            builder.build_manifest("fixture/repo", "main")


if __name__ == "__main__":
    unittest.main()
