import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import random
import shutil
import time

import evaluate_planning as planning
import operation_profile
from planning import discovery_report
import realize
import workloads


ROOT = Path(__file__).resolve().parents[1]
STAGE = "supplementary planner operation counts"
PHASES = ["planning.total", "planning.parse", "planning.build_directory", "planning.schedule",
          "action.prepare", "action.construct", "action.construct_dynamic", "action.resolve",
          "discovery.materialize", "store.add_derivation", "store.add_text", "store.add_nar", "store.build_paths"]


def profile_counts(profile, build):
    if (profile["schema_version"] != 1 or profile["mode"] != "planning-profile"
            or profile["build_directory"] != str(build)
            or profile["count_semantics"] != "planner API invocations including failures; not wire requests or daemon operations"
            or profile["phase_semantics"] != "inclusive cumulative wall time; concurrent phases overlap"):
        raise RuntimeError("operation profile has an incompatible schema, build path or count meaning")
    phases = profile["phases"]
    if set(phases) - set(PHASES) or phases.get("planning.total", {}).get("calls") != 1:
        raise RuntimeError("operation profile has unknown phases or incomplete planning")
    for metric in phases.values():
        if set(metric) != {"calls", "elapsed_ns"} or any(type(value) is not int or value < 0 for value in metric.values()):
            raise RuntimeError("operation profile has invalid counters")
    return {name: phases.get(name, {}).get("calls", 0) for name in PHASES}


def validate_pair(pair, rows):
    measured = {tuple(rows[name][key] for key in ["variant", "mode"]): rows[name] for name in pair["measured"]}
    if len(pair["measured"]) != 4 or set(measured) != {(variant, mode) for variant in ["frozen", "profiled"] for mode in planning.MODES}:
        raise RuntimeError("diagnostic pair lacks an exact frozen/profiled and off/reuse comparison")
    if len({row["plan_sha256"] for row in measured.values()}) != 1:
        raise RuntimeError("instrumented and frozen raw derivation plans differ")
    if len({json.dumps(row["input_state"], sort_keys=True) for row in measured.values()}) != 1:
        raise RuntimeError("instrumented and frozen source edits differ")
    for mode in planning.MODES:
        frozen, profiled = [measured[(variant, mode)]["discovery"] for variant in ["frozen", "profiled"]]
        for key in ["calls", "hits", "scans", "recorded_actions", "bypasses"]:
            if frozen.get(key) != profiled.get(key):
                raise RuntimeError("instrumentation changed discovery work")
    for variant in ["frozen", "profiled"]:
        warmups = [rows[name] for name in pair["warmups"] if rows[name]["variant"] == variant]
        if len(warmups) != 1 or warmups[0]["mode"] != "reuse":
            raise RuntimeError("diagnostic metadata was not initialized separately per binary")
        base = warmups[0]["discovery"]
        reuse = measured[(variant, "reuse")]["discovery"]
        if base["hits"] or not base["recorded_actions"]:
            raise RuntimeError("diagnostic metadata initialization was not cold and eligible")
        if pair["scenario"] in {"no-op", "irrelevant-edit"}:
            if reuse["hits"] != base["recorded_actions"]:
                raise RuntimeError("diagnostic unchanged input did not exercise expected hits")
        elif reuse["hits"] >= base["recorded_actions"]:
            raise RuntimeError("diagnostic first post-edit plan did not invalidate discovery")
    return measured


def diagnose(binary, workload, view, destination, report, rounds, seed):
    manifest, postimages = operation_profile.verify_source(operation_profile.SOURCE)
    if report["binary_sha256"] != manifest["reference_binary_sha256"] or rounds != 1:
        raise RuntimeError("operation counts require one diagnostic round on the frozen binary")
    report.update(operation_manifest=manifest, diagnostic_postimages=postimages,
                  operation_driver_sha256=realize.digest(Path(__file__)), runs=[], pairs=[],
                  performance_claim="none; diagnostic timings are excluded from primary speedup samples")
    shutil.copyfile(operation_profile.MANIFEST, destination / "operation-manifest.json")
    shutil.copyfile(operation_profile.configuration()[1], destination / "instrumentation.patch")
    env = dict(os.environ, CCACHE_DISABLE="1", LC_ALL="C", NIX_REMOTE="daemon")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    env["NIX_CONFIG"] = env.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"
    realize.invoke(["nix", "build", "--impure", "--expr", realize.expression(operation_profile.SOURCE),
                    "--no-link", "--print-out-paths"], env, destination, "profile-package", report)
    profiled = Path((destination / "profile-package.stdout").read_text().strip()) / "bin/nix-ninja"
    report.update(diagnostic_binary=str(profiled), diagnostic_binary_sha256=realize.digest(profiled),
                  helper_path=env["PATH"], helper_identity="frozen CLI and common pinned task/runtime shell for both binaries")
    binaries = {"frozen": binary, "profiled": profiled}
    rows = {}
    with ExitStack() as controller:
        controller.enter_context(view.lease())
        view.validate_links()
        edits = planning.Edits(view.root / "source", workload["content_probes"])
        state_root = view.control / "operation-state"
        state_root.mkdir(mode=0o700)
        report["original_source"] = edits.snapshot()

        def invoke(name, variant, mode, cache, kind, case):
            current_env = dict(env)
            row = {"name": name, "variant": variant, "mode": mode, "kind": kind,
                   "scenario": case, "input_state": edits.snapshot()}
            report["runs"].append(row)
            rows[name] = row
            if variant == "profiled":
                profile = destination / f"{name}-profile.json"
                row["profile_file"] = profile.name
                current_env["NIX_NINJA_PROFILE"] = str(profile)
            argv = [str(binaries[variant]), "-C", str(view.root / "build"), "-j4", "-t", "drv",
                    "--input-view", str(view.marker), "--discovery-mode", mode,
                    "--discovery-cache", str(cache), workload["target"]]
            controller.close()
            try:
                row.update(planning.timed_process(argv, current_env, destination, name))
            finally:
                controller.enter_context(view.lease())
            if row["returncode"]:
                raise RuntimeError(f"operation-count planning failed: {name}")
            row["discovery"] = discovery_report((destination / f"{name}.stderr").read_text(), mode)
            row["plan_sha256"] = realize.plan_digest(destination, name)
            if variant == "profiled":
                row["counts"] = profile_counts(json.loads(profile.read_text()), view.root / "build")
            if edits.snapshot() != row["input_state"]:
                raise RuntimeError("operation-count planning modified source inputs")
            return row

        try:
            for case in planning.CASES:
                edits.reset()
                edits.apply(case)
                invoke(f"prewarm-{case}", "frozen", "off", state_root / "unused", "store-warmup", case)
            rng = random.Random(seed)
            order = planning.CASES.copy()
            rng.shuffle(order)
            for case in order:
                edits.reset()
                pair = {"scenario": case, "warmups": [], "measured": []}
                report["pairs"].append(pair)
                for variant in binaries:
                    name = f"{case}-warmup-{variant}"
                    invoke(name, variant, "reuse", state_root / f"{case}-{variant}-reuse", "metadata-warmup", "no-op")
                    pair["warmups"].append(name)
                edits.apply(case)
                labels = [(variant, mode) for variant in binaries for mode in planning.MODES]
                rng.shuffle(labels)
                for variant, mode in labels:
                    name = f"{case}-{variant}-{mode}"
                    invoke(name, variant, mode, state_root / f"{case}-{variant}-{mode}", "diagnostic", case)
                    pair["measured"].append(name)
                validate_pair(pair, rows)
                pair["passed"] = True
                print(f"{case}: frozen/profiled off/reuse plans and source match; raw counts retained", flush=True)
        finally:
            edits.reset()


def main():
    parser = argparse.ArgumentParser(description="Supplement frozen evaluation with isolated planner API counts")
    parser.add_argument("workload", choices=[value["name"] for value in workloads.WORKLOADS])
    args = parser.parse_args()
    workload = next(value for value in workloads.WORKLOADS if value["name"] == args.workload)
    destination = ROOT / "results/evaluation" / f"operations-{args.workload}-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    planning.evaluate(workload, destination, 1, 2027, operation_profile.configuration()[0]["reference_binary_sha256"],
                      driver=diagnose, stage=STAGE)


if __name__ == "__main__":
    main()
