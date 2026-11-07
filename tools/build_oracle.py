import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from input_view import InputView


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(argv, env):
    start = time.monotonic_ns()
    result = subprocess.run(argv, env=env, text=True, capture_output=True)
    return {
        "argv": argv, "returncode": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr,
        "elapsed_ns": time.monotonic_ns() - start,
    }


def observe(build, target, result, env):
    if result["returncode"]:
        return {"class": "build-error"}
    path = build / target
    program = invoke([str(path)], env)
    return {
        "class": "success", "program_exit": program["returncode"],
        "sha256": digest(path), "program": program,
    }


def outcome(observation):
    return {key: value for key, value in observation.items() if key != "program"}


def diagnostics(stderr, env):
    paths = set(re.findall(r"/nix/store/[a-z0-9]{32}-[^\s:'\"^]+\.drv", stderr))
    records = []
    for path in sorted(paths):
        shown = invoke(["nix", "derivation", "show", path], env)
        if shown["returncode"] == 0:
            value = json.loads(shown["stdout"])
            for derivation in value.get("derivations", value).values():
                paths.update(derivation.get("inputDrvs", {}))
                for name in derivation.get("inputs", {}).get("drvs", {}):
                    paths.add(name if name.startswith("/") else "/nix/store/" + name)
        records.append(invoke(["nix", "log", path], env))
    queried = {record["argv"][-1] for record in records}
    for path in sorted(paths - queried):
        records.append(invoke(["nix", "log", path], env))
    return records


def path_in(source, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"fixture path escapes its source: {name}")
    path = source / relative
    if not path.parent.resolve().is_relative_to(source):
        raise ValueError(f"fixture parent escapes its source: {name}")
    if path.is_symlink() and not path.resolve().is_relative_to(source):
        raise ValueError(f"fixture symlink escapes its source: {name}")
    return path


def mutate(source, edit):
    preserved = {
        name: (path_in(source, name).stat().st_atime_ns, path_in(source, name).stat().st_mtime_ns)
        for name in edit.get("preserve_mtime", [])
    }
    for name in edit.get("remove", []):
        path_in(source, name).unlink()
    for name, data in edit.get("write", {}).items():
        path = path_in(source, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    for name, target in edit.get("symlink", {}).items():
        path = path_in(source, name)
        if not (path.parent / target).resolve().is_relative_to(source):
            raise ValueError("fixture symlink target escapes its source")
        path.symlink_to(target)
    for name, times in preserved.items():
        os.utime(path_in(source, name), ns=times)


def source_state(source):
    state = {}
    for path in sorted(source.rglob("*")):
        if path.is_file():
            value = {"sha256": digest(path), "mtime_ns": path.stat().st_mtime_ns}
            if path.is_symlink():
                value["symlink"] = str(path.readlink())
            state[str(path.relative_to(source))] = value
    return state


def campaign(binary, fixture_path, destination):
    fixture = json.loads(fixture_path.read_text())
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    env["LC_ALL"] = "C"
    report = {
        "schema_version": 1, "fixture_sha256": digest(fixture_path),
        "binary": str(binary), "binary_sha256": digest(binary),
        "oracle_sha256": digest(Path(__file__)),
        "patch_series": json.loads((ROOT / "patches/series.json").read_text()),
        "pins": json.loads((ROOT / "environment/pins.json").read_text()),
        "compiler": invoke(["cc", "--version"], env),
        "ninja": invoke(["ninja", "--version"], env),
        "daemon": invoke(["nix", "store", "info", "--store", "daemon", "--json"], env),
        "steps": [], "passed": False,
    }
    try:
        view = InputView.create(provenance={"fixture_sha256": digest(fixture_path)})
        report["input_view"] = view.data
        with closing(view), view.lease() as owned:
            source = owned / "source"
            source.mkdir()
            for name in fixture.get("directories", []):
                path_in(source, name).mkdir(parents=True)
            mutate(source, {"write": fixture["files"]})
            actual = owned / "actual"
            reference = owned / "reference"
            for build in [actual, reference]:
                build.mkdir()
                (build / "build.ninja").write_text(fixture["build_ninja"])
            previous = None
            for edit in fixture["edits"]:
                mutate(source, edit)
                if "build_ninja" in edit:
                    for build in [actual, reference]:
                        (build / "build.ninja").write_text(edit["build_ninja"])
                for name in fixture["outputs"]:
                    (reference / name).unlink(missing_ok=True)
                view.validate_links()
                step = {"name": edit["name"], "edit": edit, "source": source_state(source)}
                report["steps"].append(step)
                fresh = invoke(["ninja", "-C", str(reference), "-j1", fixture["target"]], env)
                expected = observe(reference, fixture["target"], fresh, env)
                step.update(reference=fresh, reference_observation=expected)
                if edit.get("stale_probe"):
                    stale = observe(actual, fixture["target"], {"returncode": 0}, env)
                    step["injected_stale_observation"] = stale
                    if outcome(stale) == outcome(expected):
                        raise RuntimeError("oracle did not detect an injected skipped rebuild")
                    step["stale_fault_detected"] = True
                result = invoke([str(binary), "-C", str(actual), fixture["target"]], env)
                observed = observe(actual, fixture["target"], result, env)
                step.update(actual=result, actual_observation=observed)
                if outcome(observed) != outcome(expected):
                    raise RuntimeError(f"fresh-build mismatch: {edit['name']}")
                if edit["expected_exit"] is None:
                    if observed["class"] != "build-error":
                        raise RuntimeError("invalid input produced a successful build")
                    logs = diagnostics(result["stderr"], env)
                    step["compiler_logs"] = logs
                    text = result["stderr"] + "".join(log["stdout"] for log in logs)
                    pattern = re.escape(edit["diagnostic_file"]) + r":\d+:\d+: (?:fatal )?error:"
                    if not re.search(pattern, text):
                        raise RuntimeError("Nix failure did not expose the expected compiler diagnostic")
                    if not re.search(pattern, fresh["stdout"] + fresh["stderr"]):
                        raise RuntimeError("reference failure did not expose the expected compiler diagnostic")
                else:
                    if observed.get("program_exit") != edit["expected_exit"]:
                        raise RuntimeError(f"unexpected fixture behavior: {edit['name']}")
                    if edit.get("unchanged_artifact") and observed["sha256"] != previous:
                        raise RuntimeError("irrelevant edit changed the artifact")
                    previous = observed["sha256"]
                step["passed"] = True
                print(f"{edit['name']}: matched fresh build", flush=True)
            report["passed"] = True
    except (OSError, ValueError, RuntimeError) as error:
        report["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Independent Nix versus fresh Ninja artifact oracle")
    parser.add_argument("--binary", type=Path, default=shutil.which("nix-ninja"))
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/build_edits.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.binary is None:
        parser.error("--binary or nix-ninja in PATH is required")
    binary = Path(args.binary).resolve(strict=True)
    destination = args.output or ROOT / "results/oracle" / f"build-{time.time_ns()}"
    destination.mkdir(parents=True, exist_ok=False)
    campaign(binary, args.fixture, destination)
    print(destination)


if __name__ == "__main__":
    main()
