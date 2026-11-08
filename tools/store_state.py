from contextlib import closing
from pathlib import Path
import re
import sqlite3


STORE = Path("/nix/store")
DATABASE = Path("/nix/var/nix/db/db.sqlite")
STORE_NAME = re.compile(r"[0-9abcdfghijklmnpqrsvwxyz]{32}-.+")
OUTPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9+_.-]*")


def store_path(value):
    path = Path(value)
    if path.parent != STORE or not STORE_NAME.fullmatch(path.name):
        raise RuntimeError("build-trace path is outside the logical store")
    return str(path)


def build_traces(database=DATABASE):
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        rows = connection.execute(
            "select drvPath, outputName, outputPath, coalesce(signatures, '') from BuildTraceV3 order by drvPath, outputName"
        ).fetchall()
    traces = {}
    for derivation, output, path, signatures in rows:
        derivation = store_path(str(STORE / derivation))
        if not derivation.endswith(".drv") or not OUTPUT_NAME.fullmatch(output):
            raise RuntimeError("invalid pinned build-trace key")
        key = derivation + "^" + output
        if key in traces:
            raise RuntimeError("duplicate pinned build-trace key")
        traces[key] = {"output_path": store_path(path), "signatures": signatures}
    return traces


def trace_deletion_set(protected, current, protected_paths, current_paths):
    if any(current.get(key) != value for key, value in protected.items()):
        raise RuntimeError("the protected build-trace map changed")
    owned_paths = set(current_paths) - set(protected_paths)
    selected = sorted(set(current) - set(protected))
    if len(selected) > 8192:
        raise RuntimeError("build-trace cleanup exceeds the experiment key budget")
    for key in selected:
        derivation, output = key.rsplit("^", 1)
        store_path(derivation)
        if not derivation.endswith(".drv") or not OUTPUT_NAME.fullmatch(output):
            raise RuntimeError("invalid experiment build-trace key")
        path = store_path(current[key]["output_path"])
        if derivation not in owned_paths and path not in owned_paths:
            raise RuntimeError("build-trace cleanup reaches a protected or unknown derivation/output")
    return selected
