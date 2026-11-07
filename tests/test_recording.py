import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_checks import compare
from tools.build_oracle import mutate


class RecordingOracleTests(unittest.TestCase):
    def baseline(self):
        return {"passed": True, "steps": [{"name": "edit", "plan_sha256": "a" * 64,
                "actual_observation": {"class": "success", "program_exit": 3, "sha256": "b" * 64}}]}

    def test_plan_change_is_detected_even_when_artifacts_match(self):
        disabled = self.baseline()
        changed = copy.deepcopy(disabled)
        compare(disabled, changed)
        changed["steps"][0]["plan_sha256"] = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "derivation"):
            compare(disabled, changed)

    def test_artifact_change_is_detected_even_when_program_exit_matches(self):
        disabled = self.baseline()
        changed = copy.deepcopy(disabled)
        changed["steps"][0]["actual_observation"]["sha256"] = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "artifact"):
            compare(disabled, changed)

    def test_fixture_file_kind_changes_preserve_nonempty_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve()
            mutate(source, {"write": {"probe.h": "data"}})
            mutate(source, {"remove": ["probe.h"], "directories": ["probe.h"]})
            self.assertTrue((source / "probe.h").is_dir())
            mutate(source, {"write": {"probe.h/keep": "keep"}})
            with self.assertRaises(OSError):
                mutate(source, {"remove": ["probe.h"]})
            self.assertEqual((source / "probe.h/keep").read_text(), "keep")
            mutate(source, {"remove": ["probe.h/keep", "probe.h"], "write": {"probe.h": "new"}})
            self.assertEqual((source / "probe.h").read_text(), "new")


if __name__ == "__main__":
    unittest.main()
