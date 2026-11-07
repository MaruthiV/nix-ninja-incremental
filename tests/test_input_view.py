import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from input_view import InputView


class InputViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nni-view-test-")
        self.addCleanup(self.temporary.cleanup)
        self.view = InputView.create(self.temporary.name)
        self.addCleanup(self.view.close)

    def test_other_process_cannot_edit_a_leased_view(self):
        script = "from input_view import InputView; import sys; v=InputView(sys.argv[1]); exec('with v.lease():\\n pass')"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "tools"))
        with self.view.lease():
            result = subprocess.run([sys.executable, "-c", script, str(self.view.marker)], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("input view is busy", result.stderr)
        result = subprocess.run([sys.executable, "-c", script, str(self.view.marker)], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_close_cannot_remove_a_leased_view(self):
        with self.view.lease():
            with self.assertRaisesRegex(RuntimeError, "busy"):
                self.view.close()
            self.assertTrue(self.view.root.exists())

    def test_links_cannot_reach_live_external_inputs(self):
        outside = Path(self.temporary.name) / "outside.h"
        outside.write_text("#define VALUE 1\n")
        (self.view.root / "alias.h").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes stable roots"):
            self.view.validate_links()

    def test_relocated_descriptor_is_rejected(self):
        data = json.loads(self.view.marker.read_text())
        data["root"] = self.temporary.name
        self.view.marker.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "root does not match"):
            InputView(self.view.marker)


if __name__ == "__main__":
    unittest.main()
