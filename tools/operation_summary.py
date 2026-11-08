import argparse
import json
from pathlib import Path

import evaluate_operations as operations
import evaluate_planning
from evaluation_summary import require, verified_rows
import operation_profile
import realize


ROOT = Path(__file__).resolve().parents[1]


def verify(path, data, workloads):
    manifest, patch, postimages = operation_profile.configuration()
    name = data["workload"]["name"]
    require(name in workloads and data["workload"] == workloads[name], "operation-count workload differs")
    require(data["passed"] and data["rounds"] == 1 and data["seed"] == 2027, "incomplete operation-count diagnostic")
    require(data["pins"] == json.loads((ROOT / "environment/pins.json").read_text()), "operation-count pins differ")
    require(data["patch_series"] == json.loads((ROOT / "patches/series.json").read_text())
            and data["postimages"] == operation_profile.variants.SERIES["variant_postimages"]["recording"]
            and data["diagnostic_postimages"] == postimages, "operation-count source identity differs")
    require(data["operation_manifest"] == manifest
            and json.loads((path.parent / "operation-manifest.json").read_text()) == manifest
            and realize.digest(path.parent / "instrumentation.patch") == realize.digest(patch), "operation-count patch differs")
    require(data["harness_sha256"] == realize.digest(Path(evaluate_planning.__file__))
            and data["operation_driver_sha256"] == realize.digest(Path(operations.__file__)), "operation-count driver differs")
    require(data["binary_sha256"] == manifest["reference_binary_sha256"]
            and data["diagnostic_binary_sha256"] != data["binary_sha256"], "operation-count binary labels differ")
    require(data["environment"]["host"] == "Linux x86_64"
            and data["environment"]["cpu_quota"] == "400000 100000"
            and data["resources"]["cpu.max"] == "400000 100000"
            and data["resources"]["memory.max"] == "6442450944"
            and data["resources"]["pids.max"] == "2048"
            and data["environment"]["pins"] == data["pins"]
            and data["environment"]["daemon"]["version"] == data["pins"]["runtime"]["version"],
            "operation-count machine or runtime differs")
    frozen = Path(data["binary"])
    diagnostic = Path(data["diagnostic_binary"])
    helpers = data["helper_path"].split(":")
    require(helpers[0] == str(frozen.parent) and str(diagnostic.parent) not in helpers,
            "diagnostic package leaked into the common helper PATH")
    require((path.parent / "package.stdout").read_text().strip() == str(frozen.parent.parent)
            and (path.parent / "profile-package.stdout").read_text().strip() == str(diagnostic.parent.parent),
            "operation-count package records differ")
    rows = verified_rows(path, data, "planning")
    require(len(rows) == 28, "operation-count run coverage differs")
    root = Path(data["input_view"]["root"])
    for row in rows.values():
        require(row["variant"] in {"frozen", "profiled"} and row["mode"] in {"off", "reuse"}, "unknown diagnostic label")
        argv = row["argv"]
        cache = Path(argv[argv.index("--discovery-cache") + 1])
        require(cache.is_relative_to(root.parent / "operation-state") and ".." not in cache.parts,
                "operation-count metadata is outside its owned control directory")
        binary = frozen if row["variant"] == "frozen" else diagnostic
        require(argv == [str(binary), "-C", str(root / "build"), "-j4", "-t", "drv", "--input-view",
                         str(root.parent / "view.json"), "--discovery-mode", row["mode"],
                         "--discovery-cache", str(cache), data["workload"]["target"]], "operation-count argv differs")
        if row["variant"] == "profiled":
            file = row["name"] + "-profile.json"
            require(row["profile_file"] == file, "unsafe or mismatched operation profile name")
            raw = json.loads((path.parent / file).read_text())
            require(operations.profile_counts(raw, root / "build") == row["counts"], "raw operation counters differ")
        else:
            require("profile_file" not in row and "counts" not in row, "frozen binary was labelled instrumented")
    require(len(data["pairs"]) == 4 and {pair["scenario"] for pair in data["pairs"]} == set(evaluate_planning.CASES),
            "operation-count scenario coverage differs")
    results, referenced = [], set()
    for pair in data["pairs"]:
        require(pair["passed"], "failed diagnostic pair")
        measured = operations.validate_pair(pair, rows)
        case = pair["scenario"]
        for run in pair["warmups"]:
            require(run not in referenced and rows[run]["kind"] == "metadata-warmup"
                    and rows[run]["input_state"] == data["original_source"], "metadata was not warmed before the edit")
            referenced.add(run)
        probes = data["workload"]["content_probes"]
        expected = {"implementation-edit": {probes["implementation"]}, "shared-header-edit": {probes["shared_header"]},
                    "irrelevant-edit": {".nni-irrelevant.txt"}}.get(case, set())
        for row in measured.values():
            current, original = row["input_state"], data["original_source"]
            require(row["name"] not in referenced and row["kind"] == "diagnostic" and row["scenario"] == case,
                    "diagnostic pair references another scenario")
            require(set(current) == set(original) and {key for key in original if current[key] != original[key]} == expected,
                    "operation-count edit differs from its declaration")
            referenced.add(row["name"])
        results.append({"workload": name, "scenario": case,
                        "counts": {mode: measured[("profiled", mode)]["counts"] for mode in ["off", "reuse"]},
                        "discovery": {mode: {key: measured[("profiled", mode)]["discovery"][key] for key in ["calls", "hits", "scans"]}
                                      for mode in ["off", "reuse"]}})
    prewarm = [row for row in rows.values() if row["kind"] == "store-warmup"]
    require(len(prewarm) == 4 and {row["scenario"] for row in prewarm} == set(evaluate_planning.CASES)
            and all(row["variant"] == "frozen" and row["mode"] == "off" for row in prewarm)
            and referenced | {row["name"] for row in prewarm} == set(rows), "unclassified operation-count observations")
    return results


def main():
    parser = argparse.ArgumentParser(description="Verify raw diagnostic plans, profiles and source identities before reporting API counts")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--workload", action="append", choices=["nix", "zstd"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workloads = {value["name"]: value for value in json.loads((ROOT / "benchmarks/workloads.json").read_text())["workloads"]}
    requested = set(args.workload or workloads)
    paths = [path for value in args.inputs for path in (value.rglob("result.json") if value.is_dir() else [value])]
    require(len(paths) == len(set(path.resolve() for path in paths)), "duplicate operation-count input")
    sources, results, seen = [], [], set()
    for path in sorted(paths):
        data = json.loads(path.read_text())
        if data.get("stage") != operations.STAGE:
            continue
        name = data["workload"]["name"]
        require(name in requested and name not in seen, "unexpected or duplicate operation-count workload")
        results.extend(verify(path, data, workloads))
        sources.append({"path": str(path), "sha256": realize.digest(path)})
        seen.add(name)
    require(seen == requested, "missing operation-count workload")
    summary = {"schema_version": 1, "passed": True, "stage": "verified supplementary planner API counts",
               "sources": sources, "results": results, "count_semantics": operation_profile.configuration()[0]["count_semantics"],
               "duration_semantics": operation_profile.configuration()[0]["duration_semantics"], "speedup_claim": "none"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"verified {len(results)} workload/scenario count comparisons; no new timing samples")


if __name__ == "__main__":
    main()
