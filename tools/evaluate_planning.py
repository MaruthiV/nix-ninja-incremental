import argparse
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import time

from planning import discovery_report
import realize
import workloads


ROOT = Path(__file__).resolve().parents[1]
CASES = ["no-op", "irrelevant-edit", "implementation-edit", "shared-header-edit"]
MODES = ["off", "reuse"]


def resources():
    root = Path("/sys/fs/cgroup")
    return {name: (root / name).read_text().strip() for name in [
        "cpu.max", "cpu.stat", "memory.current", "memory.peak", "memory.max", "memory.events",
        "pids.current", "pids.max", "pids.events",
    ]}


def timed_process(argv, env, destination, name):
    record = {"argv": argv, "resources_before": resources()}
    with (destination / f"{name}.stdout").open("x") as stdout, (destination / f"{name}.stderr").open("x") as stderr:
        start = time.monotonic_ns()
        with subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr) as child:
            _, status, usage = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status)
        elapsed = time.monotonic_ns() - start
    record.update(returncode=child.returncode, elapsed_ns=elapsed,
                  user_cpu_ns=round(usage.ru_utime * 1e9), system_cpu_ns=round(usage.ru_stime * 1e9),
                  max_rss_bytes=usage.ru_maxrss * (1024 if platform.system() == "Linux" else 1),
                  resources_after=resources())
    return record


class Edits:
    def __init__(self, source, probes):
        self.source = source
        self.append = probes["append"].encode()
        self.paths = {"implementation-edit": source / probes["implementation"],
                      "shared-header-edit": source / probes["shared_header"]}
        self.original = {}
        for path in self.paths.values():
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(source):
                raise ValueError("content probe must be an owned regular source file")
            metadata = path.stat()
            self.original[path] = (path.read_bytes(), metadata.st_atime_ns, metadata.st_mtime_ns)
        self.irrelevant = source / ".nni-irrelevant.txt"
        if self.irrelevant.exists() or self.irrelevant.is_symlink():
            raise ValueError("the irrelevant-edit probe already exists")

    def reset(self):
        for path, (data, atime, mtime) in self.original.items():
            path.write_bytes(data)
            os.utime(path, ns=(atime, mtime))
        self.irrelevant.unlink(missing_ok=True)

    def apply(self, case):
        if case in self.paths:
            path = self.paths[case]
            path.write_bytes(self.original[path][0] + self.append)
        elif case == "irrelevant-edit":
            self.irrelevant.write_text("not a build input\n")
        elif case != "no-op":
            raise ValueError(f"unknown scenario: {case}")

    def snapshot(self):
        paths = [*self.paths.values(), self.irrelevant]
        return {str(path.relative_to(self.source)): realize.digest(path) if path.exists() else None for path in paths}


def paired_plans(binary, workload, view, destination, report, rounds, seed):
    env = dict(os.environ, CCACHE_DISABLE="1", LC_ALL="C")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    env["NIX_CONFIG"] = env.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"
    report.update(runs=[], pairs=[], store_state="prewarmed union of declared planning states",
                  compiler_cache="disabled", page_cache="uncontrolled; no flush")
    with ExitStack() as controller:
        controller.enter_context(view.lease())
        view.validate_links()
        edits = Edits(view.root / "source", workload["content_probes"])
        report["original_source"] = edits.snapshot()
        state_root = view.control / "evaluation-state"
        state_root.mkdir(mode=0o700)

        def invoke(name, mode, cache, kind, case, round_number):
            argv = [str(binary), "-C", str(view.root / "build"), "-j4", "-t", "drv",
                    "--input-view", str(view.marker), "--discovery-mode", mode,
                    "--discovery-cache", str(cache), workload["target"]]
            row = {"name": name, "mode": mode, "kind": kind, "scenario": case,
                   "round": round_number, "input_state": edits.snapshot()}
            report["runs"].append(row)
            controller.close()
            try:
                row.update(timed_process(argv, env, destination, name))
            finally:
                controller.enter_context(view.lease())
            if row["returncode"]:
                raise RuntimeError(f"planning failed: {name}")
            row["discovery"] = discovery_report((destination / f"{name}.stderr").read_text(), mode)
            row["plan_sha256"] = realize.plan_digest(destination, name)
            if edits.snapshot() != row["input_state"]:
                raise RuntimeError(f"planning modified the owned source: {name}")
            return row

        try:
            for case in CASES:
                edits.reset()
                edits.apply(case)
                invoke(f"prewarm-{case}", "off", state_root / "prewarm", "store-warmup", case, -1)
            edits.reset()
            rng = random.Random(seed)
            for round_number in range(rounds):
                order = CASES.copy()
                rng.shuffle(order)
                for case in order:
                    pair = {"round": round_number, "scenario": case, "warmups": [], "measured": []}
                    report["pairs"].append(pair)
                    start = time.monotonic_ns()
                    edits.reset()
                    pair["reset_ns"] = time.monotonic_ns() - start
                    pair_root = state_root / f"{round_number}-{case}"
                    pair_root.mkdir(mode=0o700)
                    prefix = f"r{round_number}-{case}"
                    warmups = {}
                    for mode in MODES:
                        row = invoke(f"{prefix}-warmup-{mode}", mode, pair_root / mode,
                                     "metadata-warmup", "no-op", round_number)
                        warmups[mode] = row
                        pair["warmups"].append(row["name"])
                    base = warmups["reuse"]["discovery"]
                    if base["hits"] or not base["recorded_actions"]:
                        raise RuntimeError("metadata initialization was not a cold eligible plan")
                    start = time.monotonic_ns()
                    edits.apply(case)
                    pair["edit_ns"] = time.monotonic_ns() - start
                    measured = {}
                    modes = MODES.copy()
                    rng.shuffle(modes)
                    for mode in modes:
                        row = invoke(f"{prefix}-timed-{mode}", mode, pair_root / mode,
                                     "measured", case, round_number)
                        measured[mode] = row
                        pair["measured"].append(row["name"])
                    if measured["off"]["plan_sha256"] != measured["reuse"]["plan_sha256"]:
                        raise RuntimeError(f"paired derivations differ: {prefix}")
                    reuse = measured["reuse"]["discovery"]
                    if case in {"no-op", "irrelevant-edit"}:
                        if reuse["hits"] != base["recorded_actions"]:
                            raise RuntimeError(f"unchanged discovery did not reuse all eligible records: {prefix}")
                    elif reuse["hits"] >= base["recorded_actions"]:
                        raise RuntimeError(f"the first post-edit plan did not invalidate discovery: {prefix}")
                    metadata = pair_root / "reuse/state.json"
                    pair["metadata_bytes"] = metadata.stat().st_size
                    if len(report["pairs"]) in {1, rounds * len(CASES)}:
                        shutil.copyfile(metadata, destination / f"{prefix}-state.json")
                    start = time.monotonic_ns()
                    shutil.rmtree(pair_root)
                    pair["cleanup_ns"] = time.monotonic_ns() - start
                    pair["passed"] = True
                    print(f"{prefix}: matching first post-edit plans", flush=True)
        finally:
            edits.reset()


def evaluate(workload, destination, rounds, seed, expected_binary=None, driver=paired_plans,
             stage="frozen paired planning evaluation"):
    report = {"schema_version": 1, "stage": stage, "passed": False,
              "workload": workload, "rounds": rounds, "seed": seed,
              "harness_sha256": realize.digest(Path(__file__)), "memory_scope": "CLI/waited-child max RSS; cgroup values are boundary/cumulative context"}
    try:
        manifest = json.loads((ROOT / "results/setup/environment.json").read_text())
        current = resources()
        if (platform.system() != "Linux" or manifest["host"] != "Linux x86_64"
                or manifest["linux"] != "Linux x86_64" or manifest["cpu_quota"] != "400000 100000"
                or current["cpu.max"] != "400000 100000" or current["memory.max"] != "6442450944"
                or current["pids.max"] != "2048"):
            raise RuntimeError("final timing requires the declared native Linux machine and limits")
        pins = json.loads((ROOT / "environment/pins.json").read_text())
        if manifest["pins"] != pins or manifest["daemon"]["version"] != pins["runtime"]["version"]:
            raise RuntimeError("timing environment differs from the pinned runtime")
        report.update(environment=manifest, resources=current, machine=platform.platform(), pins=pins,
                      cpuinfo=(Path("/proc/cpuinfo")).read_text(),
                      patch_series=json.loads((ROOT / "patches/series.json").read_text()))
        source, postimages = realize.snapshot_variant("recording", destination)
        report["postimages"] = postimages
        env = dict(os.environ)
        for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
            env.pop(key, None)
        realize.invoke(["nix", "build", "--impure", "--expr", realize.expression(source),
                        "--no-link", "--print-out-paths"], env, destination, "package", report)
        paths = (destination / "package.stdout").read_text().strip().splitlines()
        if len(paths) != 1 or not paths[0].startswith("/nix/store/"):
            raise ValueError("expected one pinned CLI package")
        binary = Path(paths[0]) / "bin/nix-ninja"
        report.update(binary=str(binary), binary_sha256=realize.digest(binary))
        if expected_binary is not None and report["binary_sha256"] != expected_binary:
            raise RuntimeError("the release binary differs from the frozen implementation")
        start = time.monotonic_ns()
        with closing(workloads.capture(workload)) as view:
            report["capture_ns"] = time.monotonic_ns() - start
            report["input_view"] = view.data
            for name in ["capture.json", "configure.log"]:
                shutil.copyfile(view.control / name, destination / name)
            driver(binary, workload, view, destination, report, rounds, seed)
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Frozen paired planning measurements on the native protocol machine")
    parser.add_argument("workload", choices=[workload["name"] for workload in workloads.WORKLOADS])
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--seed", type=int, default=2027)
    parser.add_argument("--expected-binary-sha256")
    args = parser.parse_args()
    if not 10 <= args.rounds <= 50:
        parser.error("final evaluation requires between 10 and 50 paired rounds")
    workload = next(workload for workload in workloads.WORKLOADS if workload["name"] == args.workload)
    destination = ROOT / "results/evaluation" / f"planning-{args.workload}-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    evaluate(workload, destination, args.rounds, args.seed, args.expected_binary_sha256)


if __name__ == "__main__":
    main()
