import argparse
import json
from pathlib import Path
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_oracle
import edit_traces
from recording_checks import compare


def write_fixture(path, states, seed):
    path.write_text(json.dumps(edit_traces.fixture(states, seed), indent=2) + "\n")


def reduce_failure(binary, states, seed, mode, failure, destination, attempts):
    if not failure.get("error", "").startswith("fresh-build mismatch:") or not failure["steps"]:
        return {"status": "not an artifact mismatch"}
    states = states[:len(failure["steps"])]
    report = {"status": "bounded reduction", "attempts": [], "original_states": len(states)}

    def reproduces(candidate):
        if len(report["attempts"]) >= attempts:
            return False
        trial = destination / f"trial-{len(report['attempts'])}"
        trial.mkdir()
        fixture = trial / "fixture.json"
        write_fixture(fixture, candidate, seed)
        try:
            build_oracle.campaign(binary, fixture, trial, mode)
        except (OSError, ValueError, RuntimeError):
            pass
        result_file = trial / "result.json"
        outcome = json.loads(result_file.read_text()) if result_file.exists() else {}
        matched = outcome.get("error") == failure["error"]
        report["attempts"].append({"directory": str(trial), "states": len(candidate), "reproduced": matched})
        if matched:
            report["reproducing_trial"] = str(trial)
        return matched

    retained = edit_traces.minimize(states, reproduces)
    write_fixture(destination / "minimized.json", retained, seed)
    (destination / "minimized.states.json").write_text(json.dumps(retained, indent=2) + "\n")
    report["retained_states"] = len(retained)
    return report


def main():
    parser = argparse.ArgumentParser(description="Seeded artifact and plan comparisons against fresh Ninja")
    parser.add_argument("--binary", type=Path, default=shutil.which("nix-ninja"))
    parser.add_argument("--start-seed", type=int, default=2027)
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--edits", type=int, default=16)
    parser.add_argument("--reduce-failure", action="store_true")
    parser.add_argument("--reduction-attempts", type=int, default=16)
    args = parser.parse_args()
    if args.binary is None or args.seeds < 1 or args.edits < len(edit_traces.KINDS) or args.reduction_attempts < 1:
        parser.error(f"a planner binary, positive seed/attempt counts and at least {len(edit_traces.KINDS)} edits are required")
    binary = Path(args.binary).resolve(strict=True)
    destination = ROOT / "results/campaigns" / f"seeded-{time.time_ns()}"
    destination.mkdir(parents=True)
    record = {"schema_version": 1, "stage": "seeded off/record/reuse artifact campaign",
              "modes": ["off", "record", "reuse"],
              "passed": False, "binary": str(binary), "binary_sha256": build_oracle.digest(binary),
              "generator_sha256": build_oracle.digest(Path(edit_traces.__file__)),
              "start_seed": args.start_seed, "seeds": args.seeds, "edits_per_seed": args.edits,
              "completed": []}
    print(destination, flush=True)
    try:
        for seed in range(args.start_seed, args.start_seed + args.seeds):
            states = edit_traces.generate(seed, args.edits)
            fixture = destination / f"seed-{seed}.json"
            write_fixture(fixture, states, seed)
            fixture.with_suffix(".states.json").write_text(json.dumps(states, indent=2) + "\n")
            results = {}
            for mode in ["off", "record", "reuse"]:
                target = destination / f"seed-{seed}-{mode}"
                target.mkdir()
                record["current"] = {"seed": seed, "mode": mode, "directory": str(target)}
                try:
                    build_oracle.campaign(binary, fixture, target, mode)
                except (OSError, ValueError, RuntimeError):
                    result_file = target / "result.json"
                    failure = json.loads(result_file.read_text()) if result_file.exists() else {}
                    if args.reduce_failure and failure:
                        reduction = destination / "reduction"
                        reduction.mkdir()
                        record["reduction"] = reduce_failure(binary, states, seed, mode, failure, reduction, args.reduction_attempts)
                    raise
                results[mode] = json.loads((target / "result.json").read_text())
            compare(results["off"], results["record"])
            compare(results["off"], results["reuse"])
            record["completed"].append({"seed": seed, "states_per_mode": len(states),
                                         "faults_per_mode": sum(step.get("stale_fault_detected", False) for step in results["record"]["steps"])})
        record.pop("current", None)
        record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
