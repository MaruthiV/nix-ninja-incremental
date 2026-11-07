import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import oracle


class CompilerOracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nni-oracle-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.compiler = os.environ.get("CC", "cc")

    def reference(self):
        return oracle.compiler_reference([
            self.compiler, "-nostdinc", "-c", "main.c", "-o", "original.o",
        ], self.directory)

    def test_real_compiler_escaping_and_fresh_artifacts(self):
        (self.directory / "main.c").write_text(
            '#include "value with space#$.h"\nint value(void) { return VALUE; }\n'
        )
        (self.directory / "value with space#$.h").write_text("#define VALUE 7\n")
        result = self.reference()
        self.assertEqual(result["discovery"]["returncode"], 0)
        self.assertEqual(set(result["discovery"]["dependencies"]), {
            str(self.directory / "main.c"), str(self.directory / "value with space#$.h"),
        })
        self.assertEqual(result["compilation"]["returncode"], 0)
        self.assertGreater(result["compilation"]["artifact_bytes"], 0)
        self.assertFalse((self.directory / "original.o").exists())

    def test_content_changes_with_unchanged_mtime_change_reference(self):
        (self.directory / "main.c").write_text(
            '#include "value.h"\nint value(void) { return VALUE; }\n'
        )
        header = self.directory / "value.h"
        header.write_text("#define VALUE 7\n")
        first = self.reference()
        times = header.stat()
        header.write_text("#define VALUE 9\n")
        os.utime(header, ns=(times.st_atime_ns, times.st_mtime_ns))
        second = self.reference()
        self.assertNotEqual(
            first["compilation"]["artifact_sha256"], second["compilation"]["artifact_sha256"],
        )

    def test_valid_preprocessing_cannot_hide_failed_compilation(self):
        source = self.directory / "main.c"
        source.write_text("int value(void) { return 7; }\n")
        self.assertEqual(self.reference()["compilation"]["returncode"], 0)
        source.write_text("this is invalid c\n")
        result = self.reference()
        self.assertEqual(result["discovery"]["returncode"], 0)
        self.assertNotEqual(result["compilation"]["returncode"], 0)
        self.assertNotIn("artifact_sha256", result["compilation"])

    def test_missing_header_is_a_reference_failure(self):
        (self.directory / "main.c").write_text('#include "missing.h"\n')
        result = self.reference()
        self.assertNotEqual(result["discovery"]["returncode"], 0)
        self.assertNotEqual(result["compilation"]["returncode"], 0)
        self.assertNotIn("artifact_sha256", result["compilation"])

    def test_fixture_oracle_rejects_injected_stale_dependencies(self):
        fixture = json.loads((oracle.ROOT / "tests/fixtures/scanner.json").read_text())[0]
        original_capture = oracle.capture
        scanner = Path("/injected/scanner")

        def stale_capture(argv, directory):
            if argv[0] != str(scanner):
                return original_capture(argv, directory)
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "schema_version": 1,
                    "actions": [{"dependencies": [str(directory / "main.c")]}],
                }),
            }

        with patch.object(oracle, "capture", side_effect=stale_capture):
            result = oracle.check_fixture(fixture, scanner, self.compiler, self.directory)
        self.assertFalse(result["passed"])
        self.assertTrue(result["oracle_matches_fixture"])
        self.assertEqual(result["comparison"]["missing"], [str(self.directory / "literal/value.h")])


if __name__ == "__main__":
    unittest.main()
