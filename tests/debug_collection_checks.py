import argparse
from contextlib import closing, ExitStack
import gzip
import json
import os
from pathlib import Path
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from debug_repro import BINARY_SHA256, GRAPH, intermediate_object
from evaluate_builds import collection_set, require_disposable_store
from input_view import InputView
import realize
from store_state import build_traces, store_path, trace_deletion_set


def verify_rebuild(rows):
    if len(rows) != 2 or [row["name"] for row in rows] != ["initial", "recovered"]:
        raise RuntimeError("the recovery check needs exactly two ordered builds")
    first, recovered = rows
    if (first["discovery"]["hits"] or first["discovery"]["recorded_actions"] != 1
            or first["discovery"]["calls"] != 1 or first["discovery"]["scans"] != 1
            or recovered["discovery"]["calls"] != 1 or recovered["discovery"]["hits"] != 1
            or recovered["discovery"]["scans"] != 0):
        raise RuntimeError("the collected-output rebuild did not reuse its discovery record")
    if first["discovery"]["namespace"] != recovered["discovery"]["namespace"]:
        raise RuntimeError("the collected-output rebuild changed discovery namespace")
    for key in ["source_sha256", "graph_sha256", "plan_sha256"]:
        if first[key] != recovered[key]:
            raise RuntimeError("the collected-output rebuild changed source, graph or plan")
    for target in ["app", "main.o"]:
        if (first["artifacts"][target]["sha256"] != recovered["artifacts"][target]["sha256"]
                or first["artifacts"][target]["path"] != recovered["artifacts"][target]["path"]):
            raise RuntimeError("the collected-output rebuild changed artifact bytes or store identity")
    if not all(row["debug_info_present"] for row in rows):
        raise RuntimeError("the collected-output rebuild lost its debug information")


def cleanup_trace_keys(protected, current, observed, protected_traces, current_traces):
    collection_set(protected, current)
    owned_history = collection_set(protected, set(protected) | set(observed))
    return trace_deletion_set(protected_traces, current_traces, protected, set(current) | set(owned_history))


def check(destination):
    report = {"schema_version": 1, "passed": False, "stage": "mapped debug rebuild after path-only collection",
              "harness_sha256": realize.digest(Path(__file__)), "runs": [], "performance_claim": "none"}
    env = dict(os.environ, CCACHE_DISABLE="1", LC_ALL="C", NIX_REMOTE="daemon")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["NIX_CONFIG"] = env.get("NIX_CONFIG", "") + "\nmax-jobs = 1\ncores = 1\n"

    def command(name, argv):
        return realize.invoke(argv, env, destination, name, report)

    def inventory(name):
        command(name, ["nix", "path-info", "--all", "--store", "daemon"])
        paths = (destination / f"{name}.stdout").read_text().splitlines()
        if len(paths) != len(set(paths)):
            raise RuntimeError("duplicate store paths in the recovery check")
        return set(paths)

    def traces(name):
        value = build_traces()
        (destination / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
        return value

    try:
        require_disposable_store()
        report["pins"] = json.loads((ROOT / "environment/pins.json").read_text())
        report["environment"] = json.loads((ROOT / "results/setup/environment.json").read_text())
        source, report["postimages"] = realize.snapshot_variant("recording", destination)
        command("package", ["nix", "build", "--impure", "--expr", realize.expression(source), "--no-link", "--print-out-paths"])
        binary = Path((destination / "package.stdout").read_text().strip()) / "bin/nix-ninja"
        report.update(binary=str(binary), binary_sha256=realize.digest(binary))
        if report["binary_sha256"] != BINARY_SHA256:
            raise RuntimeError("debug collection requires the frozen native binary")
        control = ROOT / "benchmarks/cold-debug/control.nix"
        expression = "import " + str(control) + " { source = " + str(source) + "; }"
        command("mapped-helper", ["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths"])
        helper = store_path((destination / "mapped-helper.stdout").read_text().strip())
        report["control"] = {"name": "mapped-debug-paths-v1", "helper": helper,
                             "sources": {str(file.relative_to(ROOT)): realize.digest(file)
                                         for file in [control, control.with_name("task-flags.sh"), ROOT / "environment/baseline.nix"]}}
        env["PATH"] = helper + "/bin" + os.pathsep + str(binary.parent) + os.pathsep + env["PATH"]
        readelf = shutil.which("readelf", path=env["PATH"])
        if not readelf or not Path(readelf).is_relative_to("/nix/store"):
            raise RuntimeError("the recovery check needs pinned readelf")
        protected = inventory("protected-inventory")
        protected_traces = traces("protected-build-traces")
        owned_path_history = set()
        try:
            with closing(InputView.create(provenance={"debug_collection": report["harness_sha256"]})) as view, ExitStack() as controller:
                controller.enter_context(view.lease())
                source_dir, build = view.root / "source", view.root / "build"
                source_dir.mkdir()
                (source_dir / "main.c").write_text("int main(void) { return 0; }\n")
                report["input_view"] = view.data
                cache = view.control / "discovery"
                for name in ["initial", "recovered"]:
                    if build.exists():
                        shutil.rmtree(build)
                    build.mkdir()
                    (build / "build.ninja").write_text(GRAPH)
                    row = {"name": name, "source_sha256": realize.digest(source_dir / "main.c"),
                           "graph_sha256": realize.digest(build / "build.ninja"), "artifacts": {}}
                    report["runs"].append(row)
                    result = realize.invoke_planner(binary, view, "app", env, destination, name, report, controller, "reuse")
                    row["discovery"] = result["discovery"]
                    current = inventory(name + "-artifact-inventory")
                    owned_path_history.update(collection_set(protected, current))
                    object_file = intermediate_object(current, protected)
                    for target, file in [("app", build / "app"), ("main.o", object_file)]:
                        archive = destination / f"{name}-{target}.gz"
                        with file.open("rb") as input_file, gzip.open(archive, "xb") as output:
                            shutil.copyfileobj(input_file, output)
                        row["artifacts"][target] = {"path": str(file.resolve(strict=True)), "sha256": realize.digest(file),
                                                    "saved_artifact": archive.name}
                    command(name + "-probe", [str(build / "app")])
                    command(name + "-sections", [readelf, "--sections", "--wide", str(object_file)])
                    command(name + "-debug", [readelf, "--debug-dump=info", str(object_file)])
                    row["debug_info_present"] = ".debug_info" in (destination / f"{name}-sections.stdout").read_text()
                    realize.invoke_planner(binary, view, "app", env, destination, name + "-plan", report, controller, "reuse", "drv")
                    row["plan_sha256"] = realize.plan_digest(destination, name + "-plan")
                    if (realize.digest(source_dir / "main.c") != row["source_sha256"]
                            or realize.digest(build / "build.ninja") != row["graph_sha256"]):
                        raise RuntimeError("debug collection changed its source or graph")
                    if name == "initial":
                        before = inventory("collection-before")
                        selected = collection_set(protected, before)
                        owned_path_history.update(selected)
                        retained = traces("collection-traces-before")
                        keys = trace_deletion_set(protected_traces, retained, protected, before)
                        if not selected or not keys:
                            raise RuntimeError("the recovery check did not create paths and CA traces")
                        metadata = realize.digest(cache / "state.json")
                        shutil.copyfile(cache / "state.json", destination / "retained-state.json")
                        shutil.rmtree(build)
                        command("collection-delete-paths", ["nix", "store", "delete", "--store", "daemon", *selected])
                        after = inventory("collection-after")
                        remaining = traces("collection-traces-after")
                        report["collection"] = {"paths": selected, "retained_trace_keys": keys,
                                                "metadata_before": metadata, "metadata_after": realize.digest(cache / "state.json")}
                        if after != protected or remaining != retained or report["collection"]["metadata_after"] != metadata:
                            raise RuntimeError("path-only collection changed protected paths, retained traces or discovery metadata")
                    print(f"{name}: debug artifact retained, discovery hits={row['discovery']['hits']}", flush=True)
                shutil.copyfile(cache / "state.json", destination / "recovered-state.json")
                recovered_traces = traces("recovered-build-traces")
                verify_rebuild(report["runs"])
                if any(recovered_traces.get(key) != value for key, value in retained.items()):
                    raise RuntimeError("rebuilding changed a retained CA trace")
        except Exception as error:
            report["recovery_error"] = str(error)
            raise
        finally:
            before = inventory("final-reset-before")
            selected = collection_set(protected, before)
            current_traces = traces("final-reset-traces-before")
            keys = cleanup_trace_keys(protected, before, owned_path_history, protected_traces, current_traces)
            report["final_reset"] = {"paths": selected, "trace_keys": keys, "observed_owned_paths": sorted(owned_path_history)}
            if keys:
                command("final-reset-delete-traces", ["nix", "store", "build-trace", "delete", "--store", "local", *keys])
            if selected:
                command("final-reset-delete-paths", ["nix", "store", "delete", "--store", "daemon", *selected])
            after = inventory("final-reset-after")
            remaining = traces("final-reset-traces-after")
            if after != protected or remaining != protected_traces:
                raise RuntimeError("debug collection did not restore protected paths and traces")
        report["passed"] = True
        print("debug collection: retained traces and discovery records recover identical objects and executable", flush=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Check mapped debug builds after normal path-only output collection")
    parser.add_argument("--disposable-store", choices=["daemon"], required=True)
    parser.parse_args()
    destination = ROOT / "results/lifecycle" / f"debug-collection-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    check(destination)


if __name__ == "__main__":
    main()
