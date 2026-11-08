import argparse
from contextlib import closing, ExitStack
import gzip
import json
import os
from pathlib import Path
import shutil
import time

from evaluate_builds import collection_set, require_disposable_store
from input_view import InputView
from planning import discovery_report
import realize
from store_state import build_traces, trace_deletion_set


ROOT = Path(__file__).resolve().parents[1]
BINARY_SHA256 = "a9bdf2d1ba5d26a136e1b64602d9e0cc2b56232038e47ce006d3c30bc164052a"
GRAPH = """rule cc
  command = cc -O0 -g -frandom-seed=0 -MD -MF $out.d -o $out -c $in
  depfile = $out.d
  deps = gcc
rule link
  command = cc -frandom-seed=0 $in -o $out
build main.o: cc ../source/main.c
build app: link main.o
"""


def compare_pairs(rows):
    results = {}
    for condition in ["legacy", "mapped"]:
        pair = [row for row in rows if row["condition"] == condition]
        if len(pair) != 2 or {row["repetition"] for row in pair} != {0, 1}:
            raise RuntimeError("the diagnostic needs exactly two cold builds per condition")
        if pair[0]["source_sha256"] != pair[1]["source_sha256"] or pair[0]["plan_sha256"] != pair[1]["plan_sha256"]:
            raise RuntimeError("the diagnostic changed source state or paired derivation plans")
        equal = {name: pair[0]["artifacts"][name]["sha256"] == pair[1]["artifacts"][name]["sha256"]
                 for name in ["app", "main.o"]}
        if not all(row["debug_info_present"] for row in pair):
            raise RuntimeError("the diagnostic lost debug information")
        if condition == "legacy" and any(equal.values()):
            raise RuntimeError("legacy debug paths did not reproduce the raw byte mismatch")
        if condition == "mapped" and not all(equal.values()):
            raise RuntimeError("mapped debug paths did not produce byte-identical artifacts")
        results[condition] = {"byte_equal": equal, "plan_sha256": pair[0]["plan_sha256"]}
    return results


def check(destination):
    report = {"schema_version": 1, "passed": False, "stage": "cache-off cold debug-path reproduction and control",
              "harness_sha256": realize.digest(Path(__file__)), "runs": [], "store_resets": [],
              "performance_claim": "none; four diagnostic builds, all discovery cache off"}
    env = dict(os.environ, CCACHE_DISABLE="1", LC_ALL="C", NIX_REMOTE="daemon")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["NIX_CONFIG"] = env.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"

    def command(name, argv, current_env=env):
        return realize.invoke(argv, current_env, destination, name, report)

    def inventory(name):
        command(name, ["nix", "path-info", "--all", "--store", "daemon"])
        paths = (destination / f"{name}.stdout").read_text().splitlines()
        if len(paths) != len(set(paths)):
            raise RuntimeError("duplicate logical store paths")
        return set(paths)

    try:
        require_disposable_store()
        report["pins"] = json.loads((ROOT / "environment/pins.json").read_text())
        report["environment"] = json.loads((ROOT / "results/setup/environment.json").read_text())
        source, report["postimages"] = realize.snapshot_variant("recording", destination)
        command("package", ["nix", "build", "--impure", "--expr", realize.expression(source), "--no-link", "--print-out-paths"])
        binary = Path((destination / "package.stdout").read_text().strip()) / "bin/nix-ninja"
        report.update(binary=str(binary), binary_sha256=realize.digest(binary))
        if report["binary_sha256"] != BINARY_SHA256:
            raise RuntimeError("debug-path reproduction requires the frozen native binary")
        control = ROOT / "benchmarks/cold-debug/control.nix"
        expression = "import " + str(control) + " { source = " + str(source) + "; }"
        command("mapped-helper", ["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths"])
        helper = Path((destination / "mapped-helper.stdout").read_text().strip())
        report["control"] = {"helper": str(helper), "expression": expression,
                             "sources": {str(path.relative_to(ROOT)): realize.digest(path)
                                         for path in [control, control.with_name("task-flags.sh")]}}
        env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
        readelf = shutil.which("readelf", path=env["PATH"])
        legacy_helper = shutil.which("nix-ninja-task", path=env["PATH"])
        if not readelf or not legacy_helper or not Path(legacy_helper).is_relative_to("/nix/store"):
            raise RuntimeError("the pinned shell must supply readelf and the legacy task helper")
        report.update(readelf=readelf, legacy_helper=legacy_helper)
        protected = inventory("protected-inventory")
        protected_traces = build_traces()
        (destination / "protected-build-traces.json").write_text(json.dumps(protected_traces, indent=2) + "\n")

        def reset(name):
            before = inventory(name + "-before")
            traces = build_traces()
            selected = collection_set(protected, before)
            keys = trace_deletion_set(protected_traces, traces, protected, before)
            (destination / f"{name}-traces-before.json").write_text(json.dumps(traces, indent=2) + "\n")
            if keys:
                command(name + "-delete-traces", ["nix", "store", "build-trace", "delete", "--store", "local", *keys])
            if selected:
                command(name + "-delete", ["nix", "store", "delete", "--store", "daemon", *selected])
            after, remaining = inventory(name + "-after"), build_traces()
            (destination / f"{name}-traces-after.json").write_text(json.dumps(remaining, indent=2) + "\n")
            report["store_resets"].append({"name": name, "selected": selected, "trace_keys": keys,
                                           "protected_count": len(protected), "after_count": len(after)})
            if after != protected or remaining != protected_traces:
                raise RuntimeError("debug-path diagnostic did not restore protected paths and traces")

        with closing(InputView.create(provenance={"diagnostic": "debug-prefix-map"})) as view, ExitStack() as controller:
            controller.enter_context(view.lease())
            source_dir, build = view.root / "source", view.root / "build"
            source_dir.mkdir()
            (source_dir / "main.c").write_text("int main(void) { return 0; }\n")
            report["input_view"] = view.data
            try:
                for condition in ["legacy", "mapped"]:
                    current_env = dict(env)
                    if condition == "mapped":
                        current_env["PATH"] = str(helper / "bin") + os.pathsep + env["PATH"]
                    for repetition in range(2):
                        name = f"{condition}-{repetition}"
                        reset(name + "-reset")
                        if build.exists():
                            shutil.rmtree(build)
                        build.mkdir()
                        (build / "build.ninja").write_text(GRAPH)
                        argv = [str(binary), "-C", str(build), "-j4", "--input-view", str(view.marker),
                                "--discovery-mode", "off", "--discovery-cache", str(view.control / "unused-cache")]
                        row = {"name": name, "condition": condition, "repetition": repetition,
                               "source_sha256": realize.digest(source_dir / "main.c"), "artifacts": {}}
                        report["runs"].append(row)
                        controller.close()
                        try:
                            command(name, [*argv, "app", "main.o"], current_env)
                        finally:
                            controller.enter_context(view.lease())
                        row["discovery"] = discovery_report((destination / f"{name}.stderr").read_text(), "off")
                        for target in ["app", "main.o"]:
                            artifact = build / target
                            archive = destination / f"{name}-{target}.gz"
                            with artifact.open("rb") as input_file, gzip.open(archive, "xb") as output:
                                shutil.copyfileobj(input_file, output)
                            row["artifacts"][target] = {"path": str(artifact.resolve(strict=True)),
                                                        "sha256": realize.digest(artifact), "saved_artifact": archive.name}
                        command(name + "-probe", [str(build / "app")], current_env)
                        command(name + "-sections", [readelf, "--sections", "--wide", str(build / "main.o")], current_env)
                        command(name + "-debug", [readelf, "--debug-dump=info", str(build / "main.o")], current_env)
                        row["debug_info_present"] = ".debug_info" in (destination / f"{name}-sections.stdout").read_text()
                        controller.close()
                        try:
                            command(name + "-plan", [*argv, "-t", "drv", "app"], current_env)
                        finally:
                            controller.enter_context(view.lease())
                        row["plan_sha256"] = realize.plan_digest(destination, name + "-plan")
                        if realize.digest(source_dir / "main.c") != row["source_sha256"]:
                            raise RuntimeError("debug-path diagnostic modified its source")
                        print(f"{name}: saved raw app/object bytes and debug information", flush=True)
                report["comparison"] = compare_pairs(report["runs"])
            finally:
                reset("final-reset")
        report["passed"] = True
        print("debug paths: legacy mismatch reproduced; mapped app/object bytes match with debug information", flush=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Reproduce cold-build debug-path nondeterminism with cache off")
    parser.add_argument("--disposable-store", choices=["daemon"], required=True)
    parser.parse_args()
    destination = ROOT / "results/lifecycle" / f"debug-paths-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    check(destination)


if __name__ == "__main__":
    main()
