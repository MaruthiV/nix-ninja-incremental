import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "environment/pins.json").read_text())
SOURCE = ROOT / ".work/nix-ninja"
CONTAINER = "nni-" + hashlib.sha256(os.fsencode(ROOT)).hexdigest()[:12]


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(args):
    return run(args, stdout=subprocess.PIPE).stdout.strip()


def prepare():
    pin = PINS["upstream"]
    if not SOURCE.exists():
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--no-checkout", pin["url"], str(SOURCE)])
        run(["git", "-C", str(SOURCE), "checkout", "--detach", pin["revision"]])
    actual = output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"])
    if actual != pin["revision"]:
        raise ValueError(f"upstream revision mismatch: {actual}")
    if output(["git", "-C", str(SOURCE), "status", "--porcelain"]):
        raise ValueError("baseline source is dirty; keep changes in a separate worktree")
    for name, expected in pin["files"].items():
        actual = hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"upstream checksum mismatch: {name}")
    print(f"upstream verified: {pin['revision']}")


def inspect():
    result = subprocess.run(
        ["docker", "container", "inspect", CONTAINER],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        if "No such container" in result.stderr or "No such object" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    info = json.loads(result.stdout)[0]
    if info["Config"]["Labels"].get("nni.workspace") != str(ROOT):
        raise ValueError(f"container {CONTAINER} belongs to another workspace")
    if info["Config"]["Image"] != PINS["container"]["image"]:
        raise ValueError(f"container {CONTAINER} uses a different image")
    mounts = {m["Destination"]: m for m in info["Mounts"]}
    if mounts.get("/workspace", {}).get("Source") != str(ROOT):
        raise ValueError("container workspace mount mismatch")
    return info


def up():
    info = inspect()
    if info is None:
        pin = PINS["container"]
        run([
            "docker", "run", "--detach", "--name", CONTAINER,
            "--platform", pin["platform"], "--cpus", str(pin["cpus"]),
            "--memory", pin["memory"], "--pids-limit", str(pin["pids_limit"]),
            "--label", f"nni.workspace={ROOT}",
            "--mount", f"type=bind,src={ROOT},dst=/workspace",
            "--mount", f"type=bind,src={ROOT / 'environment/nix.conf'},dst=/etc/nix/nix.conf,readonly",
            "--workdir", "/workspace", "--entrypoint", "nix-daemon", pin["image"],
        ])
    elif not info["State"]["Running"]:
        run(["docker", "start", CONTAINER])
    deadline = time.monotonic() + 15
    while True:
        ready = subprocess.run(
            ["docker", "exec", CONTAINER, "nix", "store", "info", "--store", "daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"daemon did not become ready; inspect docker logs {CONTAINER}")
        time.sleep(0.1)
    print(f"container ready: {CONTAINER}")


def exec_args(command, interactive=False):
    info = inspect()
    if info is None or not info["State"]["Running"]:
        raise ValueError("environment is stopped; run python3 tools/dev.py up")
    flags = ["--interactive"] if interactive else []
    if interactive and sys.stdin.isatty():
        flags.append("--tty")
    return [
        "docker", "exec", *flags, "--env",
        "NIX_REMOTE=unix:///nix/var/nix/daemon-socket/socket",
        CONTAINER, *command,
    ]


def probe():
    prepare()
    version = output(exec_args(["nix", "--version"]))
    versions = {
        f"nix (Nix) {PINS['runtime']['version']}",
        f"nix (Nix) {PINS['container']['nix']}",
    }
    if version not in versions:
        raise ValueError(f"Nix version mismatch: {version}")
    manifest = {
        "schema_version": 1,
        "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": output(["uname", "-sm"]),
        "container": CONTAINER,
        "image_id": inspect()["Image"],
        "nix_version": version,
        "daemon": json.loads(output(exec_args(["nix", "store", "info", "--store", "daemon", "--json"]))),
        "linux": output(exec_args(["uname", "-sm"])),
        "cpu_quota": output(exec_args(["cat", "/sys/fs/cgroup/cpu.max"])),
        "memory_limit": output(exec_args(["cat", "/sys/fs/cgroup/memory.max"])),
        "pins": PINS,
    }
    destination = ROOT / "results/setup/environment.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "pins"}, indent=2))


def runtime():
    prepare()
    destination = ROOT / "results/setup" / f"runtime-{time.time_ns()}"
    destination.mkdir(parents=True)
    expression = '(builtins.getFlake "path:/workspace/.work/nix-ninja").inputs.nix.packages.x86_64-linux.nix-cli'
    arguments = exec_args(["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths"])
    record = {"argv": arguments, "runtime_pin": PINS["runtime"]}
    start = time.monotonic()
    try:
        with (destination / "build.log").open("w") as log:
            result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=log)
        record.update(returncode=result.returncode, stdout=result.stdout)
        paths = result.stdout.strip().splitlines()
        if result.returncode or len(paths) != 1 or not paths[0].startswith("/nix/store/"):
            raise RuntimeError(f"runtime build failed; see {destination}")
        path = paths[0]
        expected = f"nix (Nix) {PINS['runtime']['version']}"
        if output(exec_args([path + "/bin/nix", "--version"])) != expected:
            raise ValueError("runtime version mismatch")
        installed = output(exec_args(["readlink", "-f", "/root/.nix-profile"]))
        daemon = json.loads(output(exec_args(["nix", "store", "info", "--store", "daemon", "--json"])))
        record["restarted"] = installed != path or daemon.get("version") != PINS["runtime"]["version"]
        if record["restarted"]:
            run(exec_args(["nix-env", "--profile", "/nix/var/nix/profiles/nni-runtime", "--set", path]))
            run(exec_args(["ln", "-sfn", "/nix/var/nix/profiles/nni-runtime", "/root/.nix-profile"]))
            run(["docker", "restart", CONTAINER])
            up()
        if output(exec_args(["nix", "--version"])) != expected:
            raise ValueError("container did not select the pinned runtime")
        record["daemon"] = json.loads(output(exec_args(["nix", "store", "info", "--store", "daemon", "--json"])))
        if record["daemon"].get("version") != PINS["runtime"]["version"]:
            raise ValueError("daemon version mismatch")
        record["passed"] = True
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        record.update(passed=False, error=str(error))
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic() - start
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"pinned daemon ready: {PINS['runtime']['version']}")


def baseline(cli_runtime=False):
    prepare()
    daemon = json.loads(output(exec_args(["nix", "store", "info", "--store", "daemon", "--json"])))
    if daemon.get("version") != PINS["runtime"]["version"]:
        raise ValueError("baseline requires the pinned daemon; run python3 tools/dev.py runtime")
    destination = ROOT / "results/setup" / f"baseline-{time.time_ns()}"
    destination.mkdir(parents=True)
    for attempt in range(2):
        option = "true" if cli_runtime else "false"
        target = ["--impure", "--expr", f"(import /workspace/environment/baseline.nix {{ cliRuntime = {option}; }}).hello"]
        args = exec_args([
            "nix", "build", *target,
            "--no-link", "--print-out-paths", "--log-format", "raw",
        ])
        start = time.monotonic()
        with (destination / f"hello-{attempt}.log").open("w") as log:
            result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=log)
        record = {
            "argv": args, "returncode": result.returncode,
            "elapsed_seconds": time.monotonic() - start,
            "stdout": result.stdout,
            "store_state": "initially uncontrolled" if attempt == 0 else "warmed by first attempt",
            "runtime_package": "nix-cli" if cli_runtime else "nix",
            "task_work_directory": "NIX_BUILD_TOP/source/build",
        }
        if result.returncode == 0:
            paths = result.stdout.strip().splitlines()
            if len(paths) != 1 or not paths[0].startswith("/nix/store/"):
                record["validation_error"] = "expected one store output"
            else:
                path = paths[0] + "/hello"
                program = subprocess.run(exec_args([path]), text=True, capture_output=True)
                record["program"] = {
                    "path": path, "returncode": program.returncode,
                    "stdout": program.stdout, "stderr": program.stderr,
                }
                if program.returncode or program.stdout.strip() != "Hello dynamic derivations!":
                    record["validation_error"] = "unexpected hello result"
        (destination / f"hello-{attempt}.json").write_text(json.dumps(record, indent=2) + "\n")
        if record["returncode"] or "validation_error" in record:
            raise RuntimeError(f"baseline failed; see {destination}")
        print(f"hello attempt {attempt + 1}: {record['elapsed_seconds']:.3f}s")


def main():
    parser = argparse.ArgumentParser(description="Pinned, isolated nix-ninja development environment")
    parser.add_argument("action", choices=["prepare", "up", "stop", "info", "runtime", "baseline", "exec", "shell"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "up":
        up()
    elif args.action == "info":
        probe()
    elif args.action == "runtime":
        runtime()
    elif args.action == "baseline":
        if args.command not in [[], ["--cli-runtime"]]:
            parser.error("baseline accepts only --cli-runtime")
        baseline(cli_runtime=bool(args.command))
    elif args.action == "stop":
        if inspect() is not None:
            run(["docker", "stop", CONTAINER])
    elif args.action == "shell":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        command = command or ["bash"]
        run(exec_args([
            "nix-shell", "--impure", "environment/shell.nix", "--run", shlex.join(command),
        ], interactive=not args.command))
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("exec requires a command")
        run(exec_args(command))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
