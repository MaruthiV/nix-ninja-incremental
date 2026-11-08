import argparse
import hashlib
import json
from pathlib import Path
import random

from evaluate_baselines import compatibility, comparison_statistics
from evaluation_summary import require
from planning import discovery_report
import realize


ROOT = Path(__file__).resolve().parents[1]


def planner_command(data, binary, cache=None, discovery=None):
    view = Path(data["input_view"]["root"])
    argv = [binary, "-C", str(view / "build"), "-j4", "-t", "drv"]
    if discovery is not None:
        argv += ["--input-view", str(view.parent / "view.json"), "--discovery-mode", discovery,
                 "--discovery-cache", str(cache)]
    return argv + [data["workload"]["target"]]


def verify_measurement(path, data, probes, rounds, seed):
    names = [name for name in ["upstream", "corrected"] if probes[name]["comparable"]]
    if not names:
        require("measurement" not in data and not data.get("comparisons") and not data.get("reuse_comparisons"),
                "unmatched baselines cannot supply timing ratios")
        return {}, {}
    measurement = data["measurement"]
    require(measurement == json.loads((path.parent / "planning/result.json").read_text()), "nested measurement differs")
    require(measurement["passed"] and measurement["repetitions"] == rounds and measurement["seed"] == seed
            and measurement["jobs"] == 4 and measurement["pins"] == data["pins"]
            and measurement["view"] == data["input_view"], "measurement protocol or input view differs")
    capture = json.loads((path.parent / "capture.json").read_text())
    require(measurement["capture"] == capture and capture["passed"]
            and capture["input_view"] == data["input_view"], "captured input view differs")
    reuse = "reuse_comparisons" in data
    modes = ["current", *names, *(["reuse"] if reuse else [])]
    require(list(measurement["modes"]) == modes, "unexpected baseline or current mode")
    require(set(data["comparisons"]) == set(names)
            and (not reuse or set(data["reuse_comparisons"]) == set(names)), "missing or unmatched comparison")
    for name, mode in measurement["modes"].items():
        expected = data if name in {"current", "reuse"} else data["baseline_binaries"][name]
        discovery = "off" if name == "current" else "reuse" if name == "reuse" else None
        require(mode["binary"] == expected["binary"] and mode["sha256"] == expected["binary_sha256"]
                and mode["profile"] is False and mode.get("discovery") == discovery, "timed binary identity or mode differs")
    expected_order = [(name, -1, True) for name in modes]
    rng = random.Random(seed)
    for index in range(rounds):
        order = modes.copy()
        rng.shuffle(order)
        expected_order.extend((name, index, False) for name in order)
    runs = measurement["runs"]
    require([(row["mode"], row["round"], row["warmup"]) for row in runs] == expected_order,
            "missing, duplicate or out-of-order invocation")
    initial = None
    for row in runs:
        name = row["mode"]
        stem = f"{'warmup' if row['warmup'] else row['round']}-{name}"
        raw = (path.parent / "planning" / f"{stem}-stdout.json").read_text()
        plan = hashlib.sha256(json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        require(row["returncode"] == 0 and row["elapsed_ns"] > 0
                and plan == row["derivation_sha256"] == probes["current"]["plan_sha256"], "failed or changed timed plan")
        discovery = "off" if name == "current" else "reuse" if name == "reuse" else None
        argv = planner_command(data, measurement["modes"][name]["binary"], Path(measurement["cache_root"]) / name, discovery)
        require(row["argv"] == argv, "timed argv differs from the declared workload or mode")
        if discovery is not None:
            audit = discovery_report((path.parent / "planning" / f"{stem}-stderr.log").read_text(), discovery)
            require(row["discovery"] == audit, "raw discovery audit differs")
            if name == "reuse":
                if row["warmup"]:
                    require(audit["hits"] == 0 and audit["recorded_actions"] > 0, "reuse metadata was not initialized cold")
                    initial = audit["recorded_actions"]
                else:
                    require(audit["hits"] == initial, "warm reuse omitted eligible hits")
    comparisons, reuse_comparisons = {}, {}
    for name in names:
        for target, key, label in [("current", comparisons, "off"), *([("reuse", reuse_comparisons, "reuse")] if reuse else [])]:
            pairs = []
            for index in range(rounds):
                group = {row["mode"]: row["elapsed_ns"] for row in runs if not row["warmup"] and row["round"] == index}
                pairs.append((group[name], group[target]))
            computed = comparison_statistics(pairs, current_mode=label)
            stored = dict(data["comparisons" if target == "current" else "reuse_comparisons"][name])
            if target == "current":
                stored.setdefault("current_mode", "off")
            require(computed == stored, "stored statistics differ from raw pairs")
            key[name] = computed
    return comparisons, reuse_comparisons


def verify(path, binary_sha256, rounds, seed):
    data = json.loads(path.read_text())
    require(data["passed"] and data["rounds"] == rounds and data["seed"] == seed, "failed or changed baseline protocol")
    require(data["scenario"] == "no-op" and data["compiler_cache"] == "disabled", "scenario or compiler-cache control differs")
    pins = json.loads((ROOT / "environment/pins.json").read_text())
    series = json.loads((ROOT / "patches/series.json").read_text())["variant_postimages"]
    workloads = {row["name"]: row for row in json.loads((ROOT / "benchmarks/workloads.json").read_text())["workloads"]}
    require(data["workload"] == workloads[data["workload"]["name"]] and data["pins"] == pins, "workload or pins differ")
    require(data["input_view"]["provenance"]["workload"] == data["workload"], "input-view workload differs")
    require(data["binary_sha256"] == binary_sha256 and data["postimages"] == series["recording"], "binary or source identity differs")
    require(data["baseline_binaries"]["corrected"]["postimages"] == series["corrected-embed"], "corrected baseline source differs")
    upstream = data["baseline_binaries"]["upstream"]
    require(upstream["revision"] == pins["upstream"]["revision"] and upstream["working_tree"] == "clean", "upstream source differs")
    require(data["environment"]["host"] == "Linux x86_64" and data["environment"]["cpu_quota"] == "400000 100000"
            and data["resources"]["cpu.max"] == "400000 100000" and data["resources"]["memory.max"] == "6442450944"
            and data["resources"]["pids.max"] == "2048", "machine limits differ")
    require(data["environment"]["pins"] == pins and data["environment"]["daemon"]["version"] == pins["runtime"]["version"],
            "runtime environment differs from its pins")
    require(set(data["compatibility"]) == {"current", "upstream", "corrected"}, "missing compatibility probe")
    reference = realize.plan_digest(path.parent, "compatibility-current")
    probes = {}
    for name, row in data["compatibility"].items():
        require(row["name"] == "compatibility-" + name and row["elapsed_ns"] > 0, "invalid compatibility probe")
        computed = compatibility(row, path.parent, reference)
        require(all(row[key] == value for key, value in computed.items()), "raw compatibility outcome differs")
        expected = data if name == "current" else data["baseline_binaries"][name]
        argv = planner_command(data, expected["binary"], Path(data["input_view"]["root"]).parent / "baseline-current",
                               "off" if name == "current" else None)
        require(row["argv"] == argv, "compatibility argv differs")
        if name == "current":
            require(row["returncode"] == 0 and computed["comparable"], "current compatibility probe failed")
            audit = discovery_report((path.parent / "compatibility-current.stderr").read_text(), "off")
            require(row["discovery"] == audit, "raw compatibility audit differs")
        probes[name] = {**computed, "returncode": row["returncode"]}
    comparisons, reuse_comparisons = verify_measurement(path, data, probes, rounds, seed)
    raw = sorted(path.parent.glob("compatibility-*.stdout")) + sorted(path.parent.glob("compatibility-*.stderr"))
    raw += sorted((path.parent / "planning").glob("*-stdout.json")) + sorted((path.parent / "planning").glob("*-stderr.log"))
    return {"workload": data["workload"]["name"], "source": str(path), "source_sha256": realize.digest(path),
            "harness_sha256": data["harness_sha256"], "compatibility": probes,
            "comparisons": comparisons, "reuse_comparisons": reuse_comparisons,
            "raw_sources": [{"path": str(file), "sha256": realize.digest(file)} for file in raw]}


def summarize(inputs, destination, workload_names, binary_sha256, rounds=10, seed=2027):
    summary = {"schema_version": 1, "stage": "verified contextual baseline summary", "passed": False,
               "binary_sha256": binary_sha256, "rounds": rounds, "seed": seed,
               "analysis_sha256": realize.digest(Path(__file__)), "results": []}
    try:
        paths = []
        for value in inputs:
            paths.extend(value.rglob("result.json") if value.is_dir() else [value])
        require(len(paths) == len(set(path.resolve() for path in paths)), "duplicate input report")
        seen = set()
        for path in sorted(paths):
            data = json.loads(path.read_text())
            if data.get("stage") != "native contextual baseline overhead":
                continue
            name = data["workload"]["name"]
            require(name in workload_names and name not in seen, "unexpected or duplicate workload calibration")
            seen.add(name)
            summary["results"].append(verify(path, binary_sha256, rounds, seed))
        require(seen == set(workload_names), "incomplete contextual baseline coverage")
        summary["passed"] = True
        summary["interpretation"] = "equivalent-plan no-op comparisons only; exclusions and same-binary gains are distinct from net baseline effects"
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        with destination.open("x") as stream:
            stream.write(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Verify matched no-op baseline coverage, raw plans, audits and paired overhead")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--workloads", nargs="+", choices=["nix", "zstd"], default=["nix", "zstd"])
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--expected-binary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.rounds <= 50 or len(set(args.workloads)) != len(args.workloads):
        parser.error("final analysis requires 10–50 rounds and distinct workloads")
    summarize(args.inputs, args.output, args.workloads, args.expected_binary_sha256, args.rounds)


if __name__ == "__main__":
    main()
