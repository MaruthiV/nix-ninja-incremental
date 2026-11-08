import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_oracle
import campaign_summary
import edit_traces


@unittest.skipUnless(shutil.which("ninja") and shutil.which("cc"), "Ninja and a C compiler are required")
class CampaignSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.states = edit_traces.generate(2027, 16)
        cls.fixture = edit_traces.fixture(cls.states, 2027)
        cls.references = edit_traces.check_reference(cls.fixture)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.output = self.root / "summary.json"
        self.write("seed-2027.json", self.fixture)
        self.write("seed-2027.states.json", self.states)
        pins = campaign_summary.read(build_oracle.ROOT / "environment/pins.json")
        series = campaign_summary.read(build_oracle.ROOT / "patches/series.json")
        for mode in campaign_summary.MODES:
            steps = []
            previous = None
            previous_source = None
            for index, (edit, reference, snapshot) in enumerate(zip(self.fixture["edits"], self.references, self.states, strict=True)):
                audit = self.audit(mode, warm=index > 0)
                actual = {"returncode": reference["returncode"], "stdout": "", "stderr": self.stderr(audit)}
                observation = {"class": "build-error"}
                if edit["expected_exit"] is not None:
                    observation = {"class": "success", "program_exit": reference["program_exit"],
                                   "sha256": reference["artifact_sha256"],
                                   "program": {"returncode": reference["program_exit"]}}
                step = {"name": edit["name"], "passed": True, "edit": edit,
                        "reference": reference, "actual": actual,
                        "reference_observation": observation, "actual_observation": copy.deepcopy(observation),
                        "discovery": audit}
                source = {name: {"sha256": hashlib.sha256(value.encode()).hexdigest(), "mtime_ns": index + 1}
                          for name, value in snapshot["files"].items()}
                for name, target in snapshot["symlink"].items():
                    source[name] = {"sha256": source[str(Path(name).parent / target)]["sha256"],
                                    "symlink": target, "mtime_ns": index + 1}
                for name in edit.get("preserve_mtime", []):
                    source[name]["mtime_ns"] = previous_source[name]["mtime_ns"]
                step["source"] = source
                previous_source = source
                if edit.get("stale_probe"):
                    step.update(stale_fault_detected=True, injected_stale_observation=copy.deepcopy(previous))
                if edit["expected_exit"] is None:
                    step["compiler_logs"] = [{"stdout": reference["stdout"] + reference["stderr"]}]
                else:
                    previous = observation
                    plan = json.dumps({"synthetic_plan": index}, sort_keys=True, separators=(",", ":"))
                    warm = self.audit(mode, warm=True)
                    step.update(warm_discovery=warm, plan_sha256=hashlib.sha256(plan.encode()).hexdigest(),
                                planning_probe={"returncode": 0, "stdout": plan, "stderr": self.stderr(warm)})
                steps.append(step)
            case = {"schema_version": 1, "passed": True, "discovery_mode": mode,
                    "binary_sha256": "a" * 64, "oracle_sha256": campaign_summary.digest(Path(build_oracle.__file__)),
                    "fixture_sha256": campaign_summary.digest(self.campaign / "seed-2027.json"),
                    "pins": pins, "patch_series": series, "steps": steps,
                    "compiler": {"returncode": 0, "stdout": "synthetic compiler\n"},
                    "ninja": {"returncode": 0, "stdout": "synthetic ninja\n"},
                    "daemon": {"returncode": 0, "stdout": json.dumps({"url": "daemon", "version": pins["runtime"]["version"]})}}
            self.write(f"seed-2027-{mode}/result.json", case)
        self.write("result.json", {"schema_version": 1, "passed": True,
                                  "stage": "seeded off/record/reuse artifact campaign",
                                  "test_fixture": "synthetic mode reports over fresh compiler observations",
                                  "modes": campaign_summary.MODES, "start_seed": 2027, "seeds": 1,
                                  "edits_per_seed": 16, "binary_sha256": "a" * 64,
                                  "generator_sha256": campaign_summary.digest(Path(edit_traces.__file__)),
                                  "completed": [{"seed": 2027, "states_per_mode": 17,
                                                 "faults_per_mode": sum(e.get("stale_probe", False) for e in self.fixture["edits"])}]})

    def write(self, name, value):
        path = self.campaign / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")

    @staticmethod
    def audit(mode, warm):
        return {"schema_version": 1, "mode": mode, "planning_complete": True,
                "calls": 2, "hits": 2 if mode == "reuse" and warm else 0,
                "scans": 0 if mode == "reuse" and warm else 2,
                "candidate_mismatches": 0, "validation_errors": 0, "bypasses": {},
                "namespace": "synthetic" if mode != "off" else None,
                "publication": "published" if mode != "off" else "disabled",
                "validated_candidates": 2 if mode == "record" and warm else 0}

    @staticmethod
    def stderr(audit):
        return "NNI_DISCOVERY=" + json.dumps(audit) + "\n"

    def summarize(self, inputs=None, seeds=1):
        return campaign_summary.summarize(inputs or [self.campaign], self.output, 2027, seeds, 16, "a" * 64)

    def test_complete_reports_preserve_provenance_and_exact_coverage(self):
        result = self.summarize()
        self.assertTrue(result["passed"])
        self.assertEqual(result["totals"]["states"], 51)
        self.assertEqual(result["tools"]["compiler"], "synthetic compiler")
        self.assertEqual(len(result["verified"][0]["cases"]), 3)

    def test_missing_seed_cannot_be_hidden_by_passing_present_reports(self):
        with self.assertRaisesRegex(ValueError, "missing expected seeds.*2028"):
            self.summarize(seeds=2)
        self.assertFalse(campaign_summary.read(self.output)["passed"])

    def test_duplicate_input_cannot_inflate_coverage(self):
        with self.assertRaisesRegex(ValueError, "duplicate campaign input"):
            self.summarize([self.campaign, self.campaign])

    def test_missing_mode_cannot_be_hidden_by_the_top_level_pass_flag(self):
        (self.campaign / "seed-2027-reuse/result.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.summarize()
        self.assertFalse(campaign_summary.read(self.output)["passed"])

    def test_a_self_consistent_but_different_mode_artifact_is_rejected(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        for key in ["actual_observation", "reference_observation"]:
            case["steps"][0][key]["sha256"] = "b" * 64
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "cross-mode artifacts or plans differ"):
            self.summarize()

    def test_warm_always_scan_reports_cannot_pass_as_reuse(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        step = case["steps"][0]
        step["warm_discovery"].update(hits=0, scans=2)
        step["planning_probe"]["stderr"] = self.stderr(step["warm_discovery"])
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "warm probe did not reuse"):
            self.summarize()

    def test_an_altered_raw_plan_cannot_keep_its_old_digest(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        case["steps"][0]["planning_probe"]["stdout"] = '{"different": true}'
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "raw plan digest differs"):
            self.summarize()

    def test_compiler_failure_requires_the_actual_diagnostic(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        step = next(step for step in case["steps"] if step["edit"]["expected_exit"] is None)
        step["compiler_logs"] = []
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "compiler error evidence is missing"):
            self.summarize()

    def test_mixed_binary_revisions_cannot_be_aggregated(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        case["binary_sha256"] = "b" * 64
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "code identity differs"):
            self.summarize()

    def test_unapplied_source_mutations_are_rejected(self):
        case = campaign_summary.read(self.campaign / "seed-2027-reuse/result.json")
        case["steps"][1]["source"] = case["steps"][0]["source"]
        self.write("seed-2027-reuse/result.json", case)
        with self.assertRaisesRegex(ValueError, "source bytes or aliases differ"):
            self.summarize()


if __name__ == "__main__":
    unittest.main()
