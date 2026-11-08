import copy
from pathlib import Path
import shutil
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import edit_traces


class EditTraceTests(unittest.TestCase):
    def test_seed_and_snapshot_subset_preserve_reproducibility(self):
        states = edit_traces.generate(2027)
        self.assertEqual(states, edit_traces.generate(2027))
        self.assertNotEqual(states, edit_traces.generate(2028))
        self.assertEqual(len(states), 17)
        subset = edit_traces.fixture([states[0], states[7], states[-1]], 2027)
        self.assertEqual(len(subset["edits"]), 3)
        self.assertEqual(subset["edits"][-1]["expected_exit"], states[-1]["expected_exit"])

    def test_reducer_keeps_the_required_intermediate_state(self):
        states = edit_traces.generate(2027)
        essential = states[5]["name"]
        original = copy.deepcopy(states)
        reduced = edit_traces.minimize(states, lambda subset: any(state["name"] == essential for state in subset))
        self.assertEqual([state["name"] for state in reduced], [states[0]["name"], essential, states[-1]["name"]])
        self.assertEqual(states, original)

    @unittest.skipUnless(shutil.which("ninja") and shutil.which("cc"), "Ninja and a C compiler are required")
    def test_reference_uses_the_declared_compiler_diagnostic_file(self):
        value = edit_traces.fixture([edit_traces.generate(2027)[0]], 2027)
        value["edits"].append({"name": "invalid-main", "write": {"main.c": "int main(void) { return + ; }\n"},
                               "expected_exit": None, "diagnostic_file": "main.c"})
        observations = edit_traces.check_reference(value)
        self.assertEqual(len(observations), 2)
        self.assertNotEqual(observations[-1]["returncode"], 0)

    @unittest.skipUnless(shutil.which("ninja") and shutil.which("cc"), "Ninja and a C compiler are required")
    def test_eight_seeded_traces_agree_with_fresh_compilation(self):
        for seed in range(2027, 2035):
            with self.subTest(seed=seed):
                states = edit_traces.generate(seed)
                observations = edit_traces.check_reference(edit_traces.fixture(states, seed))
                self.assertEqual(len(observations), 17)
                self.assertTrue(any(record["returncode"] for record in observations))

    @unittest.skipUnless(shutil.which("ninja") and shutil.which("cc"), "Ninja and a C compiler are required")
    def test_reduced_snapshots_regenerate_a_valid_edit_sequence(self):
        states = edit_traces.generate(2031, 32)
        selected = [states[index] for index in [0, 6, 9, 10, 11, 12, 16, 32]]
        observations = edit_traces.check_reference(edit_traces.fixture(selected, 2031))
        self.assertEqual(len(observations), len(selected))


if __name__ == "__main__":
    unittest.main()
