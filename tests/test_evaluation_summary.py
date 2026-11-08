import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluation_summary as summary


class EvaluationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = "a" * 64
        self.workload = json.loads((summary.ROOT / "benchmarks/workloads.json").read_text())["workloads"][1]
        probes = self.workload["content_probes"]
        original = {probes["implementation"]: "b" * 64, probes["shared_header"]: "c" * 64, ".nni-irrelevant.txt": None}
        self.data = {"stage": "frozen paired planning evaluation", "passed": True, "binary_sha256": self.binary,
                     "workload": self.workload, "seed": 2027, "rounds": 2, "runs": [], "pairs": [],
                     "original_source": original, "capture_ns": 10, "harness_sha256": "d" * 64, "machine": "synthetic",
                     "pins": json.loads((summary.ROOT / "environment/pins.json").read_text()),
                     "postimages": json.loads((summary.ROOT / "patches/series.json").read_text())["variant_postimages"]["recording"],
                     "environment": {"host": "Linux x86_64", "cpu_quota": "400000 100000"},
                     "resources": {"memory.max": "6442450944", "pids.max": "2048"}}
        for index in range(2):
            for case in summary.evaluate_planning.CASES:
                prefix = f"r{index}-{case}"
                pair = {"round": index, "scenario": case, "passed": True, "warmups": [], "measured": [],
                        "metadata_bytes": 100, "reset_ns": 1, "edit_ns": 1, "cleanup_ns": 1}
                for mode in ["off", "reuse"]:
                    name = f"{prefix}-warmup-{mode}"
                    pair["warmups"].append(name)
                    self.add_row(name, mode, "metadata-warmup", case, index, original, 0)
                current = dict(original)
                if case == "implementation-edit":
                    current[probes["implementation"]] = "e" * 64
                elif case == "shared-header-edit":
                    current[probes["shared_header"]] = "f" * 64
                elif case == "irrelevant-edit":
                    current[".nni-irrelevant.txt"] = "1" * 64
                for mode in ["off", "reuse"]:
                    name = f"{prefix}-timed-{mode}"
                    pair["measured"].append(name)
                    hits = (2 if case in {"no-op", "irrelevant-edit"} else 1) if mode == "reuse" else 0
                    self.add_row(name, mode, "measured", case, index, current, hits)
                self.data["pairs"].append(pair)
        self.path = self.root / "result.json"

    def add_row(self, name, mode, kind, case, index, state, hits):
        audit = {"schema_version": 1, "mode": mode, "planning_complete": True, "calls": 2, "hits": hits,
                 "scans": 2 - hits, "recorded_actions": 2 if mode == "reuse" else 0,
                 "candidate_mismatches": 0, "validation_errors": 0,
                 "namespace": "synthetic" if mode == "reuse" else None,
                 "publication": "published" if mode == "reuse" else "disabled"}
        (self.root / f"{name}.stdout").write_text(json.dumps(state))
        (self.root / f"{name}.stderr").write_text("NNI_DISCOVERY=" + json.dumps(audit) + "\n")
        self.data["runs"].append({"name": name, "mode": mode, "kind": kind, "scenario": case, "round": index,
                                  "returncode": 0, "elapsed_ns": 100 if mode == "off" else 80,
                                  "discovery": audit, "input_state": dict(state),
                                  "plan_sha256": summary.realize.plan_digest(self.root, name)})

    def analyze(self, paths=None):
        self.path.write_text(json.dumps(self.data))
        return summary.summarize(paths or [self.path], self.root / "summary.json", "planning", 2, ["zstd"], self.binary)

    def test_complete_synthetic_reports_keep_every_pair_and_target(self):
        result = self.analyze()
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["results"]), 4)
        for row in result["results"]:
            self.assertEqual(row["pairs"], 2)
            self.assertAlmostEqual(row["median_paired_improvement"], 0.2)
            self.assertEqual(len(row["observations"]), 2)
            self.assertFalse(row["noise_extension_eligible"])
            if row["scenario"] in {"no-op", "implementation-edit"}:
                self.assertTrue(row["median_target_met"])

    def test_missing_pair_fails_and_keeps_a_failed_summary(self):
        self.data["pairs"].pop()
        self.data["runs"] = [row for row in self.data["runs"] if row["name"] in
                             {name for pair in self.data["pairs"] for name in pair["warmups"] + pair["measured"]}]
        with self.assertRaisesRegex(RuntimeError, "incomplete evaluation"):
            self.analyze()
        self.assertFalse(json.loads((self.root / "summary.json").read_text())["passed"])

    def test_duplicate_bundle_cannot_inflate_coverage(self):
        with self.assertRaisesRegex(RuntimeError, "duplicate input"):
            self.analyze([self.path, self.path])

    def test_mixed_binary_and_after_edit_warmups_are_rejected(self):
        original = copy.deepcopy(self.data)
        self.data["binary_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "mixed binary"):
            self.analyze()
        (self.root / "summary.json").unlink()
        self.data = original
        row = next(row for row in self.data["runs"] if row["scenario"] == "implementation-edit" and row["kind"] == "metadata-warmup")
        row["input_state"][self.workload["content_probes"]["implementation"]] = "e" * 64
        with self.assertRaisesRegex(RuntimeError, "warmed after the edit"):
            self.analyze()

    def test_disagreement_with_raw_audit_is_rejected(self):
        self.data["runs"][0]["discovery"]["calls"] = 3
        with self.assertRaisesRegex(RuntimeError, "raw discovery report differs"):
            self.analyze()

    def test_statistics_use_paired_effects_and_retain_regressions(self):
        result = summary.paired_statistics([(100, 50), (1000, 900)])
        self.assertAlmostEqual(result["median_paired_improvement"], 0.3)
        self.assertEqual(result, summary.paired_statistics([(100, 50), (1000, 900)]))
        result = summary.paired_statistics([(100, 120)] * 10)
        self.assertAlmostEqual(result["median_paired_improvement"], -0.2)
        self.assertAlmostEqual(result["paired_bootstrap_95_interval"][0], -0.2)
        self.assertEqual(result["median_paired_saved_ns"], -20)

    def test_costs_keep_overlapping_work_separate_and_missing_measurements_visible(self):
        result = self.analyze()
        costs = result["results"][0]["costs"]
        self.assertEqual(costs["metadata_bytes_median"], 100)
        self.assertEqual(costs["modes"]["reuse"]["max_rss_bytes"], {"median": None, "samples": 0})
        self.assertEqual(costs["modes"]["off"]["hits"], {"median": 0, "samples": 2})
        self.assertIn("not additive", costs["worker_counter_scope"])


if __name__ == "__main__":
    unittest.main()
