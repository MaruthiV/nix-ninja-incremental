import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import time

import planning
import realize
import workloads


ROOT = Path(__file__).resolve().parents[1]


def snapshot_unsupported(view, coverage, destination):
    files = {}
    for run in coverage:
        files.update(run.get("unsupported_files", {}))
    samples = destination / "unsupported"
    samples.mkdir()
    records = []
    retained = 0
    for index, (name, reason) in enumerate(sorted(files.items())):
        record = {"path": name, "reason": reason}
        records.append(record)
        path = Path(name)
        try:
            if view._resolve_stable(path) != path or not path.is_file():
                raise ValueError("diagnostic source is not a canonical stable file")
            size = path.stat().st_size
            record["bytes"] = size
            if size > 1024 * 1024 or retained + size > 4 * 1024 * 1024:
                record["omitted"] = "source snapshot budget"
                continue
            target = samples / f"source-{index}"
            shutil.copyfile(path, target)
            target.chmod(0o644)
            record.update(snapshot=str(target.relative_to(destination)), sha256=realize.digest(target))
            retained += size
        except (OSError, ValueError) as error:
            record["diagnostic_error"] = str(error)
    return records


def audit(workload, destination):
    record = {"schema_version": 1, "passed": False, "workload": workload,
              "stage": "record-only eligibility and derivation audit; no reuse or speedup"}
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    try:
        source, postimages = realize.snapshot_variant("recording", destination)
        record.update(postimages=postimages)
        realize.invoke(["nix", "build", "--impure", "--expr", realize.expression(source),
                        "--no-link", "--print-out-paths"], env, destination, "package", record)
        paths = (destination / "package.stdout").read_text().strip().splitlines()
        if len(paths) != 1 or not paths[0].startswith("/nix/store/"):
            raise ValueError("expected one CLI package in the Nix store")
        binary = Path(paths[0]) / "bin/nix-ninja"
        record.update(binary=str(binary), binary_sha256=realize.digest(binary))
        with closing(workloads.capture(workload)) as view:
            with view.lease():
                for name in ["capture.json", "configure.log"]:
                    shutil.copyfile(view.control / name, destination / name)
            report = planning.measure(SimpleNamespace(
                view=view.marker, output=destination / "planning",
                binary=[f"off={binary}", f"record={binary}"], profile=[],
                discovery=["off=off", "record=record"], jobs=4, repetitions=1, seed=2027,
            ))
            record["coverage"] = [{"warmup": run["warmup"], **run["discovery"]}
                                  for run in report["runs"] if run["mode"] == "record"]
            with view.lease():
                record["unsupported_samples"] = snapshot_unsupported(view, record["coverage"], destination)
                state = Path(report["cache_root"]) / "record/state.json"
                snapshot = destination / "recorded-state.json"
                shutil.copyfile(state, snapshot)
                snapshot.chmod(0o644)
                record["state_sha256"] = realize.digest(snapshot)
            record["input_view"] = view.data
        record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Real-workload recorded dependency eligibility audit")
    parser.add_argument("workload", choices=[value["name"] for value in workloads.WORKLOADS])
    args = parser.parse_args()
    workload = next(value for value in workloads.WORKLOADS if value["name"] == args.workload)
    destination = ROOT / "results/eligibility" / f"{args.workload}-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    audit(workload, destination)


if __name__ == "__main__":
    main()
