import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_oracle import invoke, digest
from evaluate_builds import collection_set, require_disposable_store
from store_state import build_traces, trace_deletion_set, store_path


def nix_program(program):
    path = Path(program)
    root = Path(*path.parts[:4])
    store_path(str(root))
    relative = path.relative_to(root)
    if not relative.parts or ".." in relative.parts:
        raise RuntimeError("probe program must stay below its store package")
    return "(builtins.storePath " + json.dumps(str(root)) + ") + " + json.dumps("/" + str(relative))


def check(destination):
    report = {"stage": "pinned CA build-trace reset versus path-only collection", "passed": False,
              "harness_sha256": digest(Path(__file__)), "processes": {}, "resets": []}
    env = dict(os.environ, NIX_REMOTE="daemon", LC_ALL="C")

    def command(name, argv, success=True):
        result = invoke(argv, env)
        report["processes"][name] = result
        (destination / f"{name}.stdout").write_text(result["stdout"])
        (destination / f"{name}.stderr").write_text(result["stderr"])
        if success and result["returncode"]:
            raise RuntimeError(f"trace-reset probe command failed: {name}")
        return result

    def paths(name):
        return set(command(name, ["nix", "path-info", "--all", "--store", "daemon"])["stdout"].splitlines())

    try:
        require_disposable_store()
        protected_paths = paths("protected-inventory")
        protected_traces = build_traces()
        (destination / "protected-build-traces.json").write_text(json.dumps(protected_traces, indent=2) + "\n")

        def reset(name, clear_traces):
            before = paths(name + "-before")
            traces = build_traces()
            selected = collection_set(protected_paths, before)
            keys = trace_deletion_set(protected_traces, traces, protected_paths, before)
            (destination / f"{name}-traces-before.json").write_text(json.dumps(traces, indent=2) + "\n")
            if clear_traces and keys:
                command(name + "-delete-traces", ["nix", "store", "build-trace", "delete", "--store", "local", *keys])
            if selected:
                command(name + "-delete-paths", ["nix", "store", "delete", "--store", "daemon", *selected])
            after = paths(name + "-after")
            remaining = build_traces()
            (destination / f"{name}-traces-after.json").write_text(json.dumps(remaining, indent=2) + "\n")
            if after != protected_paths or (clear_traces and remaining != protected_traces):
                raise RuntimeError("the probe did not restore protected paths and traces")
            report["resets"].append({"name": name, "paths": selected, "trace_keys": keys, "clear_traces": clear_traces})
            return remaining

        bash, od = shutil.which("bash"), shutil.which("od")
        if not bash or not od or not bash.startswith("/nix/store/") or not od.startswith("/nix/store/"):
            raise RuntimeError("the probe requires pinned store-backed shell/coreutils")
        shell = '"${od}" -An -N16 -tx1 /dev/urandom > "$out"'
        expression = (
            "let bash = " + nix_program(bash) + "; od = " + nix_program(od)
            + '; in builtins.derivation { name = "nni-trace-probe-' + secrets.token_hex(16)
            + '"; system = "x86_64-linux"; builder = bash; args = [ "-c" ' + json.dumps(shell) + ' ]; '
            + '__contentAddressed = true; outputHashAlgo = "sha256"; outputHashMode = "recursive"; }'
        )
        report["expression"] = expression
        argv = ["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths", "--store", "daemon"]
        first = command("first-build", argv)["stdout"].strip().splitlines()
        if len(first) != 1 or first[0] in protected_paths:
            raise RuntimeError("expected one new probe-owned content-addressed output")
        report["first_output_sha256"] = digest(Path(first[0]))
        remaining = reset("path-only-reset", False)
        if remaining == protected_traces:
            raise RuntimeError("path-only collection unexpectedly removed the probe build trace")
        second = command("expected-conflict", argv, success=False)
        if second["returncode"] == 0 or "already have another one locally" not in second["stderr"]:
            raise RuntimeError("path-only reset did not reproduce the pinned CA conflict")
        reset("complete-reset", True)
        third = command("recovered-build", argv)["stdout"].strip().splitlines()
        if len(third) != 1 or digest(Path(third[0])) == report["first_output_sha256"]:
            raise RuntimeError("the reset did not recover a new nondeterministic output")
        reset("final-reset", True)
        report.update(passed=True, protected_paths=len(protected_paths), protected_traces=len(protected_traces))
        print("trace reset: reproduced the path-only conflict, recovered, preserved all baseline paths/traces", flush=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Verify supported cleanup of only experiment-created CA build traces")
    parser.add_argument("--disposable-store", choices=["daemon"], required=True)
    parser.parse_args()
    destination = ROOT / "results/lifecycle" / f"cold-traces-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    check(destination)


if __name__ == "__main__":
    main()
