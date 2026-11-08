import argparse
import json
from pathlib import Path
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_oracle


def compare(disabled, recording):
    if not disabled["passed"] or not recording["passed"]:
        raise RuntimeError("an artifact campaign did not pass")
    for reference, observed in zip(disabled["steps"], recording["steps"], strict=True):
        if reference["name"] != observed["name"]:
            raise RuntimeError("edit sequences differ")
        if build_oracle.outcome(reference["actual_observation"]) != build_oracle.outcome(observed["actual_observation"]):
            raise RuntimeError(f"recording changed an artifact: {reference['name']}")
        if reference.get("plan_sha256") != observed.get("plan_sha256"):
            raise RuntimeError(f"recording changed a derivation: {reference['name']}")


def main():
    parser = argparse.ArgumentParser(description="Discovery modes versus fresh independent artifacts")
    parser.add_argument("--binary", type=Path, default=shutil.which("nix-ninja"))
    args = parser.parse_args()
    if args.binary is None:
        parser.error("--binary or nix-ninja in PATH is required")
    binary = Path(args.binary).resolve(strict=True)
    destination = ROOT / "results/recording" / f"check-{time.time_ns()}"
    destination.mkdir(parents=True)
    record = {"schema_version": 1, "stage": "off/record/reuse artifact and plan correctness", "passed": False,
              "modes": ["off", "record", "reuse"],
              "binary": str(binary), "binary_sha256": build_oracle.digest(binary), "fixtures": []}
    print(destination, flush=True)
    try:
        for name in ["build_edits.json", "alias_build_edits.json", "presence_build_edits.json"]:
            fixture = ROOT / "tests/fixtures" / name
            results = {}
            for mode in ["off", "record", "reuse"]:
                target = destination / f"{fixture.stem}-{mode}"
                target.mkdir()
                build_oracle.campaign(binary, fixture, target, mode)
                results[mode] = json.loads((target / "result.json").read_text())
            compare(results["off"], results["record"])
            compare(results["off"], results["reuse"])
            record["fixtures"].append({"name": name, "steps_per_mode": len(results["record"]["steps"]),
                                       "validated_candidates": sum(step["discovery"]["validated_candidates"] for step in results["record"]["steps"]),
                                       "reuse_hits": sum(step["discovery"]["hits"] + step.get("warm_discovery", {}).get("hits", 0)
                                                         for step in results["reuse"]["steps"])})
        record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
