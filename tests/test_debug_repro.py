import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import debug_repro


class DebugReproductionTests(unittest.TestCase):
    def flags(self, updates):
        env = {key: value for key, value in os.environ.items() if not key.startswith("NIX_CFLAGS_COMPILE")}
        env.pop("NIX_BUILD_TOP", None)
        env.update(updates)
        reader = "import json,os; print(json.dumps(dict(os.environ)))"
        return subprocess.run(["bash", "-c", '. "$1"; exec "$2" -c "$3"', "debug-flags",
                               str(ROOT / "benchmarks/cold-debug/task-flags.sh"), sys.executable, reader],
                              env=env, text=True, capture_output=True)

    def test_existing_salted_and_unsalted_compiler_flags_are_preserved(self):
        flags = {"NIX_CFLAGS_COMPILE": "-O2 -g", "NIX_CFLAGS_COMPILE_salt": "-isystem /nix/store/example/include",
                 "NIX_CFLAGS_COMPILE_BEFORE_salt": "-DVALUE=7"}
        result = self.flags({**flags, "NIX_BUILD_TOP": "/nix/var/nix/builds/unique-root",
                             "NIX_CC_WRAPPER_FLAGS_SET_salt": "1", "NIX_LDFLAGS_salt": "-z now"})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        for name, value in flags.items():
            self.assertEqual(actual[name], value + " -fdebug-prefix-map=/nix/var/nix/builds/unique-root=/build")
        self.assertEqual(actual["NIX_CC_WRAPPER_FLAGS_SET_salt"], "1")
        self.assertEqual(actual["NIX_LDFLAGS_salt"], "-z now")

    def test_missing_compiler_flags_get_a_map_and_missing_build_root_fails(self):
        result = self.flags({"NIX_BUILD_TOP": "/nix/var/nix/builds/unique-root"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["NIX_CFLAGS_COMPILE"],
                         " -fdebug-prefix-map=/nix/var/nix/builds/unique-root=/build")
        result = self.flags({})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def rows(self):
        return [{"condition": condition, "repetition": repetition, "source_sha256": "source",
                 "plan_sha256": condition + "-plan", "debug_info_present": True,
                 "artifacts": {target: {"sha256": str(repetition) if condition == "legacy" else "same"}
                               for target in ["app", "main.o"]}}
                for condition in ["legacy", "mapped"] for repetition in range(2)]

    def test_red_green_diagnostic_requires_equal_mapped_objects_and_executables(self):
        rows = self.rows()
        self.assertTrue(all(debug_repro.compare_pairs(rows)["mapped"]["byte_equal"].values()))
        for name in ["app", "main.o"]:
            corrupted = copy.deepcopy(rows)
            corrupted[-1]["artifacts"][name]["sha256"] = "different"
            with self.assertRaisesRegex(RuntimeError, "byte-identical"):
                debug_repro.compare_pairs(corrupted)

    def test_diagnostic_rejects_missing_debug_info_plan_changes_or_absent_red_case(self):
        for change in ["debug", "plan", "legacy", "source", "coverage"]:
            rows = self.rows()
            if change == "debug":
                rows[-1]["debug_info_present"] = False
            elif change == "plan":
                rows[-1]["plan_sha256"] = "different-plan"
            elif change == "source":
                rows[-1]["source_sha256"] = "different-source"
            elif change == "legacy":
                rows[1]["artifacts"] = rows[0]["artifacts"]
            else:
                rows.pop()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                debug_repro.compare_pairs(rows)


if __name__ == "__main__":
    unittest.main()
