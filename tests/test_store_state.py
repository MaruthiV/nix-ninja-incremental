from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import store_state


class BuildTraceStateTests(unittest.TestCase):
    def setUp(self):
        self.base_drv = "/nix/store/" + "a" * 32 + "-base.drv"
        self.base_out = "/nix/store/" + "b" * 32 + "-base"
        self.owned_drv = "/nix/store/" + "c" * 32 + "-experiment.drv"
        self.owned_out = "/nix/store/" + "d" * 32 + "-experiment"
        self.base = {self.base_drv + "^out": {"output_path": self.base_out, "signatures": "trusted"}}
        self.owned = {self.owned_drv + "^out": {"output_path": self.owned_out, "signatures": ""}}
        self.paths = [self.base_drv, self.base_out]

    def test_schema_snapshot_reads_without_changing_the_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("create table BuildTraceV3 (drvPath text, outputName text, outputPath text, signatures text)")
                connection.execute("insert into BuildTraceV3 values (?, ?, ?, ?)",
                                   (Path(self.owned_drv).name, "out", self.owned_out, None))
                connection.commit()
            before = path.read_bytes()
            self.assertEqual(store_state.build_traces(path), self.owned)
            self.assertEqual(path.read_bytes(), before)

    def test_only_new_traces_tied_to_owned_paths_are_selected(self):
        current = self.base | self.owned
        selected = store_state.trace_deletion_set(self.base, current, self.paths, self.paths + [self.owned_drv, self.owned_out])
        self.assertEqual(selected, [self.owned_drv + "^out"])

    def test_a_changed_or_missing_protected_trace_is_rejected(self):
        for current in [{}, {self.base_drv + "^out": {"output_path": self.owned_out, "signatures": "trusted"}}]:
            with self.subTest(current=current), self.assertRaisesRegex(RuntimeError, "protected build-trace map changed"):
                store_state.trace_deletion_set(self.base, current, self.paths, self.paths)

    def test_a_trace_without_an_owned_derivation_or_output_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "protected or unknown"):
            store_state.trace_deletion_set({}, self.owned, [], [])

    def test_external_and_malformed_trace_keys_are_rejected(self):
        for key in ["/tmp/experiment.drv^out", self.owned_drv + "^bad/output", self.owned_out + "^out"]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                store_state.trace_deletion_set({}, {key: {"output_path": self.owned_out}}, [], [self.owned_out])

    def test_duplicate_or_invalid_database_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("create table BuildTraceV3 (drvPath text, outputName text, outputPath text, signatures text)")
                for _ in range(2):
                    connection.execute("insert into BuildTraceV3 values (?, ?, ?, ?)",
                                       (Path(self.owned_drv).name, "out", self.owned_out, None))
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                store_state.build_traces(path)

    def test_an_unknown_runtime_schema_is_not_treated_as_empty_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db.sqlite"
            sqlite3.connect(path).close()
            with self.assertRaises(sqlite3.OperationalError):
                store_state.build_traces(path)


if __name__ == "__main__":
    unittest.main()
