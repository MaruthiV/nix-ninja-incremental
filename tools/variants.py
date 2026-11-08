import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import dev


ROOT = Path(__file__).resolve().parents[1]
SERIES = json.loads((ROOT / "patches/series.json").read_text())


def patch_set(name):
    if SERIES["upstream_revision"] != dev.PINS["upstream"]["revision"]:
        raise ValueError("patch series and environment pins disagree")
    entries = {entry["file"]: entry for entry in SERIES["patches"]}
    patches = [entries[file] for file in SERIES["variants"][name]]
    for patch in patches:
        data = (ROOT / "patches" / patch["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != patch["sha256"]:
            raise ValueError(f"patch checksum mismatch: {patch['file']}")
    return patches


def output(source, arguments):
    source = source.resolve()
    command = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    metadata = source / ".git"
    if metadata.is_file() and source.is_relative_to(ROOT):
        value = metadata.read_text().strip()
        if not value.startswith("gitdir: ") or "\n" in value:
            raise ValueError("invalid linked worktree metadata")
        original = Path(value.removeprefix("gitdir: "))
        if original.is_absolute() and not original.exists() and original.parts[-3:-1] == (".git", "worktrees"):
            admin = dev.SOURCE / ".git/worktrees" / original.name
            if not admin.is_dir():
                raise ValueError("linked worktree metadata is unavailable at this mount")
            expected = (*source.relative_to(ROOT).parts, ".git")
            backlink = Path((admin / "gitdir").read_text().strip())
            common = (admin / (admin / "commondir").read_text().strip()).resolve()
            if backlink.parts[-len(expected):] != expected or common != (dev.SOURCE / ".git").resolve():
                raise ValueError("linked worktree metadata belongs to another source")
            command += [f"--git-dir={admin}", f"--work-tree={source}"]
    return subprocess.check_output([*command, *arguments], text=True).strip()


def verify_base(source):
    if Path(output(source, ["rev-parse", "--show-toplevel"])).resolve() != source.resolve():
        raise ValueError("variant path is not its own worktree")
    if output(source, ["rev-parse", "HEAD"]) != SERIES["upstream_revision"]:
        raise ValueError("variant revision mismatch")


def verify(source, patches, variant):
    verify_base(source)
    expected = {}
    for patch in patches:
        expected.update(patch["postimage_sha256"])
    expected = SERIES.get("variant_postimages", {}).get(variant, expected)
    tracked = output(source, ["diff", "HEAD", "--name-only"]).splitlines()
    untracked = output(source, ["ls-files", "--others", "--exclude-standard"]).splitlines()
    if set(tracked + untracked) != set(expected):
        raise ValueError("variant contains missing or unrelated changes; keep edits in a separate worktree")
    for name, digest in expected.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"variant content mismatch: {name}")


def prepare(source, patches, variant):
    dev.prepare()
    if not source.exists():
        dev.run([
            "git", "-C", str(dev.SOURCE), "worktree", "add", "--detach",
            str(source), SERIES["upstream_revision"],
        ])
    verify_base(source)
    if not output(source, ["status", "--porcelain"]):
        files = [str(ROOT / "patches" / patch["file"]) for patch in patches]
        for file in files:
            dev.run(["git", "-C", str(source), "apply", "--check", file])
            dev.run(["git", "-C", str(source), "apply", file])
    verify(source, patches, variant)


def main():
    parser = argparse.ArgumentParser(description="Pinned, reviewable nix-ninja patch variants")
    parser.add_argument("action", choices=["prepare", "check"])
    parser.add_argument("variant", choices=SERIES["variants"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    source = (args.directory or ROOT / ".work" / f"nix-ninja-{args.variant}").resolve()
    if not source.is_relative_to(ROOT / ".work") or source == dev.SOURCE.resolve():
        parser.error("variants need a separate worktree under .work/")
    patches = patch_set(args.variant)
    if args.action == "prepare":
        prepare(source, patches, args.variant)
    else:
        verify(source, patches, args.variant)
    print(f"{args.variant} verified: {source}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
