import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import seeded_checks


class SeededReductionTests(unittest.TestCase):
    def test_reduction_preserves_the_artifact_failure_and_rejects_other_errors(self):
        states = seeded_checks.edit_traces.generate(2027)
        essential = states[5]["name"]
        error = "fresh-build mismatch: " + states[-1]["name"]
        failure = {"error": error, "steps": [{"name": state["name"]} for state in states]}

        def run(binary, fixture, destination, mode):
            names = [edit["name"] for edit in json.loads(fixture.read_text())["edits"]]
            report = {"passed": False, "error": error if essential in names else "unrelated setup failure"}
            (destination / "result.json").write_text(json.dumps(report))
            raise RuntimeError(report["error"])

        with tempfile.TemporaryDirectory() as temporary, patch.object(seeded_checks.build_oracle, "campaign", side_effect=run):
            destination = Path(temporary)
            report = seeded_checks.reduce_failure(Path("unused"), states, 2027, "record", failure, destination, 32)
            retained = json.loads((destination / "minimized.json").read_text())
            self.assertEqual([edit["name"] for edit in retained["edits"]], [states[0]["name"], essential, states[-1]["name"]])
            self.assertLessEqual(len(report["attempts"]), 32)
            observed = json.loads((Path(report["reproducing_trial"]) / "result.json").read_text())
            self.assertEqual(observed["error"], error)
            self.assertTrue(any(not attempt["reproduced"] for attempt in report["attempts"]))


if __name__ == "__main__":
    unittest.main()
