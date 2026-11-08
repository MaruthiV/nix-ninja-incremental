import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import evaluate_planning
from evaluation_summary import paired_statistics
import planning
import realize
import workloads


ROOT = Path(__file__).resolve().parents[1]


def verify_upstream(source, expected_revision):
    source = source.resolve(strict=True)
    git = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    revision = workloads.output([*git, "rev-parse", "HEAD"])
    if revision != expected_revision or workloads.output([*git, "status", "--porcelain"]):
        raise RuntimeError("unmodified upstream source does not match its clean pin")
    return revision


def comparison_statistics(pairs, current_mode="off"):
    result = paired_statistics(pairs)
    interval = result["paired_bootstrap_95_interval"]
    return {"pairs": result["pairs"], "current_mode": current_mode, "median_baseline_ns": result["median_off_ns"],
            "median_current_ns": result["median_reuse_ns"],
            "median_current_extra_ns": -result["median_paired_saved_ns"],
            "median_current_overhead": -result["median_paired_improvement"],
            "overhead_bootstrap_95_interval": [-interval[1], -interval[0]],
            "bootstrap_samples": result["bootstrap_samples"], "analysis_seed": result["analysis_seed"]}


def compatibility(row, destination, reference):
    if row["returncode"]:
        return {"comparable": False, "reason": "planning failed; no successful timing ratio", "returncode": row["returncode"]}
    try:
        digest = realize.plan_digest(destination, row["name"])
    except (ValueError, OSError) as error:
        return {"comparable": False, "reason": "invalid derivation output", "error": str(error)}
    return {"comparable": digest == reference, "plan_sha256": digest,
            "reason": "matching derivation" if digest == reference else "different derivation; no equivalent-plan timing ratio"}


def baseline_plans(binary, workload, view, destination, report, rounds, seed):
    os.environ["CCACHE_DISABLE"] = "1"
    os.environ["NIX_CONFIG"] = os.environ.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"
    env = dict(os.environ, LC_ALL="C")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    report.update(compiler_cache="disabled", page_cache="uncontrolled; no flush", scenario="no-op",
                  store_state="generated producers warmed before compatibility probes and retained warmups",
                  harness_sha256=realize.digest(Path(__file__)), baseline_binaries={})
    binaries = {"current": binary}
    upstream = ROOT / ".work/nix-ninja"
    revision = verify_upstream(upstream, report["pins"]["upstream"]["revision"])
    for name, source in [("upstream", upstream), ("corrected", ROOT / ".work/nix-ninja-corrected-embed")]:
        target = destination / name
        target.mkdir()
        if name == "corrected":
            _, images = realize.snapshot_variant("corrected-embed", target)
            report["baseline_binaries"][name] = {"postimages": images}
        else:
            report["baseline_binaries"][name] = {"revision": revision, "working_tree": "clean"}
        package = name + "-package"
        realize.invoke(["nix", "build", "--impure", "--expr", realize.expression(source), "--no-link", "--print-out-paths"],
                       env, target, package, report)
        paths = (target / f"{package}.stdout").read_text().strip().splitlines()
        if len(paths) != 1 or not paths[0].startswith("/nix/store/"):
            raise RuntimeError("baseline package did not produce one store output")
        binaries[name] = Path(paths[0]) / "bin/nix-ninja"
        report["baseline_binaries"][name].update(binary=str(binaries[name]), binary_sha256=realize.digest(binaries[name]))
    report["compatibility"] = {}
    reference = None
    active = {}
    with ExitStack() as controller:
        controller.enter_context(view.lease())
        view.validate_links()
        for name, executable in binaries.items():
            argv = [str(executable), "-C", str(view.root / "build"), "-j4", "-t", "drv"]
            if name == "current":
                argv += ["--input-view", str(view.marker), "--discovery-mode", "off",
                         "--discovery-cache", str(view.control / "baseline-current")]
            argv.append(workload["target"])
            name_env = dict(env, PATH=str(executable.parent) + os.pathsep + env["PATH"])
            if name == "current":
                controller.close()
            try:
                row = evaluate_planning.timed_process(argv, name_env, destination, f"compatibility-{name}")
            finally:
                if name == "current":
                    controller.enter_context(view.lease())
            row["name"] = f"compatibility-{name}"
            if name == "current":
                if row["returncode"]:
                    raise RuntimeError("current cache-off planning failed before the baseline comparison")
                reference = realize.plan_digest(destination, row["name"])
                row["discovery"] = planning.discovery_report((destination / f"{row['name']}.stderr").read_text(), "off")
            row.update(compatibility(row, destination, reference))
            report["compatibility"][name] = row
            if row["comparable"]:
                active[name] = executable
    if len(active) == 1:
        report["comparison_status"] = "no contextual baseline has matching derivations; raw probes retained, no equivalent-plan timing ratio"
        return
    baseline_names = [name for name in active if name != "current"]
    active["reuse"] = binary
    measurement = planning.measure(SimpleNamespace(view=view.marker, output=destination / "planning",
        binary=[f"{name}={path}" for name, path in active.items()], profile=[], discovery=["current=off", "reuse=reuse"],
        jobs=4, repetitions=rounds, seed=seed))
    report["measurement"] = measurement
    initial = next(row["discovery"] for row in measurement["runs"] if row["mode"] == "reuse" and row["warmup"])
    if not initial["recorded_actions"] or any(
        row["discovery"]["hits"] != initial["recorded_actions"]
        for row in measurement["runs"] if row["mode"] == "reuse" and not row["warmup"]
    ):
        raise RuntimeError("contextual warm reuse did not hit every eligible prewarmed action")
    report["comparisons"] = {}
    report["reuse_comparisons"] = {}
    for name in baseline_names:
        pairs, reuse_pairs = [], []
        for index in range(rounds):
            runs = {row["mode"]: row for row in measurement["runs"] if not row["warmup"] and row["round"] == index}
            pairs.append((runs[name]["elapsed_ns"], runs["current"]["elapsed_ns"]))
            reuse_pairs.append((runs[name]["elapsed_ns"], runs["reuse"]["elapsed_ns"]))
        report["comparisons"][name] = comparison_statistics(pairs)
        report["reuse_comparisons"][name] = comparison_statistics(reuse_pairs, current_mode="reuse")
    report["comparison_status"] = "paired equivalent-plan no-op corrected/off/reuse calibration"


def main():
    parser = argparse.ArgumentParser(description="Native contextual unmodified/corrected baseline overhead")
    parser.add_argument("workload", choices=[workload["name"] for workload in workloads.WORKLOADS])
    parser.add_argument("--expected-binary-sha256", required=True)
    args = parser.parse_args()
    workload = next(workload for workload in workloads.WORKLOADS if workload["name"] == args.workload)
    destination = ROOT / "results/evaluation" / f"baselines-{args.workload}-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    evaluate_planning.evaluate(workload, destination, 10, 2027, args.expected_binary_sha256,
                               driver=baseline_plans, stage="native contextual baseline overhead")


if __name__ == "__main__":
    main()
