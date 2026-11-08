from contextlib import redirect_stdout
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluate_builds
import evaluation_summary
from input_view import InputView


class BuildEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.view = InputView.create(parent=self.root)
        self.addCleanup(self.view.close)
        self.source, self.build = self.view.root / "source", self.view.root / "build"
        self.source.mkdir()
        self.build.mkdir()
        (self.source / "impl.c").write_text("implementation\n")
        (self.source / "value.h").write_text("header\n")
        (self.build / "build.ninja").write_text("synthetic graph\n")
        self.workload = {"name": "zstd", "target": "app", "content_probes": {
            "implementation": "impl.c", "shared_header": "value.h", "append": "\n#define NNI_BASELINE_CONTENT_PROBE 1\n"}}
        self.destination = self.root / "results"
        self.destination.mkdir()
        self.base = "/nix/store/" + "a" * 32 + "-protected"
        self.created = "/nix/store/" + "b" * 32 + "-experiment"
        self.helper = "/nix/store/" + "c" * 32 + "-debug-task"
        self.store = {self.base}
        self.traces = {}
        self.trace_key = "/nix/store/" + "d" * 32 + "-synthetic.drv^out"
        self.observations = []
        self.collections = []
        self.original = self.fingerprints()

    def fingerprints(self):
        return {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest() for name in ["impl.c", "value.h"]}

    def command(self, argv, env, destination, name, report):
        stdout = ""
        if argv[:3] == ["nix", "path-info", "--all"]:
            stdout = json.dumps(sorted(self.store)) if "--json" in argv else "\n".join(sorted(self.store)) + "\n"
        elif argv[:4] == ["nix", "build", "--impure", "--expr"]:
            self.store.add(self.helper)
            stdout = self.helper + "\n"
        elif argv[:4] == ["nix", "store", "build-trace", "delete"]:
            for key in argv[6:]:
                del self.traces[key]
        elif argv[:3] == ["nix", "store", "delete"]:
            selected = set(argv[5:])
            self.assertNotIn(self.base, selected)
            self.collections.append(selected)
            self.store -= selected
        elif "probe-1" in name:
            stdout = "zstd roundtrip ok\n"
        (destination / f"{name}.stdout").write_text(stdout)
        (destination / f"{name}.stderr").write_text("")
        row = {"argv": argv, "returncode": 0, "elapsed_ns": 1}
        report.setdefault("processes", {})[name] = row
        return row

    def process(self, argv, env, destination, name):
        mode = argv[argv.index("--discovery-mode") + 1]
        cache = Path(argv[argv.index("--discovery-cache") + 1])
        current = self.fingerprints()
        previous = json.loads((cache / "state.json").read_text()) if (cache / "state.json").exists() else None
        with self.view.lease():
            pass
        self.observations.append({"name": name, "mode": mode, "current": current,
                                  "previous": previous, "store": set(self.store),
                                  "output_present": (self.build / "app").exists(), "helper_path": env["PATH"]})
        self.store.add(self.created)
        self.traces[self.trace_key] = {"output_path": self.created, "signatures": ""}
        hits = sum(previous.get(key) == value for key, value in current.items()) if previous and mode == "reuse" else 0
        if mode == "reuse":
            cache.mkdir(mode=0o700, exist_ok=True)
            (cache / "state.json").write_text(json.dumps(current))
        audit = {"schema_version": 1, "mode": mode, "planning_complete": True, "calls": 2,
                 "hits": hits, "scans": 2 - hits, "recorded_actions": 2 if mode == "reuse" else 0,
                 "candidate_mismatches": 0, "validation_errors": 0,
                 "namespace": "synthetic" if mode == "reuse" else None,
                 "publication": "published" if mode == "reuse" else "disabled"}
        if "-t" not in argv:
            (self.build / "app").write_text(json.dumps(current, sort_keys=True))
        (destination / f"{name}.stdout").write_text(json.dumps(current))
        (destination / f"{name}.stderr").write_text("NNI_DISCOVERY=" + json.dumps(audit) + "\n")
        return {"argv": argv, "returncode": 0, "elapsed_ns": 1}

    def evaluate(self, cold=False, runner=None, debug_paths=False):
        report = {}
        with patch.object(evaluate_builds, "require_disposable_store") as guard, \
                patch.object(evaluate_builds, "build_traces", side_effect=lambda: copy.deepcopy(self.traces)), \
                patch.object(evaluate_builds.realize, "invoke", side_effect=self.command), \
                patch.object(evaluate_builds.planning, "timed_process", side_effect=runner or self.process), \
                redirect_stdout(io.StringIO()):
            evaluate_builds.paired_builds(Path("/synthetic/planner"), self.workload, self.view,
                                         self.destination, report, 2, 2027, cold=cold, debug_paths=debug_paths)
        self.assertEqual(guard.call_count, int(cold))
        return report

    def test_warm_builds_use_pre_edit_records_and_matching_store_inventory(self):
        report = self.evaluate()
        self.assertEqual(len(report["pairs"]), 8)
        for pair in report["pairs"]:
            measured = [row for row in self.observations if row["name"] in pair["measured"]]
            self.assertEqual(measured[0]["store"], measured[1]["store"])
            self.assertTrue(all(not row["output_present"] for row in measured))
            reuse = next(row for row in measured if row["mode"] == "reuse")
            self.assertEqual(reuse["previous"], self.original)
            self.assertEqual(measured[0]["current"], measured[1]["current"])
            self.assertTrue(pair["passed"])
        self.assertEqual(self.fingerprints(), self.original)
        self.assertEqual(self.collections, [])

    def test_cold_pairs_reset_exact_inventory_outputs_and_metadata(self):
        report = self.evaluate(cold=True)
        measured = [row for row in self.observations if "-timed-" in row["name"] and "verify" not in row["name"]]
        self.assertEqual(len(measured), 4)
        for row in measured:
            self.assertEqual(row["store"], {self.base})
            self.assertIsNone(row["previous"])
            self.assertFalse(row["output_present"])
        self.assertEqual(self.collections, [{self.created}] * 4)
        self.assertEqual(self.store, {self.base})
        self.assertEqual(self.traces, {})
        self.assertTrue(all(pair["passed"] for pair in report["pairs"]))

    def test_mismatched_build_bytes_fail_and_restore_source_and_lease(self):
        def mismatch(argv, env, destination, name):
            row = self.process(argv, env, destination, name)
            if "-timed-reuse" in name and "verify" not in name:
                (self.build / "app").write_text("wrong artifact")
            return row

        with self.assertRaisesRegex(RuntimeError, "paired builds differ"):
            self.evaluate(runner=mismatch)
        self.assertEqual(self.fingerprints(), self.original)
        with self.view.lease():
            pass

    def test_store_selection_preserves_base_and_rejects_external_paths(self):
        self.assertEqual(evaluate_builds.collection_set([self.base], [self.base, self.created]), [self.created])
        for current in [[self.created], [self.base, "/tmp/owned"], [self.base, "/nix/store/bad"]]:
            with self.subTest(current=current), self.assertRaises(RuntimeError):
                evaluate_builds.collection_set([self.base], current)

    def test_object_retention_handles_pinned_directory_outputs_and_legacy_files(self):
        root = self.root / "object-output"
        target = root / "src/libcmd/libnixcmd.so.2.36.0.p/installable-derived-path.cc.o"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"object bytes")
        self.assertEqual(evaluate_builds.retained_object(root), target)
        self.assertEqual(evaluate_builds.retained_object(target), target)

    def test_object_retention_rejects_missing_objects_and_symlinked_ancestors(self):
        root = self.root / "object-output"
        root.mkdir()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            evaluate_builds.retained_object(root)
        (root / "src").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            evaluate_builds.retained_object(root)

    def test_mapped_helper_is_protected_before_both_cold_modes_and_keeps_the_frozen_cli(self):
        report = self.evaluate(cold=True, debug_paths=True)
        report["binary"] = "/synthetic/planner"
        for row in self.observations:
            self.assertEqual(row["helper_path"].split(":")[:2], [self.helper + "/bin", "/synthetic"])
            self.assertIn(self.helper, row["store"])
        self.assertTrue(all(self.helper not in selected for selected in self.collections))
        control = evaluation_summary.verified_cold_control(self.destination / "result.json", report, "mapped-debug-paths-v1")
        self.assertTrue(control.startswith(self.helper))
        with self.assertRaisesRegex(RuntimeError, "epochs differ"):
            evaluation_summary.verified_cold_control(self.destination / "result.json", report, "legacy")

    def test_mapped_helper_control_identity_or_path_changes_are_rejected(self):
        report = self.evaluate(cold=True, debug_paths=True)
        report["binary"] = "/synthetic/planner"
        for change in ["source", "path", "command"]:
            data = copy.deepcopy(report)
            if change == "source":
                data["task_control"]["sources"]["benchmarks/cold-debug/task-flags.sh"] = "changed"
            elif change == "path":
                data["task_control"]["helper_path"] = "/wrong/bin:" + data["task_control"]["helper_path"]
            else:
                data["processes"]["debug-path-helper"]["argv"] += ["--different"]
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                evaluation_summary.verified_cold_control(self.destination / "result.json", data, "mapped-debug-paths-v1")

    def test_cold_helper_control_cannot_change_warm_evaluation(self):
        with self.assertRaisesRegex(ValueError, "restricted to cold"):
            self.evaluate(debug_paths=True)

    def verify_report(self, cold):
        report = self.evaluate(cold=cold)
        report["workload"] = self.workload
        kind = "cold" if cold else "warm"
        rows = evaluation_summary.verified_rows(self.destination / "result.json", report, kind)
        if cold:
            evaluation_summary.verified_store_resets(self.destination / "result.json", report)
        for pair in report["pairs"]:
            modes = evaluation_summary.verified_pair(pair, rows, report, kind)
            self.assertEqual(set(modes), {"off", "reuse"})

    def test_saved_warm_reports_are_compatible_with_raw_summary_checks(self):
        self.verify_report(False)

    def test_saved_cold_reports_are_compatible_with_raw_summary_checks(self):
        self.verify_report(True)

    def test_changed_retained_cold_artifact_cannot_pass_raw_verification(self):
        report = self.evaluate(cold=True)
        report["workload"] = self.workload
        row = next(row for row in report["runs"] if row["kind"] == "measured")
        with gzip.open(self.destination / row["saved_artifact"], "wb") as artifact:
            artifact.write(b"different retained bytes")
        with self.assertRaisesRegex(RuntimeError, "retained cold artifact bytes differ"):
            evaluation_summary.verified_rows(self.destination / "result.json", report, "cold")

    def test_retained_object_bytes_are_verified_independently_of_the_root_executable(self):
        report = self.evaluate(cold=True)
        report["workload"] = self.workload
        row = next(row for row in report["runs"] if row["kind"] == "measured")
        output = "/nix/store/" + "b" * 32 + "-ninja-build-installable-derived-path.cc.o"
        file = row["name"] + "-object-probe-" + "b" * 32 + ".artifact.gz"
        with gzip.open(self.destination / file, "wb") as artifact:
            artifact.write(b"actual object")
        row["object_probes"] = [{"store_output": output, "path": output, "saved_artifact": file,
                                 "sha256": hashlib.sha256(b"actual object").hexdigest()}]
        evaluation_summary.verified_rows(self.destination / "result.json", report, "cold")
        with gzip.open(self.destination / file, "wb") as artifact:
            artifact.write(b"changed object")
        with self.assertRaisesRegex(RuntimeError, "retained cold object bytes differ"):
            evaluation_summary.verified_rows(self.destination / "result.json", report, "cold")

    def test_matching_path_inventories_cannot_hide_a_stale_build_trace(self):
        report = self.evaluate(cold=True)
        reset = next(reset for reset in report["store_resets"] if reset["trace_keys"])
        path = self.destination / f"{reset['name']}-traces-after.json"
        path.write_text(json.dumps({self.trace_key: {"output_path": self.created, "signatures": ""}}))
        with self.assertRaisesRegex(RuntimeError, "build-trace maps do not prove an exact reset"):
            evaluation_summary.verified_store_resets(self.destination / "result.json", report)

    def test_correct_trace_snapshots_cannot_hide_an_unscoped_cleanup_command(self):
        report = self.evaluate(cold=True)
        reset = next(reset for reset in report["store_resets"] if reset["trace_keys"])
        report["processes"][reset["name"] + "-delete-traces"]["argv"] += ["unrelated.drv^out"]
        with self.assertRaisesRegex(RuntimeError, "scoped supported API"):
            evaluation_summary.verified_store_resets(self.destination / "result.json", report)

    def test_an_asymmetric_warm_store_is_rejected(self):
        def asymmetric(argv, env, destination, name):
            row = self.process(argv, env, destination, name)
            if "-timed-" in name:
                self.store.add("/nix/store/" + "c" * 32 + "-unexpected")
            return row

        with self.assertRaisesRegex(RuntimeError, "different inventories"):
            self.evaluate(runner=asymmetric)


if __name__ == "__main__":
    unittest.main()
