import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_onboard_messages", ROOT / "tools/check_onboard_messages.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class GeneratedMessageCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = {
            "CompletedTarget": ("new-md5", ("global_id", "detection_count", "tracking_success"),
                                ("string", "uint64", "bool")),
            "CompletedTargetArray": ("new-array-md5", ("targets",),
                                     ("su17_image_transfer/CompletedTarget[]",)),
        }

    def module(self, name, md5=None, names=None, types=None, model="p600"):
        expected_md5, expected_names, expected_types = self.expected[name]
        cls = type(name, (), {
            "_md5sum": md5 or expected_md5,
            "__slots__": expected_names if names is None else names,
            "_slot_types": expected_types if types is None else types,
        })
        return SimpleNamespace(
            __file__=str(self.root / ("devel_" + model) / "lib/python3/dist-packages/su17_image_transfer/msg" / ("_" + name + ".py")),
            **{name: cls})

    def verify(self, modules, model="p600"):
        with patch.object(CHECK.importlib, "import_module",
                          side_effect=lambda fullname: modules[fullname.rsplit("._", 1)[1]]):
            return CHECK.verify_generated(self.root, model, self.expected)

    def test_current_contract_passes_for_both_models(self):
        for model in ("p600", "su17"):
            with self.subTest(model=model):
                modules = {name: self.module(name, model=model) for name in self.expected}
                self.assertEqual(len(self.verify(modules, model)), 2)

    def test_stale_class_without_new_quality_fields_is_rejected(self):
        modules = {name: self.module(name) for name in self.expected}
        modules["CompletedTarget"] = self.module("CompletedTarget", "old-md5",
                                                 ("global_id",), ("string",))
        with self.assertRaisesRegex(RuntimeError, "源码与生成消息不一致"):
            self.verify(modules)

    def test_array_md5_must_also_be_regenerated(self):
        modules = {name: self.module(name) for name in self.expected}
        modules["CompletedTargetArray"] = self.module("CompletedTargetArray", "old-array-md5")
        with self.assertRaisesRegex(RuntimeError, "CompletedTargetArray"):
            self.verify(modules)

    def test_same_hash_wrong_field_type_is_rejected(self):
        modules = {name: self.module(name) for name in self.expected}
        modules["CompletedTarget"] = self.module(
            "CompletedTarget", types=("string", "uint32", "bool"))
        with self.assertRaises(RuntimeError):
            self.verify(modules)

    def test_import_from_wrong_overlay_is_rejected(self):
        modules = {name: self.module(name, model="su17") for name in self.expected}
        with self.assertRaisesRegex(RuntimeError, "其他工作空间"):
            self.verify(modules)

    def test_validation_is_independent_of_source_file_timestamp(self):
        source = self.root / "src/su17_image_transfer/msg/CompletedTarget.msg"
        source.parent.mkdir(parents=True)
        source.write_text("string global_id\nuint64 detection_count\nbool tracking_success\n")
        import os
        os.utime(source, (1, 1))
        modules = {name: self.module(name) for name in self.expected}
        modules["CompletedTarget"] = self.module("CompletedTarget", "old-md5")
        with self.assertRaises(RuntimeError):
            self.verify(modules)


if __name__ == "__main__":
    unittest.main()
