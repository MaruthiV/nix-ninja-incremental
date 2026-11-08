import argparse
import os
from pathlib import Path
import subprocess
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView
import profile_checks


class OperationProfileChecks(profile_checks.ProfileChecks):
    def test_profile_output_cannot_enter_owned_source_inputs(self):
        view = InputView.create(parent=self.root)
        self.addCleanup(view.close)
        with view.lease():
            build = view.root / "build"
            source = view.root / "source"
            build.mkdir()
            source.mkdir()
        path = source / "profile.json"
        result = subprocess.run(
            [str(self.binary), "-C", str(build), "--input-view", str(view.marker), "-t", "unknown"],
            env=dict(os.environ, NIX_NINJA_PROFILE=str(path)), text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside owned inputs", result.stderr)
        self.assertFalse(path.exists())

    def test_inside_derivation_profiling_is_rejected_before_output(self):
        path = self.root / "profile.json"
        result = subprocess.run(
            [str(self.binary), "-C", str(self.build), "-t", "unknown"],
            env=dict(os.environ, NIX_NINJA_PROFILE=str(path), NIX_NINJA_DRV="true"),
            text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("diagnostics require local mode", result.stderr)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    OperationProfileChecks.binary = args.binary.resolve(strict=True)
    unittest.main(argv=[__file__, *remaining])
