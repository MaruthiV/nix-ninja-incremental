import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import dev
import variants


class RelocatedVariantTests(unittest.TestCase):
    def setUp(self):
        if not (dev.SOURCE / ".git").is_dir():
            self.skipTest("prepare the pinned upstream source before Git integration checks")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        original = self.root / "host"
        base = original / ".work/nix-ninja"
        source = original / ".work/nix-ninja-test"
        base.parent.mkdir(parents=True)
        self.run_git(["clone", "--quiet", "--shared", "--no-checkout", str(dev.SOURCE), str(base)])
        self.run_git(["-C", str(base), "worktree", "add", "--quiet", "--detach", str(source), variants.SERIES["upstream_revision"]])
        readme = source / "README.md"
        readme.write_bytes(readme.read_bytes() + b"\nportable source fixture\n")
        (source / "portable-check.txt").write_text("expected new input\n")
        self.expected = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                         for name in ["README.md", "portable-check.txt"]}
        moved = self.root / "container"
        original.rename(moved)
        self.source = moved / ".work/nix-ninja-test"
        self.base = moved / ".work/nix-ninja"
        self.admin = self.base / ".git/worktrees/nix-ninja-test"
        self.pointer = (self.source / ".git").read_bytes()
        self.backlink = (self.admin / "gitdir").read_bytes()
        self.patches = [patch.object(variants, "ROOT", moved), patch.object(dev, "SOURCE", self.base)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_git(self, argv):
        subprocess.run(["git", *argv], check=True, text=True, capture_output=True)

    def verify(self):
        variants.verify(self.source, [{"postimage_sha256": self.expected}], "portable-check")

    def test_relocated_worktree_preserves_pin_and_diff_checks_without_rewriting_metadata(self):
        plain = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True, capture_output=True)
        self.assertNotEqual(plain.returncode, 0)
        self.verify()
        self.assertEqual((self.source / ".git").read_bytes(), self.pointer)
        self.assertEqual((self.admin / "gitdir").read_bytes(), self.backlink)

    def test_relocation_keeps_content_hash_checks(self):
        (self.source / "README.md").write_text("unexpected content\n")
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.verify()

    def test_relocation_keeps_unrelated_tracked_and_untracked_edit_rejection(self):
        for name in ["Cargo.toml", "unrelated.txt"]:
            path = self.source / name
            original = path.read_bytes() if path.exists() else None
            path.write_bytes((original or b"") + b"\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unrelated changes"):
                self.verify()
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_relocated_mount_with_different_ownership_uses_only_scoped_trust(self):
        with patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            self.verify()

    def test_relocation_rejects_an_admin_directory_bound_to_another_worktree(self):
        (self.admin / "gitdir").write_text(str(self.root / "foreign/.git") + "\n")
        with self.assertRaisesRegex(ValueError, "worktree metadata"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
