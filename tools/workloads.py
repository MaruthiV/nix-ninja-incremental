import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = json.loads((ROOT / "benchmarks/workloads.json").read_text())["workloads"]


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv):
    return run(argv, stdout=subprocess.PIPE).stdout.strip()


def prepare(workload):
    source = ROOT / ".work" / workload["name"]
    pin = workload["source"]
    if not source.exists():
        source.parent.mkdir(exist_ok=True, parents=True)
        run(["git", "clone", "--no-checkout", pin["url"], str(source)])
        run(["git", "-C", str(source), "checkout", "--detach", pin["revision"]])
    actual = output(["git", "-C", str(source), "rev-parse", "HEAD"])
    if actual != pin["revision"]:
        raise ValueError(f"{workload['name']} source revision mismatch: {actual}")
    if output(["git", "-C", str(source), "status", "--porcelain"]):
        raise ValueError("baseline workload is dirty; use a separate snapshot for edit traces")
    return source


def configure(workload, build_dir):
    if workload["name"] != "zstd":
        raise ValueError("Nix configuration uses the upstream example environment; not automated here yet")
    source = prepare(workload)
    arguments = ["meson", "setup", str(build_dir), str(source / workload["meson_source"])]
    if (build_dir / "meson-private/coredata.dat").exists():
        arguments.append("--reconfigure")
    for name, value in workload["meson_options"].items():
        value = str(value).lower() if isinstance(value, bool) else str(value)
        arguments.append(f"-D{name}={value}")
    run(arguments)
    database = build_dir / "compile_commands.json"
    manifest = {
        "schema_version": 1,
        "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workload": workload,
        "build_directory": str(build_dir),
        "machine": platform.platform(),
        "configure_argv": arguments,
        "meson": output(["meson", "--version"]),
        "ninja": output(["ninja", "--version"]),
        "commands_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
    }
    destination = ROOT / "results/workloads" / workload["name"]
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"configured-{time.time_ns()}.json"
    with path.open("x") as record:
        record.write(json.dumps(manifest, indent=2) + "\n")
    print(database)


def check(workload, build_dir):
    if workload["name"] != "zstd":
        raise ValueError("this probe checks the Zstandard library")
    source = prepare(workload)
    destination = ROOT / "results/workloads/zstd" / f"check-{time.time_ns()}"
    destination.mkdir(parents=True)
    executable = destination / "roundtrip"
    arguments = [
        "cc", "-I" + str(source / "lib"), str(ROOT / "tests/fixtures/zstd_roundtrip.c"),
        str(build_dir / workload["target"]), "-pthread", "-o", str(executable),
    ]
    record = {
        "schema_version": 1, "workload": workload,
        "build_directory": str(build_dir), "processes": [],
    }
    library = build_dir / workload["target"]
    if library.is_file():
        record["library_sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
    try:
        for argv in [arguments, [str(executable)]]:
            start = time.monotonic()
            result = subprocess.run(argv, text=True, capture_output=True)
            record["processes"].append({
                "argv": argv, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "elapsed_seconds": time.monotonic() - start,
            })
            if result.returncode:
                raise RuntimeError(f"workload check failed; see {destination / 'result.json'}")
        record["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
        stdout = record["processes"][-1]["stdout"].strip()
        if stdout != "zstd roundtrip ok":
            raise ValueError("roundtrip probe returned unexpected output")
        record["passed"] = True
    except (OSError, ValueError, RuntimeError) as error:
        record["passed"] = False
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(stdout)


def main():
    parser = argparse.ArgumentParser(description="Pinned real workloads for nix-ninja measurements")
    parser.add_argument("action", choices=["prepare", "configure", "ninja", "check"])
    parser.add_argument("workload", choices=[workload["name"] for workload in WORKLOADS])
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    workload = next(workload for workload in WORKLOADS if workload["name"] == args.workload)
    build_dir = (args.build_dir or ROOT / ".work/build" / args.workload).resolve()
    if args.action == "prepare":
        print(prepare(workload))
    elif args.action == "configure":
        configure(workload, build_dir)
    elif args.action == "check":
        check(workload, build_dir)
    else:
        prepare(workload)
        run(["ninja", "-C", str(build_dir), f"-j{args.jobs}", workload["target"]])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
