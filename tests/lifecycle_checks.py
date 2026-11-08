import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_oracle import digest, invoke, observe, outcome, source_state
from input_view import InputView
from planning import discovery_report


NINJA = """rule cc
  command = cc -O2 -g0 -frandom-seed=0 -fdiagnostics-color=never -MD -MF $out.d -c $in -o $out
  depfile = $out.d
  deps = gcc
rule link
  command = cc -frandom-seed=0 $in -o $out
build main.o: cc ../source/main.c
build app: link main.o
"""


def state_summary(cache):
    result = {}
    for path in sorted(cache.iterdir()) if cache.exists() else []:
        entry = {"mode": oct(path.lstat().st_mode & 0o777)}
        if path.is_symlink():
            entry["symlink"] = str(path.readlink())
        elif path.is_file():
            entry["bytes"] = path.stat().st_size
            with path.open("rb") as stream:
                entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            entry["kind"] = "other"
        result[path.name] = entry
    return result


@contextmanager
def fault(cache, kind):
    state = cache / "state.json"
    cleanup = lambda: None
    if kind == "missing":
        state.unlink()
    elif kind == "truncated":
        state.write_bytes(b'{"version":')
    elif kind in {"version", "namespace", "checksum", "bundle", "indices", "copied-key"}:
        envelope = json.loads(state.read_text())
        if kind == "version":
            envelope["version"] += 1
        elif kind == "namespace":
            envelope["namespace"] = "0" * 64
        elif kind == "checksum":
            envelope["checksum"] = "0" * 64
        else:
            bundle = json.loads(envelope["payload"])
            if kind == "bundle":
                bundle["version"] += 1
            elif kind == "indices":
                next(iter(bundle["records"].values()))["dependencies"] = [len(bundle["paths"])]
            else:
                key = next(iter(bundle["records"]))
                if key == "0" * 64:
                    raise ValueError("fixture key collides with the injected key")
                bundle["records"]["0" * 64] = bundle["records"].pop(key)
            envelope["payload"] = json.dumps(bundle, separators=(",", ":"))
            envelope["checksum"] = hashlib.sha256(envelope["payload"].encode()).hexdigest()
        state.write_text(json.dumps(envelope))
    elif kind == "oversized":
        with state.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    elif kind == "symlink":
        sentinel = cache.parent / "state-sentinel"
        sentinel.write_text("keep these bytes\n")
        state.unlink()
        state.symlink_to(sentinel)

        def cleanup():
            if sentinel.read_text() != "keep these bytes\n":
                raise RuntimeError("state publication followed a symlink")
    elif kind == "temps":
        (cache / ".nni-discovery.tmp-1-2").write_text("partial")
        (cache / "notes.txt").write_text("keep this\n")

        def cleanup():
            if (cache / ".nni-discovery.tmp-1-2").exists():
                raise RuntimeError("abandoned publication was not cleaned up")
            if (cache / "notes.txt").read_text() != "keep this\n":
                raise RuntimeError("cleanup removed an unrelated file")
    elif kind == "permissions":
        cache.chmod(0o755)
        cleanup = lambda: cache.chmod(0o700)
    elif kind == "cache-lock":
        lock = (cache / ".nix-ninja-discovery-owner").open("r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cleanup = lock.close
    elif kind is not None:
        raise ValueError(f"unknown state fault: {kind}")
    try:
        yield
    finally:
        cleanup()


def check(binary, destination):
    env = dict(os.environ)
    for key in ["NIX_BUILD_TOP", "NIX_NINJA_DRV", "NIX_NINJA_PROFILE"]:
        env.pop(key, None)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]
    env["LC_ALL"] = "C"
    record = {"schema_version": 1, "stage": "native cache lifecycle versus fresh Ninja",
              "binary": str(binary), "binary_sha256": digest(binary),
              "harness_sha256": digest(Path(__file__)),
              "pins": json.loads((ROOT / "environment/pins.json").read_text()),
              "patch_series": json.loads((ROOT / "patches/series.json").read_text()),
              "compiler": invoke(["cc", "--version"], env),
              "steps": [], "passed": False}
    try:
        with closing(InputView.create(provenance={"lifecycle": record["harness_sha256"]})) as view:
            source, actual, reference = [view.root / name for name in ["source", "actual", "reference"]]
            with view.lease():
                for path in [source, actual, reference]:
                    path.mkdir()
                (source / "main.c").write_text('#include "value.h"\nint main(void) { return VALUE; }\n')
                (source / "value.h").write_text("#define VALUE 7\n")
                for path in [actual, reference]:
                    (path / "build.ninja").write_text(NINJA)
            cache = view.control / "discovery"
            command = [str(binary), "-C", str(actual), "--input-view", str(view.marker),
                       "--discovery-mode", "reuse", "--discovery-cache", str(cache), "app"]

            def run(name, *, kind=None, load="loaded", hit=True, value=7, fallback=False):
                step = {"name": name, "fault": kind, "expected_load": load, "expected_hit": hit,
                        "value": value, "fallback": fallback}
                record["steps"].append(step)
                with view.lease():
                    (source / "value.h").write_text(f"#define VALUE {value}\n")
                    step["source"] = source_state(source)
                    for output in ["main.o", "main.o.d", "app"]:
                        (reference / output).unlink(missing_ok=True)
                    fresh = invoke(["ninja", "-C", str(reference), "-j1", "app"], env)
                    expected = observe(reference, "app", fresh, env)
                    step.update(reference=fresh, expected=expected)
                    view.validate_links()
                with fault(cache, kind):
                    step["state_before"] = state_summary(cache)
                    result = invoke(command, env)
                    step["actual"] = result
                    if fallback:
                        reports = [json.loads(line.removeprefix("NNI_DISCOVERY="))
                                   for line in result["stderr"].splitlines() if line.startswith("NNI_DISCOVERY=")]
                        if len(reports) != 1:
                            raise RuntimeError("expected one fallback discovery report")
                        audit = reports[0]
                        if (not audit["planning_complete"] or audit["mode"] != "reuse"
                                or audit["namespace"] is not None or audit["publication"] != "disabled"
                                or audit["hits"] or audit["calls"] != audit["scans"]
                                or audit["candidate_mismatches"] or audit["validation_errors"]):
                            raise RuntimeError("invalid uncached fallback")
                    else:
                        audit = discovery_report(result["stderr"], "reuse")
                    step["discovery"] = audit
                    if audit["load"] != load or bool(audit["hits"]) != hit:
                        raise RuntimeError(f"unexpected state handling: {name}")
                    if hit and audit["hits"] != audit["calls"]:
                        raise RuntimeError("unchanged fixture did not reuse every discovery action")
                    with view.lease():
                        observed = observe(actual, "app", result, env)
                        step["observed"] = observed
                        if (outcome(observed) != outcome(expected) or observed.get("program_exit") != value):
                            raise RuntimeError(f"fresh-build mismatch: {name}")
                    step["state_after"] = state_summary(cache)
                    if fallback and step["state_before"] != step["state_after"]:
                        raise RuntimeError("uncached fallback modified cache state")
                step["passed"] = True
                print(f"{name}: matched fresh build", flush=True)

            run("cold", load="absent", hit=False)
            run("warm")
            for index, (kind, load) in enumerate([
                ("missing", "absent"), ("truncated", "malformed"), ("version", "version"),
                ("namespace", "namespace"), ("checksum", "checksum"), ("bundle", "invalid-bundle"),
                ("indices", "invalid-bundle"), ("copied-key", "invalid-bundle"),
                ("oversized", "oversized"), ("symlink", "non-regular"),
            ]):
                value = 8 + index
                run(kind, kind=kind, load=load, hit=False, value=value)
                run(f"{kind}-recovery", value=value)
            run("abandoned-temp", kind="temps", value=value)
            for kind in ["permissions", "cache-lock"]:
                value += 1
                run(kind, kind=kind, load="setup-error", hit=False, value=value, fallback=True)
                run(f"{kind}-recovery", hit=False, value=value)
                run(f"{kind}-warm", value=value)
            with view.lease():
                before = state_summary(cache)
                app = digest(actual / "app")
                result = invoke(command, env)
                after = state_summary(cache)
                if (result["returncode"] == 0 or "input view is busy" not in result["stderr"]
                        or before != after or digest(actual / "app") != app):
                    raise RuntimeError("input-view contention modified state or artifacts")
                record["input_contention"] = result
            run("input-contention-recovery", value=value)
            record["final_state"] = state_summary(cache)
            record["input_view"] = view.data
        record["passed"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (destination / "result.json").write_text(json.dumps(record, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Cache recovery against independent fresh artifacts")
    parser.add_argument("--binary", type=Path, default=shutil.which("nix-ninja"))
    args = parser.parse_args()
    if args.binary is None:
        parser.error("--binary or nix-ninja in PATH is required")
    destination = ROOT / "results/lifecycle" / f"check-{time.time_ns()}"
    destination.mkdir(parents=True)
    print(destination, flush=True)
    check(Path(args.binary).resolve(strict=True), destination)


if __name__ == "__main__":
    main()
