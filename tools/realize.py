import argparse
import bz2
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time

import workloads
from planning import discovery_report


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


def invoke_planner(binary, view, target, env, destination, name, report, controller, mode=None, tool=None):
    command = [str(binary), "-C", str(view.root / "build"), "-j4"]
    if mode is not None:
        command += ["--input-view", str(view.marker), "--discovery-mode", mode,
                    "--discovery-cache", str(view.control / "discovery")]
        controller.close()
    if tool is not None:
        command += ["-t", tool]
    try:
        result = invoke([*command, target], env, destination, name, report)
    finally:
        if mode is not None:
            controller.enter_context(view.lease())
    if mode is not None:
        result["discovery"] = discovery_report((destination / f"{name}.stderr").read_text(), mode)
    return result


def plan_digest(destination, name):
    value = json.loads((destination / f"{name}.stdout").read_text())
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def realize(variant, destination, discovery_mode=None):
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    record = {
        "schema_version": 1, "passed": False,
        "stage": "full-target functional compatibility; no reference equivalence or performance claim",
        "variant": variant, "discovery_mode": discovery_mode, "machine": platform.platform(),
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
        with closing(view), ExitStack() as controller:
            controller.enter_context(view.lease())
            view.validate_links()
            capture = (view.control / "capture.json").read_bytes()
            (destination / "capture.json").write_bytes(capture)
            shutil.copyfile(view.control / "configure.log", destination / "configure.log")
            build = view.root / "build"

            def planner(name, mode, tool=None):
                return invoke_planner(binary, view, workload["target"], env, destination,
                                      name, record, controller, mode, tool)

            if discovery_mode is not None:
                cold = planner("cold-plan", discovery_mode, "drv")
                if cold["discovery"]["hits"]:
                    raise RuntimeError("a cold full-target plan unexpectedly reused discovery")
                if discovery_mode != "off" and not cold["discovery"]["recorded_actions"]:
                    raise RuntimeError("full-target planning did not produce eligible records")
                planner("fresh-plan", "off", "drv")
                record["plan_sha256"] = plan_digest(destination, "cold-plan")
                if plan_digest(destination, "fresh-plan") != record["plan_sha256"]:
                    raise RuntimeError("persistent full-target planning changed the fresh derivation")
            record["build"] = planner("build", discovery_mode)
            if discovery_mode in {"record", "reuse"}:
                counter = "hits" if discovery_mode == "reuse" else "validated_candidates"
                if not record["build"]["discovery"][counter]:
                    raise RuntimeError("full-target build did not exercise persisted discovery")
                planner("warm-plan", discovery_mode, "drv")
                if plan_digest(destination, "warm-plan") != record["plan_sha256"]:
                    raise RuntimeError("warm full-target planning changed the derivation")
                snapshot = destination / "discovery-state.json"
                shutil.copyfile(view.control / "discovery/state.json", snapshot)
                snapshot.chmod(0o644)
                record["state_sha256"] = digest(snapshot)
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
    parser.add_argument("--discovery-mode", choices=["off", "record", "reuse"])
    args = parser.parse_args()
    series = json.loads((ROOT / "patches/series.json").read_text())
    if args.variant not in series["variant_postimages"]:
        parser.error("variant needs declared postimages")
    if args.discovery_mode is not None and args.variant != "recording":
        parser.error("discovery modes require the recording variant")
    destination = ROOT / "results/realization" / f"nix-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    realize(args.variant, destination, args.discovery_mode)


if __name__ == "__main__":
    main()
