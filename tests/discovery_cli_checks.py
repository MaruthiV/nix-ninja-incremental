import argparse
from contextlib import closing
from pathlib import Path
import os
import subprocess
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView


class DiscoveryCliChecks(unittest.TestCase):
    binary = None

    def setUp(self):
        self.view = self.enterContext(closing(InputView.create()))
        with self.view.lease():
            self.build = self.view.root / "build"
            self.build.mkdir()

    def invoke(self, *args):
        env = dict(os.environ)
        for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
            env.pop(key, None)
        return subprocess.run([str(self.binary), "-C", str(self.build), *args],
                              env=env, text=True, capture_output=True, timeout=15)

    def test_help_exposes_all_discovery_modes(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("off, record, reuse", result.stdout)

    def test_persistent_modes_require_an_input_view(self):
        for mode in ["record", "reuse"]:
            with self.subTest(mode=mode):
                result = self.invoke("--discovery-mode", mode, "-t", "list")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires --input-view and --discovery-cache", result.stderr)

    def test_persistent_modes_require_a_cache_directory(self):
        for mode in ["record", "reuse"]:
            with self.subTest(mode=mode):
                result = self.invoke("--discovery-mode", mode, "--input-view", str(self.view.marker), "-t", "list")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires --discovery-cache", result.stderr)

    def test_all_modes_respect_the_controller_lease_and_recover(self):
        for mode in ["off", "record", "reuse"]:
            with self.subTest(mode=mode):
                args = ["--discovery-mode", mode, "--input-view", str(self.view.marker),
                        "--discovery-cache", str(self.view.control / "state"), "-t", "list"]
                with self.view.lease():
                    result = self.invoke(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("input view is busy", result.stderr)
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("nix-ninja subtools:", result.stdout)
                self.assertFalse((self.view.control / "state").exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discovery CLI and controller lease contract")
    parser.add_argument("--binary", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    DiscoveryCliChecks.binary = args.binary.resolve(strict=True)
    unittest.main(argv=[__file__, *remaining])
