import copy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluate_operations as operations


class OperationProfileTests(unittest.TestCase):
    def profile(self):
        return {"schema_version": 1, "mode": "planning-profile", "build_directory": "/owned/build",
                "count_semantics": "planner API invocations including failures; not wire requests or daemon operations",
                "phase_semantics": "inclusive cumulative wall time; concurrent phases overlap",
                "phases": {"planning.total": {"calls": 1, "elapsed_ns": 10},
                           "store.add_derivation": {"calls": 7, "elapsed_ns": 20}}}

    def test_counts_allow_overlapping_duration_and_zero_absent_operations(self):
        counts = operations.profile_counts(self.profile(), Path("/owned/build"))
        self.assertEqual(counts["store.add_derivation"], 7)
        self.assertEqual(counts["store.add_text"], 0)

    def test_invalid_profiles_fail_instead_of_supplying_partial_counts(self):
        for change in ["negative", "bool", "unknown", "incomplete", "directory", "semantics"]:
            profile = self.profile()
            if change in {"negative", "bool"}:
                profile["phases"]["store.add_derivation"]["calls"] = -1 if change == "negative" else True
            elif change == "unknown":
                profile["phases"]["unknown.store"] = {"calls": 1, "elapsed_ns": 2}
            elif change == "incomplete":
                profile["phases"].pop("planning.total")
            elif change == "directory":
                profile["build_directory"] = "/different/build"
            else:
                profile["count_semantics"] = "wire requests"
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                operations.profile_counts(profile, Path("/owned/build"))

    def pair(self):
        rows, pair = {}, {"scenario": "implementation-edit", "measured": [], "warmups": []}
        for variant in ["frozen", "profiled"]:
            name = variant + "-warmup"
            rows[name] = {"variant": variant, "mode": "reuse", "discovery": {"hits": 0, "recorded_actions": 2}}
            pair["warmups"].append(name)
            for mode in ["off", "reuse"]:
                name = variant + "-" + mode
                rows[name] = {"variant": variant, "mode": mode, "plan_sha256": "same", "input_state": {"impl": "new"},
                              "discovery": {"calls": 2, "hits": int(mode == "reuse"), "scans": 2 - int(mode == "reuse"),
                                            "recorded_actions": 2 if mode == "reuse" else 0}}
                pair["measured"].append(name)
        return pair, rows

    def test_first_post_edit_pair_requires_equivalent_work_and_separate_warmups(self):
        pair, rows = self.pair()
        self.assertEqual(len(operations.validate_pair(pair, rows)), 4)
        for change in ["plan", "source", "audit", "warmup", "coverage"]:
            altered, values = copy.deepcopy(pair), copy.deepcopy(rows)
            if change == "plan":
                values["profiled-off"]["plan_sha256"] = "different"
            elif change == "source":
                values["profiled-off"]["input_state"] = {"impl": "old"}
            elif change == "audit":
                values["profiled-reuse"]["discovery"]["scans"] += 1
            elif change == "warmup":
                values["profiled-warmup"]["discovery"]["hits"] = 1
            else:
                altered["measured"].pop()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                operations.validate_pair(altered, values)


if __name__ == "__main__":
    unittest.main()
