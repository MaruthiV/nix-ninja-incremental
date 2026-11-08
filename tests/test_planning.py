from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView
import planning


class PlanningLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.view = InputView.create(parent=self.root, provenance={"workload": {"target": "result"}})
        (self.view.control / "capture.json").write_text('{"passed": true}')
        self.build = self.view.root / "build"
        self.build.mkdir()
        self.configuration = self.build / "stub.json"
        self.configuration.write_text("{}")
        fixture = Path(__file__).parent / "fixtures/planning_stub.py"
        self.binary = self.root / "planner"
        self.binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        self.binary.chmod(0o700)

    def measure(self, discovery):
        args = SimpleNamespace(view=self.view.marker, output=self.root / "report",
                               binary=[f"{name}={self.binary}" for name in discovery], profile=[],
                               discovery=[f"{name}={mode}" for name, mode in discovery.items() if mode],
                               jobs=1, repetitions=1, seed=2027)
        with patch.object(planning, "resources", return_value={}), redirect_stdout(io.StringIO()):
            return planning.measure(args)

    def test_legacy_lease_and_planner_owned_modes_can_share_one_measurement(self):
        report = self.measure({"legacy": None, "disabled": "off", "recording": "record"})
        self.assertTrue(report["passed"])
        self.assertTrue(Path(report["cache_root"]).is_relative_to(self.view.control))
        self.assertFalse(Path(report["cache_root"]).is_relative_to(self.root / "report"))
        self.assertEqual(len({run["derivation_sha256"] for run in report["runs"]}), 1)
        warm = next(run for run in report["runs"] if run["mode"] == "recording" and not run["warmup"])
        self.assertEqual(warm["discovery"]["validated_candidates"], 1)
        with self.view.lease():
            pass

    def test_audit_mismatch_retains_failure_and_releases_the_controller_lease(self):
        self.configuration.write_text(json.dumps({"audit": {"candidate_mismatches": 1}}))
        with self.assertRaisesRegex(RuntimeError, "audit failed"):
            self.measure({"recording": "record"})
        result = json.loads((self.root / "report/result.json").read_text())
        self.assertFalse(result["passed"])
        self.assertIn("audit failed", result["error"])
        self.assertIn("NNI_DISCOVERY=", (self.root / "report/warmup-recording-stderr.log").read_text())
        with self.view.lease():
            pass

    def test_reuse_measurement_requires_real_warm_hit_counters(self):
        report = self.measure({"reusing": "reuse"})
        self.assertTrue(report["passed"])
        self.assertEqual(report["runs"][0]["discovery"]["hits"], 0)
        self.assertEqual(report["runs"][1]["discovery"]["hits"], 1)
        self.assertEqual(report["runs"][1]["discovery"]["scans"], 0)

    def test_always_scanning_cannot_pass_as_warm_reuse(self):
        self.configuration.write_text(json.dumps({"audit": {"hits": 0, "scans": 1}}))
        with self.assertRaisesRegex(RuntimeError, "did not exercise persisted records"):
            self.measure({"reusing": "reuse"})
        self.assertFalse(json.loads((self.root / "report/result.json").read_text())["passed"])
        with self.view.lease():
            pass

    def test_record_mode_cannot_report_hits(self):
        self.configuration.write_text(json.dumps({"audit": {"hits": 1, "scans": 0}}))
        with self.assertRaisesRegex(RuntimeError, "audit failed"):
            self.measure({"recording": "record"})

    def test_reuse_cannot_double_count_a_hit_and_a_scan(self):
        self.configuration.write_text(json.dumps({"audit": {"hits": 1, "scans": 1}}))
        with self.assertRaisesRegex(RuntimeError, "audit failed"):
            self.measure({"reusing": "reuse"})


if __name__ == "__main__":
    unittest.main()
