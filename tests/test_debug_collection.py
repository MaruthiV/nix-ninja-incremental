import copy
import unittest

import debug_collection_checks as checks


class DebugCollectionTests(unittest.TestCase):
    def rows(self):
        first = {"name": "initial", "source_sha256": "source", "graph_sha256": "graph", "plan_sha256": "plan",
                 "debug_info_present": True, "discovery": {"calls": 1, "scans": 1, "hits": 0,
                                                          "recorded_actions": 1, "namespace": "namespace"},
                 "artifacts": {name: {"path": "/nix/store/same-" + name, "sha256": name} for name in ["app", "main.o"]}}
        recovered = copy.deepcopy(first)
        recovered.update(name="recovered", discovery={"calls": 1, "hits": 1, "scans": 0, "namespace": "namespace"})
        return [first, recovered]

    def test_rebuild_checks_bytes_store_identity_plan_and_actual_discovery_hits(self):
        checks.verify_rebuild(self.rows())
        for change in ["bytes", "path", "plan", "source", "graph", "debug", "hits", "namespace", "cold", "coverage"]:
            rows = self.rows()
            if change in {"bytes", "path"}:
                rows[1]["artifacts"]["main.o"]["sha256" if change == "bytes" else "path"] = "different"
            elif change in {"plan", "source", "graph"}:
                rows[1][change + "_sha256"] = "different"
            elif change == "debug":
                rows[1]["debug_info_present"] = False
            elif change == "hits":
                rows[1]["discovery"]["hits"] = 0
            elif change == "namespace":
                rows[1]["discovery"]["namespace"] = "different"
            elif change == "cold":
                rows[0]["discovery"]["hits"] = 1
            else:
                rows.pop()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                checks.verify_rebuild(rows)

    def test_final_cleanup_recognizes_traces_from_already_collected_owned_paths(self):
        base = "/nix/store/" + "a" * 32 + "-base"
        derivation = "/nix/store/" + "b" * 32 + "-fixture.drv"
        output = "/nix/store/" + "c" * 32 + "-fixture"
        key = derivation + "^out"
        traces = {key: {"output_path": output, "signatures": ""}}
        self.assertEqual(checks.cleanup_trace_keys({base}, {base}, {derivation, output}, {}, traces), [key])
        with self.assertRaisesRegex(RuntimeError, "protected or unknown"):
            checks.cleanup_trace_keys({base}, {base}, set(), {}, traces)
        with self.assertRaisesRegex(RuntimeError, "logical store|outside"):
            checks.cleanup_trace_keys({base}, {base}, {"/tmp/unrelated"}, {}, traces)

    def test_cleanup_rejects_missing_base_paths_or_changed_protected_traces(self):
        base = "/nix/store/" + "a" * 32 + "-base"
        key = "/nix/store/" + "b" * 32 + "-protected.drv^out"
        protected = {key: {"output_path": base, "signatures": ""}}
        with self.assertRaisesRegex(RuntimeError, "protected store inventory"):
            checks.cleanup_trace_keys({base}, set(), set(), protected, protected)
        with self.assertRaisesRegex(RuntimeError, "protected build-trace map"):
            checks.cleanup_trace_keys({base}, {base}, set(), protected, {})


if __name__ == "__main__":
    unittest.main()
