import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ProfileChecks(unittest.TestCase):
    binary = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nni-profile-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.build = self.root / "build"
        self.build.mkdir()

    def invoke(self, path):
        return subprocess.run(
            [str(self.binary), "-C", str(self.build), "-t", "unknown"],
            env=dict(os.environ, NIX_NINJA_PROFILE=str(path)),
            text=True, capture_output=True,
        )

    def test_failed_command_keeps_diagnostics(self):
        path = self.root / "profile.json"
        result = self.invoke(path)
        self.assertNotEqual(result.returncode, 0)
        record = json.loads(path.read_text())
        self.assertEqual(record["mode"], "planning-profile")
        self.assertEqual(record["build_directory"], str(self.build))
        self.assertGreaterEqual(record["elapsed_ns"], 0)

    def test_existing_record_is_preserved(self):
        path = self.root / "profile.json"
        original = b"existing result\n"
        path.write_bytes(original)
        self.assertNotEqual(self.invoke(path).returncode, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_diagnostics_cannot_become_build_inputs(self):
        path = self.build / "profile.json"
        result = self.invoke(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the build directory", result.stderr)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    ProfileChecks.binary = args.binary.resolve(strict=True)
    unittest.main(argv=[__file__, *remaining])
