import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import evaluate_operations as operations
import evaluate_planning
import operation_profile
import operation_summary
import realize
import test_evaluate_operations


class OperationSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.destination = Path(temporary.name)
        self.path = self.destination / "result.json"
        manifest, patch, postimages = operation_profile.configuration()
        self.workload = json.loads((ROOT / "benchmarks/workloads.json").read_text())["workloads"][0]
        pins = json.loads((ROOT / "environment/pins.json").read_text())
        self.root = self.destination / "owned/inputs"
        self.binaries = {"frozen": "/nix/store/frozen/bin/nix-ninja", "profiled": "/nix/store/profiled/bin/nix-ninja"}
        probes = self.workload["content_probes"]
        self.original = {probes["implementation"]: "original-impl", probes["shared_header"]: "original-header",
                         ".nni-irrelevant.txt": None}
        self.data = {"passed": True, "workload": self.workload, "rounds": 1, "seed": 2027, "pins": pins,
                     "patch_series": operation_profile.variants.SERIES,
                     "postimages": operation_profile.variants.SERIES["variant_postimages"]["recording"],
                     "diagnostic_postimages": postimages, "operation_manifest": manifest,
                     "harness_sha256": realize.digest(Path(evaluate_planning.__file__)),
                     "operation_driver_sha256": realize.digest(Path(operations.__file__)),
                     "binary": self.binaries["frozen"], "binary_sha256": manifest["reference_binary_sha256"],
                     "diagnostic_binary": self.binaries["profiled"], "diagnostic_binary_sha256": "instrumented",
                     "environment": {"host": "Linux x86_64", "cpu_quota": "400000 100000", "pins": pins,
                                     "daemon": {"version": pins["runtime"]["version"]}},
                     "resources": {"cpu.max": "400000 100000", "memory.max": "6442450944", "pids.max": "2048"},
                     "helper_path": "/nix/store/frozen/bin:/common/helpers", "input_view": {"root": str(self.root)},
                     "runs": [], "pairs": [], "original_source": self.original}
        (self.destination / "operation-manifest.json").write_text(json.dumps(manifest))
        shutil.copyfile(patch, self.destination / "instrumentation.patch")
        for name, binary in self.binaries.items():
            file = "package.stdout" if name == "frozen" else "profile-package.stdout"
            (self.destination / file).write_text(str(Path(binary).parent.parent) + "\n")
        for case in evaluate_planning.CASES:
            current = dict(self.original)
            if case == "implementation-edit":
                current[probes["implementation"]] = "edited-impl"
            elif case == "shared-header-edit":
                current[probes["shared_header"]] = "edited-header"
            elif case == "irrelevant-edit":
                current[".nni-irrelevant.txt"] = "irrelevant"
            self.add_row(f"prewarm-{case}", "frozen", "off", "store-warmup", case, current)
            pair = {"scenario": case, "warmups": [], "measured": [], "passed": True}
            for variant in self.binaries:
                name = f"{case}-warmup-{variant}"
                self.add_row(name, variant, "reuse", "metadata-warmup", "no-op", self.original)
                pair["warmups"].append(name)
                for mode in ["off", "reuse"]:
                    name = f"{case}-{variant}-{mode}"
                    self.add_row(name, variant, mode, "diagnostic", case, current)
                    pair["measured"].append(name)
            self.data["pairs"].append(pair)

    def add_row(self, name, variant, mode, kind, case, state):
        hits = 0 if mode == "off" or kind != "diagnostic" else (2 if case in {"no-op", "irrelevant-edit"} else 1)
        audit = {"schema_version": 1, "mode": mode, "planning_complete": True, "calls": 2, "hits": hits, "scans": 2 - hits,
                 "recorded_actions": 2 if mode == "reuse" else 0, "candidate_mismatches": 0, "validation_errors": 0,
                 "namespace": None if mode == "off" else "synthetic", "publication": "disabled" if mode == "off" else "published"}
        cache = self.root.parent / "operation-state" / name
        plan = json.dumps({"state": state}, sort_keys=True, separators=(",", ":"))
        row = {"name": name, "variant": variant, "mode": mode, "kind": kind, "scenario": case, "input_state": state,
               "returncode": 0, "elapsed_ns": 1, "discovery": audit, "plan_sha256": hashlib.sha256(plan.encode()).hexdigest(),
               "argv": [self.binaries[variant], "-C", str(self.root / "build"), "-j4", "-t", "drv", "--input-view",
                        str(self.root.parent / "view.json"), "--discovery-mode", mode, "--discovery-cache", str(cache), self.workload["target"]]}
        (self.destination / f"{name}.stdout").write_text(plan)
        (self.destination / f"{name}.stderr").write_text("NNI_DISCOVERY=" + json.dumps(audit) + "\n")
        if variant == "profiled":
            profile = test_evaluate_operations.OperationProfileTests().profile()
            profile["build_directory"] = str(self.root / "build")
            row["profile_file"] = name + "-profile.json"
            (self.destination / row["profile_file"]).write_text(json.dumps(profile))
            row["counts"] = operations.profile_counts(profile, self.root / "build")
        self.data["runs"].append(row)

    def verify(self, data=None):
        return operation_summary.verify(self.path, data or self.data, {self.workload["name"]: self.workload})

    def test_complete_raw_fixture_verifies_all_four_scenarios(self):
        result = self.verify()
        self.assertEqual({row["scenario"] for row in result}, set(evaluate_planning.CASES))
        self.assertEqual(result[0]["counts"]["reuse"]["store.add_derivation"], 7)

    def test_changed_raw_profile_or_raw_plan_cannot_pass_cached_summary_values(self):
        row = next(row for row in self.data["runs"] if row["variant"] == "profiled" and row["kind"] == "diagnostic")
        file = self.destination / row["profile_file"]
        original = file.read_text()
        profile = json.loads(original)
        profile["phases"]["store.add_derivation"]["calls"] += 1
        file.write_text(json.dumps(profile))
        with self.assertRaisesRegex(RuntimeError, "raw operation counters"):
            self.verify()
        file.write_text(original)
        (self.destination / f"{row['name']}.stdout").write_text('{"different":"plan"}')
        with self.assertRaisesRegex(RuntimeError, "raw derivation"):
            self.verify()

    def test_diagnostic_helper_leaks_and_wrong_binary_argv_are_rejected(self):
        for change in ["path", "argv"]:
            data = copy.deepcopy(self.data)
            if change == "path":
                data["helper_path"] += ":/nix/store/profiled/bin"
            else:
                data["runs"][0]["argv"][0] = self.binaries["profiled"]
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.verify(data)

    def test_missing_cases_or_post_edit_metadata_warmups_are_rejected(self):
        for change in ["coverage", "warmup"]:
            data = copy.deepcopy(self.data)
            if change == "coverage":
                data["pairs"].pop()
            else:
                row = next(row for row in data["runs"] if row["kind"] == "metadata-warmup")
                row["input_state"] = {"wrong": "post-edit"}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.verify(data)


if __name__ == "__main__":
    unittest.main()
