from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
from cold_trace_checks import nix_program


class ProbeProgramPathTests(unittest.TestCase):
    def test_store_context_keeps_the_multicall_entry_point(self):
        package = "/nix/store/" + "a" * 32 + "-coreutils"
        with patch.object(Path, "resolve", side_effect=AssertionError("the entry point must not be canonicalized")):
            expression = nix_program(package + "/bin/od")
        self.assertEqual(expression, f'(builtins.storePath "{package}") + "/bin/od"')
        self.assertNotIn("coreutils/bin/coreutils", expression)

    def test_external_or_escaping_program_paths_are_rejected(self):
        package = "/nix/store/" + "a" * 32 + "-coreutils"
        for path in ["/usr/bin/od", package, package + "/../outside/bin/od"]:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                nix_program(path)


if __name__ == "__main__":
    unittest.main()
