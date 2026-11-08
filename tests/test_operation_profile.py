import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import operation_profile


class OperationProfileSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "patches").mkdir()
        destination = self.root / "benchmarks/operations"
        destination.mkdir(parents=True)
        shutil.copyfile(operation_profile.ROOT / "patches/series.json", self.root / "patches/series.json")
        for name in ["manifest.json", "instrumentation.patch"]:
            shutil.copyfile(operation_profile.MANIFEST.parent / name, destination / name)
        self.manifest = destination / "manifest.json"
        self.root_patch = patch.object(operation_profile, "ROOT", self.root)
        self.manifest_patch = patch.object(operation_profile, "MANIFEST", self.manifest)
        self.root_patch.start()
        self.manifest_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.manifest_patch.stop)

    def test_diagnostic_manifest_preserves_the_complete_frozen_base(self):
        data, file, expected = operation_profile.configuration()
        self.assertEqual(data["base_variant"], "recording")
        self.assertEqual(file.name, "instrumentation.patch")
        base = operation_profile.variants.SERIES["variant_postimages"]["recording"]
        changed = data["patch"]["postimage_sha256"]
        self.assertEqual({name: expected[name] for name in base if name not in changed},
                         {name: digest for name, digest in base.items() if name not in changed})

    def test_changed_base_registry_or_diagnostic_patch_fails_before_application(self):
        series = self.root / "patches/series.json"
        original = series.read_bytes()
        series.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "base patch registry"):
            operation_profile.configuration()
        series.write_bytes(original)
        patch_path = self.manifest.parent / "instrumentation.patch"
        patch_path.write_bytes(patch_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "patch checksum"):
            operation_profile.configuration()

    def test_patch_paths_cannot_escape_the_diagnostic_directory(self):
        data = json.loads(self.manifest.read_text())
        data["patch"]["file"] = "../instrumentation.patch"
        self.manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "unsupported operation-profile manifest"):
            operation_profile.configuration()

    def test_registered_source_worktrees_are_never_converted_to_diagnostics(self):
        with patch.object(operation_profile.variants, "prepare") as prepare:
            for name in operation_profile.variants.SERIES["variants"]:
                source = self.root / ".work" / f"nix-ninja-{name}"
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "own worktree"):
                    operation_profile.prepare(source)
            prepare.assert_not_called()
