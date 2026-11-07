import hashlib
from pathlib import Path
import stat
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView
from recording_audit import snapshot_unsupported


class UnsupportedSnapshotTests(unittest.TestCase):
    def test_sources_are_confined_bounded_and_archivable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            view = InputView.create(parent=root)
            source = view.root / "source.c"
            source.write_text("#include COMPUTED\n")
            source.chmod(0o600)
            external = root / "external.c"
            external.write_text("private external data")
            large = view.root / "large.c"
            with large.open("wb") as output:
                output.truncate(1024 * 1024 + 1)
            destination = root / "report"
            destination.mkdir()
            coverage = [{"unsupported_files": {str(path): "computed-dependency" for path in [source, external, large]}}]
            with view.lease():
                records = {record["path"]: record for record in snapshot_unsupported(view, coverage, destination)}
            copied = destination / records[str(source)]["snapshot"]
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(records[str(source)]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o644)
            self.assertIn("diagnostic_error", records[str(external)])
            self.assertEqual(records[str(large)]["omitted"], "source snapshot budget")
            self.assertEqual(len(list((destination / "unsupported").iterdir())), 1)
            view.close()


if __name__ == "__main__":
    unittest.main()
