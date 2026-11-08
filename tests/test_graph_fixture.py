import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_oracle


class GraphFixtureTests(unittest.TestCase):
    def test_all_graph_edits_have_independent_fresh_artifacts_and_detect_skipped_semantic_changes(self):
        fixture = json.loads((build_oracle.ROOT / "tests/fixtures/graph_build_edits.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, build = root / "source", root / "build"
            source.mkdir()
            build.mkdir()
            build_oracle.mutate(source, {"write": fixture["files"]})
            (build / "build.ninja").write_text(fixture["build_ninja"])
            previous = None
            for edit in fixture["edits"]:
                with self.subTest(edit=edit["name"]):
                    build_oracle.mutate(source, edit)
                    if "build_ninja" in edit:
                        (build / "build.ninja").write_text(edit["build_ninja"])
                    for name in fixture["outputs"]:
                        (build / name).unlink(missing_ok=True)
                    result = build_oracle.invoke(["ninja", "-C", str(build), "-j1", fixture["target"]], dict(os.environ))
                    observed = build_oracle.observe(build, fixture["target"], result, dict(os.environ))
                    self.assertEqual(result["returncode"], 0, result["stdout"] + result["stderr"])
                    self.assertEqual(observed["program_exit"], edit["expected_exit"])
                    if edit.get("unchanged_artifact"):
                        self.assertEqual(observed["sha256"], previous["sha256"])
                    if edit.get("stale_probe"):
                        self.assertNotEqual(build_oracle.outcome(observed), build_oracle.outcome(previous))
                    previous = observed


if __name__ == "__main__":
    unittest.main()
