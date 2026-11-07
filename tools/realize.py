import argparse
import bz2
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time

import workloads


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expression(source):
    def quote(path):
        return json.dumps(str(path)).replace("${", "\\${")

    return (
        f"(import (builtins.toPath {quote(ROOT / 'environment/baseline.nix')}) "
        f"{{ source = builtins.toPath {quote(source)}; cliRuntime = true; }}).nix-ninja"
    )


def invoke(argv, env, destination, name, report):
    start = time.monotonic_ns()
    record = {"argv": argv}
    report.setdefault("processes", {})[name] = record
    with (destination / f"{name}.stdout").open("x") as stdout, (destination / f"{name}.stderr").open("x") as stderr:
        process = subprocess.run(argv, env=env, text=True, stdout=stdout, stderr=stderr)
    record.update(returncode=process.returncode, elapsed_ns=time.monotonic_ns() - start)
    if process.returncode:
        raise RuntimeError(f"{name} failed with exit {process.returncode}; see {destination}")
    return record


def recent_logs(destination, since):
    retained = []
    total = 0
    target = destination / "derivation-logs"
    target.mkdir()
    for path in sorted(Path("/nix/var/log/nix/drvs").glob("*/*")):
        if path.stat().st_mtime < since:
            continue
        data = path.read_bytes()
        if path.suffix == ".bz2":
            data = bz2.decompress(data)
        if total + len(data) > 64 * 1024 * 1024:
            retained.append({"truncated": "64 MiB diagnostic budget"})
            break
        name = path.parent.name + path.name.removesuffix(".bz2")
        (target / (name + ".log")).write_bytes(data)
        retained.append({"derivation": name, "bytes": len(data), "selection": "modified during this run"})
        total += len(data)
    (destination / "derivation-log-index.json").write_text(json.dumps(retained, indent=2) + "\n")


def snapshot_variant(variant, destination):
    series = json.loads((ROOT / "patches/series.json").read_text())
    source = ROOT / ".work" / f"nix-ninja-{variant}"
    expected = series["variant_postimages"][variant]
    actual = {name: digest(source / name) for name in expected}
    if actual != expected:
        raise ValueError("variant source does not match its declared postimages")
    patches = destination / "patches"
    patches.mkdir()
    for name in series["variants"][variant]:
        data = (ROOT / "patches" / name).read_bytes()
        entry = next(entry for entry in series["patches"] if entry["file"] == name)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"patch checksum mismatch: {name}")
        (patches / name).write_bytes(data)
    (destination / "patch-series.json").write_text(json.dumps(series, indent=2) + "\n")
    return source, actual


def realize(variant, destination):
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    record = {
        "schema_version": 1, "passed": False,
        "stage": "full-target functional compatibility; no reference equivalence or performance claim",
        "variant": variant, "machine": platform.platform(),
    }
    since = time.time()
    try:
        source, actual = snapshot_variant(variant, destination)
        record.update(postimages=actual, pins=json.loads((ROOT / "environment/pins.json").read_text()))
        record["package"] = invoke([
            "nix", "build", "--impure", "--expr", expression(source), "--no-link", "--print-out-paths",
        ], env, destination, "package", record)
        paths = (destination / "package.stdout").read_text().strip().splitlines()
        if len(paths) != 1 or not paths[0].startswith("/nix/store/"):
            raise ValueError("expected one CLI package in the Nix store")
        binary = Path(paths[0]) / "bin/nix-ninja"
        record.update(binary=str(binary), binary_sha256=digest(binary))
        env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
        workload = next(value for value in workloads.WORKLOADS if value["name"] == "nix")
        view = workloads.capture(workload)
        record["input_view"] = view.data
        with closing(view), view.lease():
            view.validate_links()
            capture = (view.control / "capture.json").read_bytes()
            (destination / "capture.json").write_bytes(capture)
            shutil.copyfile(view.control / "configure.log", destination / "configure.log")
            build = view.root / "build"
            record["build"] = invoke([
                str(binary), "-C", str(build), "-j4", workload["target"],
            ], env, destination, "build", record)
            target = build / workload["target"]
            record.update(artifact=str(target.resolve(strict=True)), artifact_sha256=digest(target))
            checks = [
                [str(target), "--version"],
                [str(target), "--extra-experimental-features", "nix-command", "eval", "--offline", "--expr", "1 + 1"],
                ["patchelf", "--print-rpath", str(target)],
            ]
            record["checks"] = []
            for index, command in enumerate(checks):
                record["checks"].append(invoke(command, env, destination, f"probe-{index}", record))
            if (destination / "probe-1.stdout").read_text().strip() != "2":
                raise RuntimeError("the built Nix binary returned an incorrect evaluation")
            record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        try:
            recent_logs(destination, since)
        except (OSError, EOFError) as error:
            record["diagnostic_error"] = str(error)
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Pinned full Nix target compatibility check")
    parser.add_argument("--variant", default="aliases")
    args = parser.parse_args()
    series = json.loads((ROOT / "patches/series.json").read_text())
    if args.variant not in series["variant_postimages"]:
        parser.error("variant needs declared postimages")
    destination = ROOT / "results/realization" / f"nix-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    realize(args.variant, destination)


if __name__ == "__main__":
    main()
