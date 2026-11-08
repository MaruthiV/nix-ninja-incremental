import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import baseline_summary as summary


class BaselineSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "result.json"
        self.binary = "a" * 64
        self.plan = {"root": "current"}
        workload = json.loads((summary.ROOT / "benchmarks/workloads.json").read_text())["workloads"][1]
        pins = json.loads((summary.ROOT / "environment/pins.json").read_text())
        series = json.loads((summary.ROOT / "patches/series.json").read_text())["variant_postimages"]
        self.data = {"stage": "native contextual baseline overhead", "passed": True,
                     "workload": workload, "pins": pins, "rounds": 2, "seed": 2027,
                     "binary": "/nix/store/current/bin/nix-ninja", "binary_sha256": self.binary,
                     "postimages": series["recording"], "harness_sha256": "d" * 64,
                     "scenario": "no-op", "compiler_cache": "disabled",
                     "environment": {"host": "Linux x86_64", "cpu_quota": "400000 100000",
                                     "pins": pins, "daemon": {"version": pins["runtime"]["version"]}},
                     "resources": {"cpu.max": "400000 100000", "memory.max": "6442450944", "pids.max": "2048"},
                     "input_view": {"root": "/tmp/owned/inputs", "provenance": {"workload": workload}},
                     "baseline_binaries": {
                         "upstream": {"binary": "/nix/store/upstream/bin/nix-ninja", "binary_sha256": "b" * 64,
                                      "revision": pins["upstream"]["revision"], "working_tree": "clean"},
                         "corrected": {"binary": "/nix/store/corrected/bin/nix-ninja", "binary_sha256": "c" * 64,
                                       "postimages": series["corrected-embed"]}}, "compatibility": {}}
        for name in ["current", "upstream", "corrected"]:
            stem = "compatibility-" + name
            (self.root / f"{stem}.stdout").write_text(json.dumps({"root": "other"} if name == "upstream" else self.plan))
        reference = summary.realize.plan_digest(self.root, "compatibility-current")
        for name in ["current", "upstream", "corrected"]:
            stem = "compatibility-" + name
            binary = self.data if name == "current" else self.data["baseline_binaries"][name]
            row = {"name": stem, "returncode": 0, "elapsed_ns": 50,
                   "argv": summary.planner_command(self.data, binary["binary"], "/tmp/owned/baseline-current", "off" if name == "current" else None)}
            row.update(summary.compatibility(row, self.root, reference))
            if name == "current":
                row["discovery"] = self.audit("off", 0)
            (self.root / f"{stem}.stderr").write_text("NNI_DISCOVERY=" + json.dumps(row["discovery"]) if name == "current" else "")
            self.data["compatibility"][name] = row
        self.measurement = {"passed": True, "repetitions": 2, "seed": 2027, "jobs": 4,
                            "pins": pins, "view": self.data["input_view"], "cache_root": "/tmp/owned/cache",
                            "capture": {"passed": True, "input_view": self.data["input_view"]},
                            "modes": {}, "runs": []}
        (self.root / "capture.json").write_text(json.dumps(self.measurement["capture"]))
        for name in ["current", "corrected", "reuse"]:
            source = self.data if name in {"current", "reuse"} else self.data["baseline_binaries"][name]
            mode = {"binary": source["binary"], "sha256": source["binary_sha256"], "profile": False}
            if name in {"current", "reuse"}:
                mode["discovery"] = "off" if name == "current" else "reuse"
            self.measurement["modes"][name] = mode
        (self.root / "planning").mkdir()
        modes = list(self.measurement["modes"])
        order = [(name, -1, True) for name in modes]
        rng = random.Random(2027)
        for index in range(2):
            randomized = modes.copy()
            rng.shuffle(randomized)
            order.extend((name, index, False) for name in randomized)
        for name, index, warmup in order:
            stem = f"{'warmup' if warmup else index}-{name}"
            mode = self.measurement["modes"][name]
            row = {"mode": name, "round": index, "warmup": warmup, "returncode": 0,
                   "elapsed_ns": {"current": 140, "corrected": 100, "reuse": 90}[name],
                   "derivation_sha256": reference,
                   "argv": summary.planner_command(self.data, mode["binary"], Path(self.measurement["cache_root"]) / name, mode.get("discovery"))}
            stderr = ""
            if name in {"current", "reuse"}:
                row["discovery"] = self.audit(mode["discovery"], 2 if name == "reuse" and not warmup else 0)
                stderr = "NNI_DISCOVERY=" + json.dumps(row["discovery"])
            (self.root / "planning" / f"{stem}-stdout.json").write_text(json.dumps(self.plan))
            (self.root / "planning" / f"{stem}-stderr.log").write_text(stderr)
            self.measurement["runs"].append(row)
        self.data["comparisons"] = {"corrected": summary.comparison_statistics([(100, 140)] * 2)}
        self.data["reuse_comparisons"] = {"corrected": summary.comparison_statistics([(100, 90)] * 2, current_mode="reuse")}
        self.sync()

    def audit(self, mode, hits):
        return {"schema_version": 1, "mode": mode, "planning_complete": True, "calls": 3,
                "hits": hits, "scans": 3 - hits, "recorded_actions": 2 if mode == "reuse" else 0,
                "candidate_mismatches": 0, "validation_errors": 0,
                "namespace": "test" if mode == "reuse" else None,
                "publication": "published" if mode == "reuse" else "disabled"}

    def sync(self):
        self.data["measurement"] = self.measurement
        (self.root / "planning/result.json").write_text(json.dumps(self.measurement))
        self.path.write_text(json.dumps(self.data))

    def analyze(self, paths=None):
        return summary.summarize(paths or [self.path], self.root / "summary.json", ["zstd"], self.binary, rounds=2)

    def test_complete_partial_coverage_reuse_keeps_both_explicit_baselines(self):
        result = self.analyze()["results"][0]
        self.assertFalse(result["compatibility"]["upstream"]["comparable"])
        self.assertAlmostEqual(result["comparisons"]["corrected"]["median_current_overhead"], 0.4)
        self.assertAlmostEqual(result["reuse_comparisons"]["corrected"]["median_current_overhead"], -0.1)
        self.assertEqual(result["reuse_comparisons"]["corrected"]["current_mode"], "reuse")

    def test_legacy_off_only_calibration_is_not_relabelled_as_reuse(self):
        self.measurement["modes"].pop("reuse")
        self.measurement["runs"] = [row for row in self.measurement["runs"] if row["mode"] != "reuse"]
        rows = {(row["mode"], row["round"]): row for row in self.measurement["runs"]}
        ordered = [rows[name, -1] for name in ["current", "corrected"]]
        rng = random.Random(2027)
        for index in range(2):
            modes = ["current", "corrected"]
            rng.shuffle(modes)
            ordered.extend(rows[name, index] for name in modes)
        self.measurement["runs"] = ordered
        self.data.pop("reuse_comparisons")
        self.data["comparisons"]["corrected"].pop("current_mode")
        self.sync()
        result = self.analyze()["results"][0]
        self.assertEqual(result["reuse_comparisons"], {})
        self.assertEqual(result["comparisons"]["corrected"]["current_mode"], "off")

    def test_changed_raw_plan_is_rejected_and_failure_is_retained(self):
        (self.root / "planning/0-reuse-stdout.json").write_text(json.dumps({"root": "changed"}))
        with self.assertRaisesRegex(RuntimeError, "changed timed plan"):
            self.analyze()
        self.assertFalse(json.loads((self.root / "summary.json").read_text())["passed"])

    def test_omitted_eligible_hits_cannot_hide_behind_matching_plans(self):
        row = next(row for row in self.measurement["runs"] if row["mode"] == "reuse" and not row["warmup"])
        row["discovery"] = self.audit("reuse", 1)
        (self.root / "planning/0-reuse-stderr.log").write_text("NNI_DISCOVERY=" + json.dumps(row["discovery"]))
        self.sync()
        with self.assertRaisesRegex(RuntimeError, "omitted eligible hits"):
            self.analyze()

    def test_forged_compatibility_and_statistics_do_not_override_raw_evidence(self):
        self.data["compatibility"]["upstream"]["comparable"] = True
        self.path.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(RuntimeError, "raw compatibility"):
            self.analyze()
        (self.root / "summary.json").unlink()
        self.data["compatibility"]["upstream"]["comparable"] = False
        self.data["reuse_comparisons"]["corrected"]["median_current_overhead"] = -0.9
        self.path.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(RuntimeError, "statistics differ"):
            self.analyze()

    def test_failed_baselines_remain_exclusions_without_timing_ratios(self):
        row = self.data["compatibility"]["corrected"]
        row.update(returncode=1, comparable=False, reason="planning failed; no successful timing ratio")
        row.pop("plan_sha256")
        for key in ["measurement", "comparisons", "reuse_comparisons"]:
            self.data.pop(key)
        self.path.write_text(json.dumps(self.data))
        result = self.analyze()["results"][0]
        self.assertEqual(result["comparisons"], {})
        self.assertEqual(result["compatibility"]["corrected"]["returncode"], 1)

    def test_calibrations_cannot_be_pooled_or_duplicate_the_same_workload(self):
        other = self.root / "z-duplicate"
        other.mkdir()
        target = other / "result.json"
        target.write_text(self.path.read_text())
        with self.assertRaisesRegex(RuntimeError, "duplicate workload"):
            self.analyze([self.path, target])

    def test_wrong_mode_flags_or_reordered_measurements_are_rejected(self):
        original = copy.deepcopy(self.measurement)
        self.measurement["runs"][-1]["argv"][-1] = "another-target"
        self.sync()
        with self.assertRaisesRegex(RuntimeError, "timed argv"):
            self.analyze()
        (self.root / "summary.json").unlink()
        self.measurement = original
        self.measurement["runs"][-2:] = reversed(self.measurement["runs"][-2:])
        self.sync()
        with self.assertRaisesRegex(RuntimeError, "out-of-order"):
            self.analyze()

    def test_recorded_quota_does_not_override_actual_limits_or_runtime(self):
        self.data["resources"]["cpu.max"] = "200000 100000"
        self.path.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(RuntimeError, "machine limits"):
            self.analyze()
        (self.root / "summary.json").unlink()
        self.data["resources"]["cpu.max"] = "400000 100000"
        self.data["environment"]["daemon"]["version"] = "different-runtime"
        self.path.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(RuntimeError, "runtime environment"):
            self.analyze()


if __name__ == "__main__":
    unittest.main()
