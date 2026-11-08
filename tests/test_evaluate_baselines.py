import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluate_baselines


class BaselineComparisonTests(unittest.TestCase):
    def test_only_matching_successful_plans_enter_the_timing_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "probe.stdout").write_text(json.dumps({"same": True}))
            digest = evaluate_baselines.realize.plan_digest(root, "probe")
            row = {"name": "probe", "returncode": 0}
            self.assertTrue(evaluate_baselines.compatibility(row, root, digest)["comparable"])
            result = evaluate_baselines.compatibility(row, root, "different")
            self.assertFalse(result["comparable"])
            self.assertIn("different derivation", result["reason"])

    def test_resource_failures_and_invalid_plans_remain_visible_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = evaluate_baselines.compatibility({"name": "probe", "returncode": 1}, root, "reference")
            self.assertFalse(result["comparable"])
            self.assertEqual(result["returncode"], 1)
            (root / "probe.stdout").write_text("not a plan")
            result = evaluate_baselines.compatibility({"name": "probe", "returncode": 0}, root, "reference")
            self.assertFalse(result["comparable"])
            self.assertEqual(result["reason"], "invalid derivation output")

    def test_overhead_has_explicit_baseline_and_current_labels(self):
        result = evaluate_baselines.comparison_statistics([(100, 120)] * 10)
        self.assertAlmostEqual(result["median_current_overhead"], 0.2)
        self.assertEqual(result["median_current_extra_ns"], 20)
        self.assertEqual(result["median_baseline_ns"], 100)
        self.assertEqual(result["median_current_ns"], 120)
        self.assertAlmostEqual(result["overhead_bootstrap_95_interval"][0], 0.2)
        result = evaluate_baselines.comparison_statistics([(100, 80)] * 10)
        self.assertAlmostEqual(result["median_current_overhead"], -0.2)


if __name__ == "__main__":
    unittest.main()
