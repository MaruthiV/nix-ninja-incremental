import argparse
import json
from pathlib import Path
import subprocess
import sys

import dev
import realize
import variants


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".work/nix-ninja-recording-profile"
MANIFEST = ROOT / "benchmarks/operations/manifest.json"


def configuration():
    data = json.loads(MANIFEST.read_text())
    patch = data["patch"]
    file = Path(patch["file"])
    if data["schema_version"] != 1 or data["base_variant"] != "recording" or file.name != str(file):
        raise ValueError("unsupported operation-profile manifest")
    if realize.digest(ROOT / "patches/series.json") != data["base_series_sha256"]:
        raise ValueError("the frozen base patch registry changed")
    patch_path = MANIFEST.parent / file
    if realize.digest(patch_path) != patch["sha256"]:
        raise ValueError("diagnostic patch checksum differs")
    expected = dict(variants.SERIES["variant_postimages"]["recording"])
    expected.update(patch["postimage_sha256"])
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in expected):
        raise ValueError("diagnostic source path escapes its worktree")
    return data, patch_path, expected


def verify_source(source):
    data, _, expected = configuration()
    variants.verify(source, [{"postimage_sha256": expected}], "operation-profile")
    return data, expected


def prepare(source=SOURCE):
    source = source.resolve()
    protected = {dev.SOURCE.resolve(), *(ROOT / ".work" / f"nix-ninja-{name}" for name in variants.SERIES["variants"])}
    if not source.is_relative_to(ROOT / ".work") or source in protected:
        raise ValueError("operation profiling requires its own worktree under .work")
    _, patch_path, _ = configuration()
    if source.exists():
        try:
            verify_source(source)
            return source
        except ValueError:
            pass
    variants.prepare(source, variants.patch_set("recording"), "recording")
    dev.run(["git", "-C", str(source), "apply", "--check", str(patch_path)])
    dev.run(["git", "-C", str(source), "apply", str(patch_path)])
    verify_source(source)
    return source


def main():
    parser = argparse.ArgumentParser(description="Isolated diagnostic instrumentation on the frozen recording source")
    parser.add_argument("action", choices=["prepare", "check"])
    parser.add_argument("--directory", type=Path, default=SOURCE)
    args = parser.parse_args()
    if args.action == "prepare":
        source = prepare(args.directory)
    else:
        source = args.directory.resolve()
        verify_source(source)
    print(f"operation-profile source verified: {source}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"operation profile: {error}", file=sys.stderr)
        sys.exit(1)
