import argparse
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_oracle import digest, invoke, observe, outcome
from input_view import InputView
import realize
from lifecycle_checks import NINJA


def deletion_set(protected, current, referrers, target):
    protected, current, referrers = set(protected), set(current), set(referrers)
    if not protected <= current:
        raise RuntimeError("the protected store inventory changed before collection")
    if target not in referrers or target in protected or not referrers <= current - protected:
        raise RuntimeError("collection reaches a preexisting or unknown store path")
    if not 1 <= len(referrers) <= 4096:
        raise RuntimeError("collection exceeds the fixture path budget")
    for name in referrers:
        path = Path(name)
        if path.parent != Path("/nix/store") or len(path.name) < 34 or path.name[32] != "-":
            raise RuntimeError("collection path is outside the logical store")
        if any(character not in "0123456789abcdfghijklmnpqrsvwxyz" for character in path.name[:32]):
            raise RuntimeError("collection path has an invalid store hash")
    return sorted(referrers)


def state_summary(cache):
    state = cache / "state.json"
    envelope = json.loads(state.read_text())
    if hashlib.sha256(envelope["payload"].encode()).hexdigest() != envelope["checksum"]:
        raise RuntimeError("published state checksum does not match")
    bundle = json.loads(envelope["payload"])
    return {"bytes": state.stat().st_size, "sha256": digest(state),
            "keys": sorted(bundle["records"]),
            **{name: len(bundle[name]) for name in ["records", "paths", "resolutions", "files"]}}


def check(binary, destination, iterations):
    env = dict(os.environ, NIX_REMOTE="daemon", LC_ALL="C")
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    record = {"schema_version": 1, "stage": "native metadata bounds and owned store-input collection",
              "binary": str(binary), "binary_sha256": digest(binary),
              "harness_sha256": digest(Path(__file__)), "iterations": iterations,
              "pins": json.loads((ROOT / "environment/pins.json").read_text()),
              "patch_series": json.loads((ROOT / "patches/series.json").read_text()),
              "growth": [], "passed": False}
    try:
        if platform.system() != "Linux" or not Path("/.dockerenv").is_file():
            raise RuntimeError("store lifecycle checks require the project container")
        with closing(InputView.create(provenance={"store_lifecycle": record["harness_sha256"]})) as view:
            with ExitStack() as controller:
                controller.enter_context(view.lease())
                source, build, reference = [view.root / name for name in ["source", "build", "reference"]]
                for path in [source, build, reference]:
                    path.mkdir()
                cache = view.control / "discovery"

                def graph(revision):
                    text = NINJA.replace("cc -O2", f"cc -DMETADATA_REVISION={revision} -O2")
                    for path in [build, reference]:
                        (path / "build.ninja").write_text(text)

                def planner(name, tool="drv"):
                    return realize.invoke_planner(binary, view, "app", env, destination,
                                                  name, record, controller, "reuse", tool)

                def compare_artifact(result, expected_exit):
                    for output in ["main.o", "main.o.d", "app"]:
                        (reference / output).unlink(missing_ok=True)
                    fresh = invoke(["ninja", "-C", str(reference), "-j1", "app"], env)
                    expected = observe(reference, "app", fresh, env)
                    observed = observe(build, "app", result, env)
                    if outcome(expected) != outcome(observed) or observed.get("program_exit") != expected_exit:
                        raise RuntimeError("store lifecycle artifact differs from fresh Ninja")
                    return {"reference": fresh, "expected": expected, "observed": observed}

                initial = None
                seen = set()
                for revision in range(iterations):
                    header = f"version-{revision:04d}.h"
                    value = 21 + revision % 7
                    (source / header).write_text(f"#define VALUE {value}\n")
                    (source / "main.c").write_text(f'#include "{header}"\nint main(void) {{ return VALUE; }}\n')
                    graph(revision)
                    cold = planner(f"revision-{revision}-cold")
                    warm = planner(f"revision-{revision}-warm")
                    state = state_summary(cache)
                    row = {"revision": revision, "cold": cold["discovery"], "warm": warm["discovery"], "state": state}
                    record["growth"].append(row)
                    if cold["discovery"]["hits"] or warm["discovery"]["hits"] != 1 or state["records"] != 1:
                        raise RuntimeError("an edited command reused stale state or retained old records")
                    if state["keys"][0] in seen:
                        raise RuntimeError("a changed command kept the same action key")
                    seen.update(state["keys"])
                    if initial is None:
                        initial = state
                        shutil.copyfile(cache / "state.json", destination / "growth-first-state.json")
                    if state["bytes"] > initial["bytes"] + 4096 or any(
                        state[name] > initial[name] + 8 for name in ["paths", "resolutions", "files"]
                    ):
                        raise RuntimeError("retained state grows across replaced action keys")
                    if revision % 16 == 0 or revision == iterations - 1:
                        result = planner(f"revision-{revision}-build", None)
                        row["artifact"] = compare_artifact(result, value)
                        print(f"revision {revision}: one retained record, {state['bytes']} bytes", flush=True)
                shutil.copyfile(cache / "state.json", destination / "growth-last-state.json")

                def store_command(name, arguments):
                    realize.invoke(arguments, env, destination, name, record)
                    return (destination / f"{name}.stdout").read_text().splitlines()

                protected = set(store_command("protected-inventory", ["nix", "path-info", "--all", "--store", "daemon"]))
                fixture = view.control / ("nni-collection-" + secrets.token_hex(16))
                fixture.mkdir()
                (fixture / "marker.h").write_text(secrets.token_hex(32) + "\n")
                added = store_command("input-add", ["nix-store", "--store", "daemon", "--add", str(fixture)])
                if len(added) != 1 or added[0] in protected:
                    raise RuntimeError("expected a new, fixture-owned store input")
                store_input = added[0]
                record["store_input"] = store_input
                (source / "main.c").write_text(
                    f'#if __has_include("{store_input}/marker.h")\n#define VALUE 77\n#else\n#define VALUE 0\n#endif\n'
                    'int main(void) { return VALUE; }\n'
                )
                graph(iterations)
                planner("input-cold")
                present = planner("input-present", None)
                if not present["discovery"]["hits"]:
                    raise RuntimeError("the store-input fixture did not exercise reuse")
                record["input_present"] = compare_artifact(present, 77)
                current = set(store_command("before-collection", ["nix", "path-info", "--all", "--store", "daemon"]))
                referrers = store_command("input-referrers", ["nix-store", "--store", "daemon", "--query", "--referrers-closure", store_input])
                selected = deletion_set(protected, current, referrers, store_input)
                record["collection_paths"] = selected
                print(f"collecting {len(selected)} fixture-owned paths", flush=True)
                store_command("input-collection", ["nix", "store", "delete", "--store", "daemon", *selected])
                after = set(store_command("after-collection", ["nix", "path-info", "--all", "--store", "daemon"]))
                if not protected <= after or store_input in after or not current - after <= set(selected):
                    raise RuntimeError("collection changed the protected store inventory")
                for output in ["main.o", "main.o.d", "app"]:
                    (build / output).unlink(missing_ok=True)
                command = [str(binary), "-C", str(build), "--input-view", str(view.marker),
                           "--discovery-mode", "reuse", "--discovery-cache", str(cache), "app"]
                controller.close()
                try:
                    missing = invoke(command, env)
                finally:
                    controller.enter_context(view.lease())
                record["input_missing"] = missing
                audits = [json.loads(line.removeprefix("NNI_DISCOVERY=")) for line in missing["stderr"].splitlines()
                          if line.startswith("NNI_DISCOVERY=")]
                if len(audits) > 1 or any(audit["hits"] or audit["candidate_mismatches"] for audit in audits):
                    raise RuntimeError("a missing store input authorized reuse")
                if missing["returncode"] == 0:
                    if len(audits) != 1 or not audits[0]["planning_complete"]:
                        raise RuntimeError("a missing-input build omitted its completed audit")
                    record["missing_artifact"] = compare_artifact(missing, 0)
                elif store_input not in missing["stderr"]:
                    raise RuntimeError("missing-input failure did not identify the collected input")
                restored = store_command("input-restore", ["nix-store", "--store", "daemon", "--add", str(fixture)])
                if restored != [store_input]:
                    raise RuntimeError("restoring the same bytes changed the store input path")
                recovered = planner("input-recovery", None)
                record["input_recovery"] = compare_artifact(recovered, 77)
                warm = planner("input-recovery-warm")
                if not warm["discovery"]["hits"]:
                    raise RuntimeError("restored store input did not recover reuse")
                record["final_state"] = state_summary(cache)
                record["input_view"] = view.data
        record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Metadata bounds and collection in the project disposable store")
    parser.add_argument("--binary", type=Path, default=shutil.which("nix-ninja"))
    parser.add_argument("--disposable-store", choices=["daemon"], required=True)
    parser.add_argument("--iterations", type=int, default=64)
    args = parser.parse_args()
    if args.binary is None or not 1 <= args.iterations <= 512:
        parser.error("a planner binary and between 1 and 512 revisions are required")
    destination = ROOT / "results/lifecycle" / f"store-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    check(Path(args.binary).resolve(strict=True), destination, args.iterations)


if __name__ == "__main__":
    main()
