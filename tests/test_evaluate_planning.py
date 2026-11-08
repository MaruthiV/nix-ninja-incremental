from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluate_planning
from input_view import InputView


class PairedPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.view = InputView.create(parent=self.root)
        self.addCleanup(self.view.close)
        self.source = self.view.root / "source"
        self.source.mkdir()
        (self.view.root / "build").mkdir()
        (self.source / "impl.c").write_text("original implementation\n")
        (self.source / "value.h").write_text("original header\n")
        self.workload = {"target": "app", "content_probes": {"implementation": "impl.c", "shared_header": "value.h",
                                                            "append": "\n#define NNI_BASELINE_CONTENT_PROBE 1\n"}}
        self.destination = self.root / "results"
        self.destination.mkdir()
        self.original = self.fingerprints()
        self.calls = []

    def fingerprints(self):
        return {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest() for name in ["impl.c", "value.h"]}

    def process(self, argv, env, destination, name):
        mode = argv[argv.index("--discovery-mode") + 1]
        cache = Path(argv[argv.index("--discovery-cache") + 1])
        current = self.fingerprints()
        previous = json.loads((cache / "state.json").read_text()) if (cache / "state.json").exists() else None
        with self.view.lease():
            pass
        self.calls.append({"name": name, "mode": mode, "current": current, "previous": previous})
        hits = sum(previous.get(key) == value for key, value in current.items()) if previous and mode == "reuse" else 0
        if mode == "reuse":
            cache.mkdir(mode=0o700, exist_ok=True)
            (cache / "state.json").write_text(json.dumps(current))
        audit = {"schema_version": 1, "mode": mode, "planning_complete": True, "calls": 2,
                 "hits": hits, "scans": 2 - hits, "recorded_actions": 2 if mode == "reuse" else 0,
                 "candidate_mismatches": 0, "validation_errors": 0,
                 "namespace": "synthetic" if mode == "reuse" else None,
                 "publication": "published" if mode == "reuse" else "disabled"}
        (destination / f"{name}.stdout").write_text(json.dumps(current))
        (destination / f"{name}.stderr").write_text("NNI_DISCOVERY=" + json.dumps(audit) + "\n")
        return {"argv": argv, "returncode": 0, "elapsed_ns": 1}

    def evaluate(self, runner=None):
        report = {}
        with patch.object(evaluate_planning, "timed_process", side_effect=runner or self.process), redirect_stdout(io.StringIO()):
            evaluate_planning.paired_plans(Path("/synthetic/planner"), self.workload, self.view,
                                           self.destination, report, 2, 2027)
        return report

    def test_edits_follow_pre_edit_metadata_and_measured_inputs_match(self):
        report = self.evaluate()
        self.assertEqual(len(report["pairs"]), 8)
        for pair in report["pairs"]:
            warmups = [call for call in self.calls if call["name"] in pair["warmups"]]
            self.assertTrue(all(call["current"] == self.original for call in warmups))
            reuse = next(call for call in self.calls if call["name"] in pair["measured"] and call["mode"] == "reuse")
            self.assertEqual(reuse["previous"], self.original)
            measured = [call for call in self.calls if call["name"] in pair["measured"]]
            self.assertEqual(measured[0]["current"], measured[1]["current"])
            self.assertTrue(pair["passed"])
        self.assertEqual(self.fingerprints(), self.original)
        self.assertFalse((self.source / ".nni-irrelevant.txt").exists())

    def test_seeded_order_is_reproducible_and_contains_both_mode_orders(self):
        report = self.evaluate()
        orders = [[name.rsplit("-", 1)[1] for name in pair["measured"]] for pair in report["pairs"]]
        self.assertIn(["off", "reuse"], orders)
        self.assertIn(["reuse", "off"], orders)
        self.assertEqual({pair["scenario"] for pair in report["pairs"]}, set(evaluate_planning.CASES))

    def test_mismatched_plans_fail_and_restore_source_and_lease(self):
        def mismatched(argv, env, destination, name):
            result = self.process(argv, env, destination, name)
            if "-timed-reuse" in name:
                (destination / f"{name}.stdout").write_text('{"different": true}')
            return result

        with self.assertRaisesRegex(RuntimeError, "paired derivations differ"):
            self.evaluate(mismatched)
        self.assertEqual(self.fingerprints(), self.original)
        with self.view.lease():
            pass

    def test_an_all_hit_first_post_edit_probe_is_rejected(self):
        def stale(argv, env, destination, name):
            result = self.process(argv, env, destination, name)
            if "-edit-timed-reuse" in name and "irrelevant" not in name:
                path = destination / f"{name}.stderr"
                audit = json.loads(path.read_text().split("=", 1)[1])
                audit.update(hits=2, scans=0)
                path.write_text("NNI_DISCOVERY=" + json.dumps(audit) + "\n")
            return result

        with self.assertRaisesRegex(RuntimeError, "first post-edit plan did not invalidate"):
            self.evaluate(stale)

    def test_process_exit_and_resource_usage_are_retained(self):
        with patch.object(evaluate_planning, "resources", return_value={}):
            result = evaluate_planning.timed_process([sys.executable, "-c", "import sys; print('kept'); sys.exit(7)"],
                                                     dict(os.environ), self.destination, "failure")
        self.assertEqual(result["returncode"], 7)
        self.assertGreater(result["elapsed_ns"], 0)
        self.assertGreater(result["max_rss_bytes"], 0)
        self.assertEqual((self.destination / "failure.stdout").read_text().strip(), "kept")


if __name__ == "__main__":
    unittest.main()
