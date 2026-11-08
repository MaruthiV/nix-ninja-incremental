from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView
import realize


class RealizationLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.view = InputView.create(parent=self.root)
        self.addCleanup(self.view.close)
        self.build = self.view.root / "build"
        self.build.mkdir()
        self.configuration = self.build / "stub.json"
        self.configuration.write_text("{}")
        self.destination = self.root / "records"
        self.destination.mkdir()
        self.binary = self.root / "planner"
        fixture = Path(__file__).parent / "fixtures/planning_stub.py"
        self.binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        self.binary.chmod(0o700)

    def invoke(self, controller, name, report, mode=None, tool=None):
        return realize.invoke_planner(self.binary, self.view, "result", dict(os.environ),
                                      self.destination, name, report, controller, mode, tool)

    def test_legacy_invocation_keeps_the_controller_lease(self):
        report = {}
        with ExitStack() as controller:
            controller.enter_context(self.view.lease())
            result = self.invoke(controller, "legacy", report)
            self.assertEqual(result["returncode"], 0)
            self.assertNotIn("discovery", result)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with self.view.lease():
                    pass

    def test_owned_invocations_release_and_reacquire_the_controller_lease(self):
        report = {}
        with ExitStack() as controller:
            controller.enter_context(self.view.lease())
            cold = self.invoke(controller, "cold", report, "reuse", "drv")
            warm = self.invoke(controller, "warm", report, "reuse")
            self.assertEqual(cold["discovery"]["hits"], 0)
            self.assertEqual(warm["discovery"]["hits"], 1)
            self.assertEqual(realize.plan_digest(self.destination, "cold"), realize.plan_digest(self.destination, "warm"))
            self.assertEqual(warm["argv"][-1], "result")
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with self.view.lease():
                    pass
        with self.view.lease():
            pass

    def test_audit_failure_retains_diagnostics_and_reacquires_the_lease(self):
        self.configuration.write_text(json.dumps({"audit": {"candidate_mismatches": 1}}))
        report = {}
        with ExitStack() as controller:
            controller.enter_context(self.view.lease())
            with self.assertRaisesRegex(RuntimeError, "audit failed"):
                self.invoke(controller, "mismatch", report, "reuse")
            self.assertIn("NNI_DISCOVERY=", (self.destination / "mismatch.stderr").read_text())
            self.assertEqual(report["processes"]["mismatch"]["returncode"], 0)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with self.view.lease():
                    pass

    def test_child_failure_retains_diagnostics_and_reacquires_the_lease(self):
        self.configuration.write_text("{")
        report = {}
        with ExitStack() as controller:
            controller.enter_context(self.view.lease())
            with self.assertRaisesRegex(RuntimeError, "failed with exit"):
                self.invoke(controller, "failure", report, "reuse")
            self.assertNotEqual(report["processes"]["failure"]["returncode"], 0)
            self.assertIn("JSONDecodeError", (self.destination / "failure.stderr").read_text())
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with self.view.lease():
                    pass


if __name__ == "__main__":
    unittest.main()
