import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import build_oracle
import edit_traces
from planning import discovery_report


ROOT = Path(__file__).resolve().parents[1]
MODES = ["off", "record", "reuse"]


def read(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"report exceeds the input budget: {path}")
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verified_source(step, snapshot, previous):
    source = step["source"]
    expected = {name: {"sha256": hashlib.sha256(value.encode()).hexdigest()}
                for name, value in snapshot["files"].items()}
    for name, target in snapshot["symlink"].items():
        target_name = str(Path(name).parent / target)
        require(target_name in expected, f"unknown fixture alias target: {target_name}")
        expected[name] = {"sha256": expected[target_name]["sha256"], "symlink": target}
    require(set(source) == set(expected), f"source file set differs: {step['name']}")
    for name, values in expected.items():
        require({key: value for key, value in source[name].items() if key != "mtime_ns"} == values,
                f"source bytes or aliases differ: {step['name']}/{name}")
    for name in step["edit"].get("preserve_mtime", []):
        require(previous is not None and source[name]["mtime_ns"] == previous[name]["mtime_ns"],
                f"mtime was not preserved: {step['name']}/{name}")
    return source


def verified_steps(data, fixture, mode, snapshots):
    require(data["passed"] is True and data["schema_version"] == 1, "campaign did not pass")
    require(data["discovery_mode"] == mode, "discovery mode differs from its directory")
    require(len(data["steps"]) == len(fixture["edits"]), "edit states are missing or duplicated")
    evidence = []
    counts = {"states": 0, "warm_plans": 0, "fault_detections": 0, "reuse_hits": 0}
    previous = None
    previous_source = None
    for step, edit, snapshot in zip(data["steps"], fixture["edits"], snapshots, strict=True):
        name = edit["name"]
        require(step["passed"] is True and step["name"] == name and step["edit"] == edit, f"edit identity changed: {name}")
        previous_source = verified_source(step, snapshot, previous_source)
        expected, actual = step["reference_observation"], step["actual_observation"]
        require(build_oracle.outcome(expected) == build_oracle.outcome(actual), f"fresh artifact differs: {name}")
        audit = discovery_report(step["actual"]["stderr"], mode)
        require(audit == step["discovery"], f"raw discovery report differs: {name}")
        if mode == "reuse" and counts["states"] == 0:
            require(audit["hits"] == 0, "cold campaign reports a hit")
        counts["states"] += 1
        counts["reuse_hits"] += audit["hits"]
        if edit.get("stale_probe"):
            require(step.get("stale_fault_detected") is True, f"injected fault was not detected: {name}")
            require(build_oracle.outcome(step["injected_stale_observation"]) != build_oracle.outcome(expected),
                    f"injected fault is indistinguishable from fresh output: {name}")
            counts["fault_detections"] += 1
        if edit["expected_exit"] is None:
            require(actual["class"] == "build-error" and step["actual"]["returncode"] != 0
                    and step["reference"]["returncode"] != 0, f"failed compilation became successful: {name}")
            pattern = re.escape(edit["diagnostic_file"]) + r":\d+:\d+: (?:fatal )?error:"
            reference = step["reference"]["stdout"] + step["reference"]["stderr"]
            actual_text = step["actual"]["stderr"] + "".join(log["stdout"] for log in step["compiler_logs"])
            require(re.search(pattern, reference) and re.search(pattern, actual_text),
                    f"compiler error evidence is missing: {name}")
            require("plan_sha256" not in step and "warm_discovery" not in step,
                    f"failed compilation has a successful warm probe: {name}")
        else:
            require(actual["class"] == "success" and actual["program_exit"] == edit["expected_exit"]
                    and step["actual"]["returncode"] == 0 and step["reference"]["returncode"] == 0,
                    f"unexpected program behavior: {name}")
            for observation in [actual, expected]:
                require(re.fullmatch(r"[0-9a-f]{64}", observation["sha256"]), f"invalid artifact digest: {name}")
                require(observation["program"]["returncode"] == observation["program_exit"],
                        f"raw program result differs: {name}")
            if edit.get("unchanged_artifact"):
                require(actual["sha256"] == previous, f"irrelevant edit changed the artifact: {name}")
            previous = actual["sha256"]
            probe = step["planning_probe"]
            require(probe["returncode"] == 0, f"warm planning failed: {name}")
            warm = discovery_report(probe["stderr"], mode)
            require(warm == step["warm_discovery"], f"raw warm report differs: {name}")
            if mode == "reuse":
                require(warm["hits"] > 0, f"warm probe did not reuse discovery: {name}")
            if mode == "record":
                require(warm["validated_candidates"] > 0, f"warm probe did not audit discovery: {name}")
            encoded = json.dumps(json.loads(probe["stdout"]), sort_keys=True, separators=(",", ":")).encode()
            require(hashlib.sha256(encoded).hexdigest() == step["plan_sha256"], f"raw plan digest differs: {name}")
            counts["warm_plans"] += 1
            counts["reuse_hits"] += warm["hits"]
        evidence.append({"name": name, "artifact": build_oracle.outcome(actual), "plan": step.get("plan_sha256")})
    return evidence, counts


def summarize(inputs, destination, start_seed, seeds, edits, binary_sha256=None):
    report = {"schema_version": 1, "stage": "verified seeded campaign coverage; no performance claim",
              "passed": False, "start_seed": start_seed, "seeds": seeds, "edits_per_seed": edits,
              "modes": MODES, "verifier_sha256": digest(Path(__file__)), "campaigns": [], "verified": [],
              "totals": {"states": 0, "warm_plans": 0, "fault_detections": 0, "reuse_hits": 0}}
    try:
        require(seeds > 0 and edits >= len(edit_traces.KINDS), "invalid expected campaign size")
        pins = read(ROOT / "environment/pins.json")
        series = read(ROOT / "patches/series.json")
        generator = digest(Path(edit_traces.__file__))
        oracle = digest(Path(build_oracle.__file__))
        required = set(range(start_seed, start_seed + seeds))
        seen, sources = set(), set()
        tools = None
        for supplied in inputs:
            supplied = supplied.resolve(strict=True)
            paths = [supplied] if supplied.is_file() else sorted(supplied.rglob("result.json"))
            found = False
            for path in paths:
                top = read(path)
                if top.get("stage") != "seeded off/record/reuse artifact campaign":
                    continue
                found = True
                require(path not in sources, "duplicate campaign input")
                sources.add(path)
                report["campaigns"].append({"path": str(path), "sha256": digest(path)})
                require(top["passed"] is True and top["schema_version"] == 1, f"campaign failed: {top.get('error', path)}")
                require(top["modes"] == MODES and top["edits_per_seed"] == edits, "campaign mode/edit contract differs")
                require(top["generator_sha256"] == generator, "campaign generator revision differs")
                if binary_sha256 is None:
                    binary_sha256 = top["binary_sha256"]
                require(re.fullmatch(r"[0-9a-f]{64}", binary_sha256), "invalid binary digest")
                require(top["binary_sha256"] == binary_sha256, "campaign binary identity differs")
                declared = list(range(top["start_seed"], top["start_seed"] + top["seeds"]))
                require([row["seed"] for row in top["completed"]] == declared, "declared campaign seeds are incomplete or duplicated")
                require(set(declared) <= required and not seen.intersection(declared), "campaign seeds overlap or exceed the requested range")
                for completion in top["completed"]:
                    seed = completion["seed"]
                    states = edit_traces.generate(seed, edits)
                    states_path = path.parent / f"seed-{seed}.states.json"
                    fixture_path = path.parent / f"seed-{seed}.json"
                    require(read(states_path) == states, f"seed {seed}: recorded snapshots differ from the generator")
                    fixture = read(fixture_path)
                    require(fixture == edit_traces.fixture(states, seed), f"seed {seed}: fixture differs from recorded snapshots")
                    require(completion["states_per_mode"] == edits + 1, f"seed {seed}: incorrect state count")
                    row = {"seed": seed, "fixture_sha256": digest(fixture_path), "states_sha256": digest(states_path), "cases": []}
                    reference = None
                    for mode in MODES:
                        case_path = path.parent / f"seed-{seed}-{mode}/result.json"
                        case = read(case_path)
                        require(case["binary_sha256"] == binary_sha256 and case["oracle_sha256"] == oracle,
                                f"seed {seed}/{mode}: code identity differs")
                        require(case["pins"] == pins and case["patch_series"] == series,
                                f"seed {seed}/{mode}: source/environment pins differ")
                        require(case["fixture_sha256"] == row["fixture_sha256"], f"seed {seed}/{mode}: fixture digest differs")
                        require(all(case[name]["returncode"] == 0 for name in ["compiler", "ninja", "daemon"]),
                                f"seed {seed}/{mode}: tool identity query failed")
                        daemon = json.loads(case["daemon"]["stdout"])
                        require(daemon["url"] == "daemon" and daemon["version"] == pins["runtime"]["version"],
                                f"seed {seed}/{mode}: daemon version differs")
                        identity = {"compiler": case["compiler"]["stdout"].splitlines()[0],
                                    "ninja": case["ninja"]["stdout"].strip(), "daemon_version": daemon["version"]}
                        if tools is None:
                            tools = identity
                        require(identity == tools, f"seed {seed}/{mode}: compiler or Ninja version differs")
                        try:
                            evidence, counts = verified_steps(case, fixture, mode, states)
                        except (ValueError, KeyError, TypeError) as error:
                            raise ValueError(f"seed {seed}/{mode}: {error}") from error
                        if reference is None:
                            reference = evidence
                        require(evidence == reference, f"seed {seed}/{mode}: cross-mode artifacts or plans differ")
                        require(counts["fault_detections"] == completion["faults_per_mode"], f"seed {seed}: fault coverage differs")
                        row["cases"].append({"mode": mode, "path": str(case_path), "sha256": digest(case_path), **counts})
                        for name, value in counts.items():
                            report["totals"][name] += value
                    report["verified"].append(row)
                    seen.add(seed)
            require(found, f"input contains no all-mode campaign: {supplied}")
        require(seen == required, f"missing expected seeds: {sorted(required - seen)}")
        require(report["totals"]["states"] == seeds * (edits + 1) * len(MODES), "aggregate state count differs")
        report.update(binary_sha256=binary_sha256, generator_sha256=generator, oracle_sha256=oracle,
                      tools=tools, pins=pins, patch_series=series, passed=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as output:
            output.write(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="Verify complete seeded native campaign artifacts")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--start-seed", type=int, default=2027)
    parser.add_argument("--seeds", type=int, required=True)
    parser.add_argument("--edits", type=int, required=True)
    parser.add_argument("--binary-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = summarize(args.inputs, args.output, args.start_seed, args.seeds, args.edits, args.binary_sha256)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"verified {len(result['verified'])} seeds and {result['totals']['states']} artifact states")


if __name__ == "__main__":
    main()
