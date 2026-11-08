import argparse
from contextlib import ExitStack
from functools import partial
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import time

import evaluate_planning as planning
from planning import discovery_report
import realize
from store_state import build_traces, store_path, trace_deletion_set
import workloads


ROOT = Path(__file__).resolve().parents[1]


def require_disposable_store():
    if platform.system() != "Linux" or not Path("/.dockerenv").is_file():
        raise RuntimeError("cold builds require the explicitly disposable project container store")


def collection_set(protected, current):
    protected, current = set(protected), set(current)
    if not protected <= current:
        raise RuntimeError("the protected store inventory changed")
    selected = sorted(current - protected)
    if len(selected) > 8192:
        raise RuntimeError("collection exceeds the experiment path budget")
    for name in selected:
        path = Path(name)
        if (path.parent != Path("/nix/store") or len(path.name) < 34 or path.name[32] != "-"
                or any(character not in "0123456789abcdfghijklmnpqrsvwxyz" for character in path.name[:32])):
            raise RuntimeError("collection path is outside the logical store")
    return selected


def retained_object(path):
    if path.is_symlink():
        raise RuntimeError("object probe cannot follow a store-output symlink")
    if path.is_file():
        return path
    target = path
    for component in ["src", "libcmd", "libnixcmd.so.2.36.0.p", "installable-derived-path.cc.o"]:
        target /= component
        if target.is_symlink():
            raise RuntimeError("object probe cannot follow an output-tree symlink")
    if not target.is_file():
        raise RuntimeError("the pinned object probe is missing from its output tree")
    return target


def paired_builds(binary, workload, view, destination, report, rounds, seed, cold=False, first_round=0, debug_paths=False):
    env = dict(os.environ, CCACHE_DISABLE="1", LC_ALL="C", NIX_REMOTE="daemon")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    env["NIX_CONFIG"] = env.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"
    report.update(runs=[], pairs=[], compiler_cache="disabled", page_cache="uncontrolled; no flush",
                  store_state="identical protected inventory before every cold build" if cold else "prewarmed union of declared build states",
                  harness_sha256=realize.digest(Path(__file__)), cold=cold, first_round=first_round)
    if cold:
        require_disposable_store()
        report["cold_control"] = "mapped-debug-paths-v1" if debug_paths else "legacy"
    if debug_paths:
        if not cold:
            raise ValueError("the debug-path control is restricted to cold evaluation")
        control = ROOT / "benchmarks/cold-debug/control.nix"
        source = ROOT / ".work/nix-ninja-recording"
        expression = "import " + str(control) + " { source = " + str(source) + "; }"
        realize.invoke(["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths"],
                       env, destination, "debug-path-helper", report)
        helper = store_path((destination / "debug-path-helper.stdout").read_text().strip())
        env["PATH"] = helper + "/bin" + os.pathsep + env["PATH"]
        report["task_control"] = {"helper": helper, "helper_path": env["PATH"], "expression": expression,
                                  "sources": {str(path.relative_to(ROOT)): realize.digest(path)
                                              for path in [control, control.with_name("task-flags.sh"), ROOT / "environment/baseline.nix"]}}
    with ExitStack() as controller:
        controller.enter_context(view.lease())
        view.validate_links()
        source, build = view.root / "source", view.root / "build"
        template = view.control / "baseline-build"
        start = time.monotonic_ns()
        shutil.copytree(build, template, symlinks=True)
        report["build_template_ns"] = time.monotonic_ns() - start
        edits = planning.Edits(source, workload["content_probes"])
        report["original_source"] = edits.snapshot()
        state_root = view.control / "evaluation-state"
        state_root.mkdir(mode=0o700)

        def restore_build():
            start = time.monotonic_ns()
            shutil.rmtree(build)
            shutil.copytree(template, build, symlinks=True)
            return time.monotonic_ns() - start

        def inventory(name):
            realize.invoke(["nix", "path-info", "--all", "--store", "daemon"], env, destination, name, report)
            paths = (destination / f"{name}.stdout").read_text().splitlines()
            if len(paths) != len(set(paths)):
                raise RuntimeError("store inventory contains duplicate paths")
            return set(paths)

        def reset_store(name, protected):
            start = time.monotonic_ns()
            before = inventory(name + "-before")
            selected = collection_set(protected, before)
            traces_before = build_traces()
            trace_keys = trace_deletion_set(protected_traces, traces_before, protected, before)
            (destination / f"{name}-traces-before.json").write_text(json.dumps(traces_before, indent=2) + "\n")
            if trace_keys:
                realize.invoke(["nix", "store", "build-trace", "delete", "--store", "local", *trace_keys],
                               env, destination, name + "-delete-traces", report)
            if selected:
                realize.invoke(["nix", "store", "delete", "--store", "daemon", *selected],
                               env, destination, name + "-delete", report)
            after = inventory(name + "-after")
            traces_after = build_traces()
            (destination / f"{name}-traces-after.json").write_text(json.dumps(traces_after, indent=2) + "\n")
            report.setdefault("store_resets", []).append({"name": name, "selected": selected,
                                                          "trace_keys": trace_keys, "protected_count": len(protected),
                                                          "after_count": len(after), "elapsed_ns": time.monotonic_ns() - start})
            if after != protected or traces_after != protected_traces:
                raise RuntimeError("store reset did not restore the exact protected inventory and build-trace map")

        def retain_artifact(name, path):
            start = time.monotonic_ns()
            target = destination / f"{name}.artifact.gz"
            with path.open("rb") as source, target.open("xb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=1, mtime=0) as archive:
                    shutil.copyfileobj(source, archive, length=1024 * 1024)
            return {"saved_artifact": target.name, "artifact_copy_ns": time.monotonic_ns() - start}

        def invoke(name, mode, cache, kind, case, round_number, tool=None):
            argv = [str(binary), "-C", str(build), "-j4", "--input-view", str(view.marker),
                    "--discovery-mode", mode, "--discovery-cache", str(cache)]
            if tool is not None:
                argv += ["-t", tool]
            argv.append(workload["target"])
            row = {"name": name, "mode": mode, "kind": kind, "scenario": case, "round": round_number,
                   "input_state": edits.snapshot()}
            report["runs"].append(row)
            row["build_reset_ns"] = restore_build()
            controller.close()
            try:
                row.update(planning.timed_process(argv, env, destination, name))
            finally:
                controller.enter_context(view.lease())
            if row["returncode"]:
                raise RuntimeError(f"build evaluation failed: {name}")
            row["discovery"] = discovery_report((destination / f"{name}.stderr").read_text(), mode)
            if tool == "drv":
                row["plan_sha256"] = realize.plan_digest(destination, name)
            else:
                target = build / workload["target"]
                row.update(artifact=str(target.resolve(strict=True)), artifact_sha256=realize.digest(target))
                probe(name, target)
                if cold:
                    row.update(retain_artifact(name, target))
                    realize.invoke(["nix", "path-info", "--all", "--json", "--store", "daemon"],
                                   env, destination, name + "-store-info", report)
                    row["object_probes"] = []
                    for path in sorted(inventory(name + "-artifact-inventory")):
                        path = Path(path)
                        if path.name.endswith("-installable-derived-path.cc.o"):
                            target_object = retained_object(path)
                            artifact = retain_artifact(name + "-object-probe-" + path.name[:32], target_object)
                            row["object_probes"].append({"store_output": str(path), "path": str(target_object),
                                                         "sha256": realize.digest(target_object), **artifact})
                    if workload["name"] == "nix" and not row["object_probes"]:
                        raise RuntimeError("the cold Nix build did not retain its diagnostic object")
            if edits.snapshot() != row["input_state"]:
                raise RuntimeError(f"build modified the owned source: {name}")
            return row

        def probe(name, target):
            if workload["name"] == "nix":
                commands = [[str(target), "--version"],
                            [str(target), "--extra-experimental-features", "nix-command", "eval", "--offline", "--expr", "1 + 1"],
                            ["patchelf", "--print-rpath", str(target)]]
                expected = (1, "2")
            else:
                executable = destination / f"{name}-roundtrip"
                commands = [["cc", "-I" + str(source / "lib"), str(ROOT / "tests/fixtures/zstd_roundtrip.c"),
                             str(target), "-pthread", "-o", str(executable)], [str(executable)]]
                expected = (1, "zstd roundtrip ok")
            for index, argv in enumerate(commands):
                realize.invoke(argv, env, destination, f"{name}-probe-{index}", report)
            if (destination / f"{name}-probe-{expected[0]}.stdout").read_text().strip() != expected[1]:
                raise RuntimeError(f"built artifact failed its functional probe: {name}")

        try:
            if not cold:
                for case in planning.CASES:
                    edits.reset()
                    edits.apply(case)
                    invoke(f"prewarm-{case}", "off", state_root / "prewarm", "store-warmup", case, -1)
            edits.reset()
            restore_build()
            start = time.monotonic_ns()
            protected = inventory("protected-inventory")
            protected_traces = build_traces() if cold else None
            if cold:
                (destination / "protected-build-traces.json").write_text(json.dumps(protected_traces, indent=2) + "\n")
                report["store_reset_contract"] = "valid paths and pinned CA build-trace map; supported APIs only"
            report["store_state_setup_ns"] = time.monotonic_ns() - start
            report["protected_inventory_sha256"] = hashlib.sha256("\n".join(sorted(protected)).encode()).hexdigest()
            report["protected_inventory_count"] = len(protected)
            rng = random.Random(seed)
            orders = []
            for _ in range(first_round + rounds):
                cases = ["cold-build"] if cold else planning.CASES.copy()
                rng.shuffle(cases)
                order = []
                for case in cases:
                    modes = planning.MODES.copy()
                    rng.shuffle(modes)
                    order.append((case, modes))
                orders.append(order)
            for round_number in range(first_round, first_round + rounds):
                for case, modes in orders[round_number]:
                    prefix = f"r{round_number}-{case}"
                    pair = {"round": round_number, "scenario": case, "warmups": [], "measured": [], "passed": False}
                    report["pairs"].append(pair)
                    start = time.monotonic_ns()
                    edits.reset()
                    pair["reset_ns"] = time.monotonic_ns() - start
                    pair_root = state_root / prefix
                    pair_root.mkdir(mode=0o700)
                    base = None
                    if not cold:
                        for mode in planning.MODES:
                            row = invoke(f"{prefix}-warmup-{mode}", mode, pair_root / mode,
                                         "metadata-warmup", "no-op", round_number, "drv")
                            pair["warmups"].append(row["name"])
                            if mode == "reuse":
                                base = row["discovery"]
                        if base["hits"] or not base["recorded_actions"]:
                            raise RuntimeError("pre-edit metadata initialization was not cold and eligible")
                    start = time.monotonic_ns()
                    if not cold:
                        edits.apply(case)
                    pair["edit_ns"] = time.monotonic_ns() - start
                    measured = {}
                    for mode in modes:
                        name = f"{prefix}-timed-{mode}"
                        if cold:
                            restore_build()
                            reset_store(name + "-reset", protected)
                        elif inventory(name + "-inventory") != protected:
                            raise RuntimeError("the store-warm modes would start from different inventories")
                        row = invoke(name, mode, pair_root / mode, "measured", case, round_number)
                        pair["measured"].append(row["name"])
                        measured[mode] = row
                        audit = row["discovery"]
                        if mode == "reuse":
                            if cold and (audit["hits"] or not audit["recorded_actions"]):
                                raise RuntimeError("a cold build did not initialize eligible metadata")
                            if not cold and case in {"no-op", "irrelevant-edit"} and audit["hits"] != base["recorded_actions"]:
                                raise RuntimeError("unchanged build did not reuse all eligible records")
                            if not cold and case.endswith("edit") and case != "irrelevant-edit" and audit["hits"] >= base["recorded_actions"]:
                                raise RuntimeError("first post-edit build did not invalidate discovery")
                        plan = invoke(name + "-verify-plan", "off", pair_root / "verify", "post-build-plan", case, round_number, "drv")
                        row["verified_plan_sha256"] = plan["plan_sha256"]
                    if (measured["off"]["artifact_sha256"] != measured["reuse"]["artifact_sha256"]
                            or measured["off"]["verified_plan_sha256"] != measured["reuse"]["verified_plan_sha256"]):
                        raise RuntimeError("paired builds differ in artifact bytes or derivations")
                    metadata = pair_root / "reuse/state.json"
                    pair["metadata_bytes"] = metadata.stat().st_size
                    if len(report["pairs"]) in {1, rounds * (1 if cold else len(planning.CASES))}:
                        shutil.copyfile(metadata, destination / f"{prefix}-state.json")
                    start = time.monotonic_ns()
                    shutil.rmtree(pair_root)
                    pair["cleanup_ns"] = time.monotonic_ns() - start
                    pair["passed"] = True
                    print(f"{prefix}: matching build artifacts and plans", flush=True)
            if cold:
                restore_build()
                reset_store("final-reset", protected)
        finally:
            edits.reset()


def main():
    parser = argparse.ArgumentParser(description="Frozen paired end-to-end build measurements")
    parser.add_argument("workload", choices=[workload["name"] for workload in workloads.WORKLOADS])
    parser.add_argument("--kind", choices=["warm", "cold"], required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--first-round", type=int, default=0)
    parser.add_argument("--seed", type=int, default=2027)
    parser.add_argument("--expected-binary-sha256", required=True)
    parser.add_argument("--debug-path-control", choices=["legacy", "mapped"], default="legacy")
    parser.add_argument("--disposable-store", choices=["daemon"])
    args = parser.parse_args()
    if (args.first_round < 0 or args.rounds < 1 or args.first_round + args.rounds > 50
            or (args.kind == "warm" and (args.rounds < 10 or args.first_round))):
        parser.error("warm runs require 10–50 pairs; cold shards require indices within the first 50 pairs")
    if args.kind == "cold" and args.disposable_store != "daemon":
        parser.error("cold runs require explicit --disposable-store daemon")
    if args.kind != "cold" and args.debug_path_control != "legacy":
        parser.error("the debug-path control is restricted to cold evaluation")
    workload = next(workload for workload in workloads.WORKLOADS if workload["name"] == args.workload)
    destination = ROOT / "results/evaluation" / f"{args.kind}-{args.workload}-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    driver = partial(paired_builds, cold=args.kind == "cold", first_round=args.first_round,
                     debug_paths=args.debug_path_control == "mapped")
    planning.evaluate(workload, destination, args.rounds, args.seed, args.expected_binary_sha256,
                      driver=driver, stage="frozen paired end-to-end evaluation")


if __name__ == "__main__":
    main()
