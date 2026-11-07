import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import time

from input_view import InputView


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resources():
    root = Path("/sys/fs/cgroup")
    return {name: (root / name).read_text() for name in ["pids.events", "memory.events"]}


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
        "planner_cache": "absent", "store_state": "uncontrolled before warmups; warmups retained",
        "machine": subprocess.check_output(["uname", "-sm"], text=True).strip(),
        "runs": [], "passed": False,
    }
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    expected = None

    def invoke(name, round_number, warmup=False):
        nonlocal expected
        mode = modes[name]
        stem = f"{'warmup' if warmup else round_number}-{name}"
        run_env = dict(env)
        run_env["PATH"] = str(Path(mode["binary"]).parent) + os.pathsep + env["PATH"]
        phases = destination / f"{stem}-phases.json"
        if mode["profile"]:
            run_env["NIX_NINJA_PROFILE"] = str(phases)
        argv = [mode["binary"], "-C", str(view.root / "build"), f"-j{args.jobs}", "-t", "drv", workload["target"]]
        before = resources()
        start = time.monotonic_ns()
        process = subprocess.run(argv, env=run_env, text=True, capture_output=True)
        elapsed = time.monotonic_ns() - start
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
        with view.lease():
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


def main():
    parser = argparse.ArgumentParser(description="Paired planning measurements in an owned stable input view")
    parser.add_argument("--view", required=True, type=Path)
    parser.add_argument("--binary", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--profile", action="append", default=[], metavar="NAME=PATH")
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
