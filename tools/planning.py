import argparse
from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import tempfile
import time

from input_view import InputView


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resources():
    root = Path("/sys/fs/cgroup")
    return {name: (root / name).read_text() for name in ["pids.events", "memory.events"]}


def discovery_report(stderr, mode):
    reports = [json.loads(line.removeprefix("NNI_DISCOVERY=")) for line in stderr.splitlines()
               if line.startswith("NNI_DISCOVERY=")]
    if len(reports) != 1:
        raise RuntimeError("expected one discovery report from the planner")
    report = reports[0]
    if report["schema_version"] != 1 or report["mode"] != mode or not report["planning_complete"]:
        raise RuntimeError("incomplete or incompatible discovery report")
    if report["hits"] or report["candidate_mismatches"] or report["validation_errors"]:
        raise RuntimeError("record-only discovery audit failed")
    if mode == "record" and (report["namespace"] is None or report["publication"] != "published"):
        raise RuntimeError("discovery recording did not publish state")
    return report


def measure(args):
    view = InputView(args.view)
    destination = args.output.resolve()
    if destination.is_relative_to(view.root):
        raise ValueError("measurement records must be outside build inputs")
    destination.mkdir(parents=True, exist_ok=False)
    modes = {}
    for profile, specifications in [(False, args.binary), (True, args.profile)]:
        for specification in specifications:
            name, file = specification.split("=", 1)
            if not name or name in modes or not name.replace("-", "").isalnum():
                raise ValueError("mode names must be unique letters, digits or hyphens")
            binary = Path(file).resolve(strict=True)
            modes[name] = {"binary": str(binary), "sha256": sha256(binary), "profile": profile}
    if not modes:
        raise ValueError("at least one --binary or --profile mode is required")
    configured = set()
    for specification in args.discovery:
        name, mode = specification.split("=", 1)
        if name not in modes or name in configured or mode not in {"off", "record"}:
            raise ValueError("discovery modes must uniquely name a binary and use off or record")
        modes[name]["discovery"] = mode
        configured.add(name)
    cache_root = Path(tempfile.mkdtemp(prefix="planning-state-", dir=view.control)) if configured else None
    capture = json.loads((view.control / "capture.json").read_text())
    if not capture.get("passed"):
        raise ValueError("input-view configuration did not pass")
    workload = view.data["provenance"]["workload"]
    report = {
        "schema_version": 1, "stage": "diagnostic planning only",
        "view": view.data, "capture": capture, "modes": modes,
        "jobs": args.jobs, "repetitions": args.repetitions, "seed": args.seed,
        "pins": json.loads((ROOT / "environment/pins.json").read_text()),
        "patch_series": json.loads((ROOT / "patches/series.json").read_text()),
        "planner_cache": "record-only; every action scans fresh" if configured else "absent",
        "cache_root": str(cache_root) if cache_root is not None else None,
        "store_state": "uncontrolled before warmups; warmups retained",
        "machine": subprocess.check_output(["uname", "-sm"], text=True).strip(),
        "runs": [], "passed": False,
    }
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    expected = None

    controller = ExitStack()

    def invoke(name, round_number, warmup=False):
        nonlocal expected
        mode = modes[name]
        stem = f"{'warmup' if warmup else round_number}-{name}"
        run_env = dict(env)
        run_env["PATH"] = str(Path(mode["binary"]).parent) + os.pathsep + env["PATH"]
        phases = destination / f"{stem}-phases.json"
        if mode["profile"]:
            run_env["NIX_NINJA_PROFILE"] = str(phases)
        argv = [mode["binary"], "-C", str(view.root / "build"), f"-j{args.jobs}", "-t", "drv"]
        discovery = mode.get("discovery")
        if discovery is not None:
            argv += ["--input-view", str(view.marker), "--discovery-mode", discovery,
                     "--discovery-cache", str(cache_root / name)]
        argv.append(workload["target"])
        before = resources()
        if discovery is not None:
            controller.close()
        start = time.monotonic_ns()
        try:
            process = subprocess.run(argv, env=run_env, text=True, capture_output=True)
            elapsed = time.monotonic_ns() - start
        finally:
            if discovery is not None:
                controller.enter_context(view.lease())
        record = {
            "mode": name, "round": round_number, "warmup": warmup,
            "argv": argv, "returncode": process.returncode, "elapsed_ns": elapsed,
            "resources_before": before, "resources_after": resources(),
        }
        report["runs"].append(record)
        (destination / f"{stem}-stdout.json").write_text(process.stdout)
        (destination / f"{stem}-stderr.log").write_text(process.stderr)
        if process.returncode:
            raise RuntimeError(f"planning failed: {stem}")
        if discovery is not None:
            record["discovery"] = discovery_report(process.stderr, discovery)
            if discovery == "record" and not warmup:
                initial = next(run["discovery"] for run in report["runs"]
                               if run["mode"] == name and run["warmup"])
                if initial["recorded_actions"] and not record["discovery"]["validated_candidates"]:
                    raise RuntimeError(f"warm planning did not validate persisted records: {stem}")
        value = json.dumps(json.loads(process.stdout), sort_keys=True, separators=(",", ":")).encode()
        record["derivation_sha256"] = hashlib.sha256(value).hexdigest()
        if expected is None:
            expected = record["derivation_sha256"]
        if record["derivation_sha256"] != expected:
            raise RuntimeError(f"derivation mismatch: {stem}")
        if mode["profile"]:
            record["phases"] = json.loads(phases.read_text())
        print(f"{stem}: {elapsed / 1e9:.6f}s, matching derivation", flush=True)

    try:
        with controller:
            controller.enter_context(view.lease())
            start = time.monotonic_ns()
            view.validate_links()
            report["view_verification_ns"] = time.monotonic_ns() - start
            for name in modes:
                invoke(name, -1, warmup=True)
            rng = random.Random(args.seed)
            for round_number in range(args.repetitions):
                order = list(modes)
                rng.shuffle(order)
                for name in order:
                    invoke(name, round_number)
        samples = defaultdict(list)
        for record in report["runs"]:
            if not record["warmup"]:
                samples[record["mode"]].append(record["elapsed_ns"])
        report["median_ns"] = {name: statistics.median(values) for name, values in samples.items()}
        report["passed"] = True
    except (OSError, ValueError, RuntimeError) as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="Paired planning measurements in an owned stable input view")
    parser.add_argument("--view", required=True, type=Path)
    parser.add_argument("--binary", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--profile", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--discovery", action="append", default=[], metavar="NAME=off|record")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=2027)
    parser.add_argument("--output", type=Path, default=ROOT / "results/baseline" / f"planning-{time.time_ns()}")
    args = parser.parse_args()
    if args.jobs < 1 or args.repetitions < 1:
        parser.error("jobs and repetitions must be positive")
    measure(args)
    print(args.output)


if __name__ == "__main__":
    main()
