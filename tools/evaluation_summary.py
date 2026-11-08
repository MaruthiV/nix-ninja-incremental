import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import random
import statistics

import evaluate_planning
from planning import discovery_report
import realize
from store_state import store_path, trace_deletion_set


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def paired_statistics(pairs, seed=2027, samples=10000):
    require(len(pairs) > 0 and all(off > 0 and reuse > 0 for off, reuse in pairs), "nonpositive or missing paired timing")
    effects = [1 - reuse / off for off, reuse in pairs]
    rng = random.Random(seed)
    bootstrap = sorted(statistics.median(rng.choices(effects, k=len(effects))) for _ in range(samples))

    def quantile(fraction):
        position = (len(bootstrap) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(bootstrap) - 1)
        return bootstrap[lower] + (bootstrap[upper] - bootstrap[lower]) * (position - lower)

    interval = [quantile(0.025), quantile(0.975)]
    return {"pairs": len(pairs), "median_paired_improvement": statistics.median(effects),
            "paired_bootstrap_95_interval": interval, "bootstrap_samples": samples, "analysis_seed": seed,
            "median_off_ns": statistics.median(off for off, _ in pairs),
            "median_reuse_ns": statistics.median(reuse for _, reuse in pairs),
            "median_paired_saved_ns": statistics.median(off - reuse for off, reuse in pairs),
            "noise_extension_eligible": interval[1] - interval[0] > 0.10}


def cost_statistics(observations):
    result = {"worker_counter_scope": "cumulative worker times overlap; they are not additive elapsed phases",
              "rss_scope": "CLI and waited children; independent daemon workers excluded",
              "metadata_bytes_median": statistics.median(row["metadata_bytes"] for row in observations),
              "controller_ns_median": {name: statistics.median(row[name] for row in observations)
                                       for name in ["reset_ns", "edit_ns", "cleanup_ns"]}, "modes": {}}
    for mode in ["off", "reuse"]:
        rows = [row["modes"][mode] for row in observations]
        fields = {name: [row[name] for row in rows if name in row]
                  for name in ["user_cpu_ns", "system_cpu_ns", "max_rss_bytes", "build_reset_ns"]}
        for name in ["setup_ns", "cumulative_discovery_ns", "cumulative_validation_ns", "publication_ns", "hits", "scans"]:
            fields[name] = [row["discovery"][name] for row in rows if name in row["discovery"]]
        for name in ["files_hashed", "bytes_hashed", "resolutions", "retained_bytes"]:
            fields[name] = [row["discovery"]["observations"][name] for row in rows
                            if row["discovery"].get("observations") is not None and name in row["discovery"]["observations"]]
        result["modes"][mode] = {name: {"median": statistics.median(values) if values else None, "samples": len(values)}
                                  for name, values in fields.items()}
    return result


def verified_rows(path, data, kind):
    rows = {}
    for row in data["runs"]:
        name, mode = row["name"], row["mode"]
        require(name not in rows and Path(name).name == name, "duplicate or unsafe run name")
        require(row["returncode"] == 0 and row["elapsed_ns"] > 0, f"failed or untimed invocation: {name}")
        raw = discovery_report((path.parent / f"{name}.stderr").read_text(), mode)
        require(row["discovery"] == raw, f"raw discovery report differs: {name}")
        if kind == "planning" or row["kind"] in {"metadata-warmup", "post-build-plan"}:
            require(realize.plan_digest(path.parent, name) == row["plan_sha256"], f"raw derivation differs: {name}")
        if kind != "planning" and row["kind"] == "measured":
            plan = realize.plan_digest(path.parent, name + "-verify-plan")
            require(row["verified_plan_sha256"] == plan, f"post-build derivation differs: {name}")
            probe_name = name + "-probe-1"
            probe = data["processes"][probe_name]
            require(probe["returncode"] == 0, f"functional probe failed: {name}")
            expected = "2" if data["workload"]["name"] == "nix" else "zstd roundtrip ok"
            require((path.parent / f"{probe_name}.stdout").read_text().strip() == expected,
                    f"functional probe output differs: {name}")
            if kind == "cold":
                require(row["saved_artifact"] == f"{name}.artifact.gz", "unexpected retained cold artifact")
                with gzip.open(path.parent / row["saved_artifact"], "rb") as artifact:
                    require(hashlib.file_digest(artifact, "sha256").hexdigest() == row["artifact_sha256"],
                            f"retained cold artifact bytes differ: {name}")
                probes = row.get("object_probes", [])
                if data.get("cold_control") == "mapped-debug-paths-v1" and data["workload"]["name"] == "nix":
                    require(len(probes) > 0, "mapped cold Nix data lacks its diagnostic object")
                seen_outputs = set()
                for probe in probes:
                    output = Path(store_path(probe["store_output"]))
                    require(output not in seen_outputs and output.name.endswith("-installable-derived-path.cc.o"),
                            "duplicate or unexpected cold object output")
                    seen_outputs.add(output)
                    require(probe["path"] in {str(output), str(output / "src/libcmd/libnixcmd.so.2.36.0.p/installable-derived-path.cc.o")},
                            "cold object file is outside its pinned output layout")
                    require(probe["saved_artifact"] == f"{name}-object-probe-{output.name[:32]}.artifact.gz",
                            "unexpected retained cold object filename")
                    with gzip.open(path.parent / probe["saved_artifact"], "rb") as artifact:
                        require(hashlib.file_digest(artifact, "sha256").hexdigest() == probe["sha256"],
                                "retained cold object bytes differ")
        rows[name] = row
    return rows


def verified_pair(pair, rows, data, kind):
    require(pair.get("passed", False), "failed pair cannot enter the summary")
    measured = [rows[name] for name in pair["measured"]]
    require(len(measured) == 2 and {row["mode"] for row in measured} == {"off", "reuse"}, "missing or duplicated paired mode")
    modes = {row["mode"]: row for row in measured}
    case, index = pair["scenario"], pair["round"]
    require(all(row["kind"] == "measured" and row["scenario"] == case and row["round"] == index for row in measured),
            "pair points to the wrong scenario or round")
    require(modes["off"]["input_state"] == modes["reuse"]["input_state"], "paired inputs differ")
    plan_key = "plan_sha256" if kind == "planning" else "verified_plan_sha256"
    require(modes["off"][plan_key] == modes["reuse"][plan_key], "paired derivations differ")
    if kind != "planning":
        require(modes["off"]["artifact_sha256"] == modes["reuse"]["artifact_sha256"], "paired artifact observations differ")
    original = data["original_source"]
    probes = data["workload"]["content_probes"]
    changed = {name for name in original if original[name] != modes["off"]["input_state"].get(name)}
    expected = {"implementation-edit": {probes["implementation"]}, "shared-header-edit": {probes["shared_header"]},
                "irrelevant-edit": {".nni-irrelevant.txt"}}
    require(set(modes["off"]["input_state"]) == set(original) and changed == expected.get(case, set()),
            "the measured edit differs from the declared scenario")
    audit = modes["reuse"]["discovery"]
    if kind == "cold":
        require(not pair["warmups"] and audit["hits"] == 0 and audit["recorded_actions"] > 0,
                "cold pair reused warm metadata")
    else:
        warmups = [rows[name] for name in pair["warmups"]]
        require(len(warmups) == 2 and {row["mode"] for row in warmups} == {"off", "reuse"}, "missing pre-edit metadata initialization")
        require(all(row["kind"] == "metadata-warmup" and row["input_state"] == original for row in warmups),
                "metadata was warmed after the edit")
        base = next(row for row in warmups if row["mode"] == "reuse")["discovery"]
        require(base["hits"] == 0 and base["recorded_actions"] > 0, "metadata initialization was not cold and eligible")
        if case in {"no-op", "irrelevant-edit"}:
            require(audit["hits"] == base["recorded_actions"], "unchanged pair omitted eligible hits")
        else:
            require(audit["hits"] < base["recorded_actions"], "edit did not invalidate pre-edit records")
    return modes


def verified_store_resets(path, data):
    protected = set((path.parent / "protected-inventory.stdout").read_text().splitlines())
    protected_traces = json.loads((path.parent / "protected-build-traces.json").read_text())
    names = set()
    for reset in data["store_resets"]:
        name = reset["name"]
        require(Path(name).name == name and name not in names, "duplicate or unsafe cold reset name")
        names.add(name)
        before = set((path.parent / f"{name}-before.stdout").read_text().splitlines())
        after = set((path.parent / f"{name}-after.stdout").read_text().splitlines())
        require(protected <= before and after == protected and reset["selected"] == sorted(before - protected),
                "cold store inventories do not prove an exact reset")
        before_traces = json.loads((path.parent / f"{name}-traces-before.json").read_text())
        after_traces = json.loads((path.parent / f"{name}-traces-after.json").read_text())
        require(trace_deletion_set(protected_traces, before_traces, protected, before) == reset["trace_keys"]
                and after_traces == protected_traces, "cold build-trace maps do not prove an exact reset")
        if reset["trace_keys"]:
            command = data["processes"][name + "-delete-traces"]
            require(command["returncode"] == 0 and command["argv"] == [
                "nix", "store", "build-trace", "delete", "--store", "local", *reset["trace_keys"]],
                "cold build traces were not removed with the scoped supported API")
        if reset["selected"]:
            command = data["processes"][name + "-delete"]
            require(command["returncode"] == 0 and command["argv"] == [
                "nix", "store", "delete", "--store", "daemon", *reset["selected"]],
                "cold paths were not removed with the scoped supported API")


def verified_cold_control(path, data, control):
    require(data.get("cold_control", "legacy") == control, "cold control epochs differ")
    if control == "legacy":
        require("task_control" not in data, "legacy cold data contains an undeclared task control")
        return "legacy"
    require(control == "mapped-debug-paths-v1", "unknown cold control")
    task = data["task_control"]
    helper = store_path(task["helper"])
    expected = {str(file.relative_to(ROOT)): realize.digest(file) for file in [
        ROOT / "benchmarks/cold-debug/control.nix", ROOT / "benchmarks/cold-debug/task-flags.sh", ROOT / "environment/baseline.nix"]}
    require(task["sources"] == expected, "cold task-control sources differ")
    require(task["helper_path"].split(":")[:2] == [helper + "/bin", str(Path(data["binary"]).parent)],
            "cold task helper does not precede the frozen CLI in PATH")
    require((path.parent / "debug-path-helper.stdout").read_text().strip() == helper,
            "cold helper package output differs")
    package = data["processes"]["debug-path-helper"]
    require(package["returncode"] == 0 and package["argv"] == ["nix", "build", "--impure", "--expr", task["expression"],
                                                             "--no-link", "--print-out-paths"], "cold helper package command differs")
    return helper + ":" + json.dumps(expected, sort_keys=True)


def summarize(inputs, destination, kind, rounds, workload_names, binary_sha256, seed=2027, cold_control="legacy"):
    summary = {"schema_version": 1, "stage": "verified paired evaluation summary", "passed": False,
               "kind": kind, "rounds": rounds, "workloads": workload_names, "binary_sha256": binary_sha256,
               "analysis_seed": seed, "analysis_sha256": realize.digest(Path(__file__)), "sources": [], "results": []}
    try:
        scenarios = ["cold-build"] if kind == "cold" else evaluate_planning.CASES
        expected = {(name, case, index) for name in workload_names for case in scenarios for index in range(rounds)}
        manifest = json.loads((ROOT / "benchmarks/workloads.json").read_text())
        workloads = {workload["name"]: workload for workload in manifest["workloads"]}
        pins = json.loads((ROOT / "environment/pins.json").read_text())
        postimages = json.loads((ROOT / "patches/series.json").read_text())["variant_postimages"]["recording"]
        paths = []
        for value in inputs:
            paths.extend(value.rglob("result.json") if value.is_dir() else [value])
        require(len(paths) == len(set(path.resolve() for path in paths)), "duplicate input report")
        grouped, seen, controls = defaultdict(list), set(), set()
        for path in sorted(paths):
            data = json.loads(path.read_text())
            stage = "frozen paired planning evaluation" if kind == "planning" else "frozen paired end-to-end evaluation"
            if data.get("stage") != stage or (kind != "planning" and data.get("cold") != (kind == "cold")):
                continue
            require(data["passed"], f"failed evaluation retained: {path}")
            name = data["workload"]["name"]
            require(name in workload_names and data["workload"] == workloads[name], "unexpected workload or edit probes")
            require(data["binary_sha256"] == binary_sha256 and data["postimages"] == postimages and data["pins"] == pins,
                    "mixed binary, source or environment pins")
            require(data["seed"] == seed and data["environment"]["host"] == "Linux x86_64"
                    and data["environment"]["cpu_quota"] == "400000 100000"
                    and data["resources"]["memory.max"] == "6442450944" and data["resources"]["pids.max"] == "2048",
                    "evaluation machine, seed or resource limits differ")
            rows = verified_rows(path, data, kind)
            if kind == "cold":
                controls.add(verified_cold_control(path, data, cold_control))
                verified_store_resets(path, data)
            for pair in data["pairs"]:
                key = (name, pair["scenario"], pair["round"])
                require(key in expected and key not in seen, f"unexpected or duplicate pair: {key}")
                seen.add(key)
                modes = verified_pair(pair, rows, data, kind)
                if kind == "cold":
                    resets = {reset["name"] for reset in data["store_resets"]}
                    require(all(row["name"] + "-reset" in resets for row in modes.values()), "missing pre-build store reset")
                elif kind == "warm":
                    protected = (path.parent / "protected-inventory.stdout").read_text().splitlines()
                    for row in modes.values():
                        actual = (path.parent / f"{row['name']}-inventory.stdout").read_text().splitlines()
                        require(set(actual) == set(protected), "store-warm inventories differ")
                grouped[(name, pair["scenario"])].append({"round": pair["round"], "source": str(path),
                    "order": [rows[value]["mode"] for value in pair["measured"]], "metadata_bytes": pair["metadata_bytes"],
                    "reset_ns": pair["reset_ns"], "edit_ns": pair["edit_ns"], "cleanup_ns": pair["cleanup_ns"],
                    "modes": modes})
            require({row["name"] for row in rows.values() if row["kind"] == "measured"}
                    == {value for pair in data["pairs"] for value in pair["measured"]}, "unaccounted measured invocation")
            summary["sources"].append({"path": str(path), "sha256": realize.digest(path),
                                       "capture_ns": data["capture_ns"], "harness_sha256": data["harness_sha256"],
                                       "environment": data["environment"], "machine": data["machine"]})
        require(seen == expected, f"incomplete evaluation: missing {sorted(expected - seen)}")
        if kind == "cold":
            require(len(controls) == 1, "cold helper identities differ between shards")
            summary["cold_control"] = cold_control
        for (name, case), observations in sorted(grouped.items()):
            observations.sort(key=lambda row: row["round"])
            pairs = [(row["modes"]["off"]["elapsed_ns"], row["modes"]["reuse"]["elapsed_ns"]) for row in observations]
            result = {"workload": name, "scenario": case, **paired_statistics(pairs, seed),
                      "costs": cost_statistics(observations), "observations": observations}
            threshold = 0.20 if kind == "planning" and case in {"no-op", "implementation-edit"} else None
            if kind != "planning" and case in {"cold-build", "shared-header-edit"}:
                threshold = -0.05
            if threshold is not None:
                result.update(target_minimum_improvement=threshold,
                              median_target_met=result["median_paired_improvement"] + 1e-12 >= threshold)
            summary["results"].append(result)
        summary["passed"] = True
        summary["interpretation"] = "verified coverage and observations; target misses require explanation, not exclusion; RSS excludes independent daemon workers"
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        with destination.open("x") as stream:
            stream.write(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Verify exact paired evaluation coverage and analyze retained raw timings")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--kind", choices=["planning", "warm", "cold"], required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--workloads", nargs="+", choices=["nix", "zstd"], default=["nix", "zstd"])
    parser.add_argument("--expected-binary-sha256", required=True)
    parser.add_argument("--cold-control", choices=["legacy", "mapped-debug-paths-v1"], default="legacy")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.rounds <= 50 or len(set(args.workloads)) != len(args.workloads):
        parser.error("final analysis requires 10–50 pairs and distinct workload names")
    if args.kind != "cold" and args.cold_control != "legacy":
        parser.error("a cold task control cannot label planning or warm evaluation")
    summarize(args.inputs, args.output, args.kind, args.rounds, args.workloads, args.expected_binary_sha256,
              cold_control=args.cold_control)


if __name__ == "__main__":
    main()
