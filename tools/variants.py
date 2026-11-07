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
    return subprocess.check_output(["git", "-C", str(source), *arguments], text=True).strip()


def verify_base(source):
    if Path(output(source, ["rev-parse", "--show-toplevel"])).resolve() != source.resolve():
        raise ValueError("variant path is not its own worktree")
    if output(source, ["rev-parse", "HEAD"]) != SERIES["upstream_revision"]:
        raise ValueError("variant revision mismatch")


def verify(source, patches):
    verify_base(source)
    expected = {}
    for patch in patches:
        expected.update(patch["postimage_sha256"])
    tracked = output(source, ["diff", "HEAD", "--name-only"]).splitlines()
    untracked = output(source, ["ls-files", "--others", "--exclude-standard"]).splitlines()
    if set(tracked + untracked) != set(expected):
        raise ValueError("variant contains missing or unrelated changes; keep edits in a separate worktree")
    for name, digest in expected.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"variant content mismatch: {name}")


def prepare(source, patches):
    dev.prepare()
    if not source.exists():
        dev.run([
            "git", "-C", str(dev.SOURCE), "worktree", "add", "--detach",
            str(source), SERIES["upstream_revision"],
        ])
    verify_base(source)
    if not output(source, ["status", "--porcelain"]):
        files = [str(ROOT / "patches" / patch["file"]) for patch in patches]
        dev.run(["git", "-C", str(source), "apply", "--check", *files])
        dev.run(["git", "-C", str(source), "apply", *files])
    verify(source, patches)


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
        prepare(source, patches)
    else:
        verify(source, patches)
    print(f"{args.variant} verified: {source}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
