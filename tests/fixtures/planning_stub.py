import argparse
import fcntl
import json
from pathlib import Path
import sys


parser = argparse.ArgumentParser()
parser.add_argument("-C", type=Path)
parser.add_argument("-j")
parser.add_argument("-t")
parser.add_argument("target")
parser.add_argument("--input-view", type=Path)
parser.add_argument("--discovery-mode")
parser.add_argument("--discovery-cache", type=Path)
args = parser.parse_args()
if sys.argv[-1] != args.target:
    sys.exit("planner flags must precede the trailing target")
configuration = json.loads((args.C / "stub.json").read_text())
control = args.C.parent.parent
with (control / "view.lock").open("r+") as lock:
    acquired = True
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        acquired = False
    if acquired != (args.input_view is not None):
        sys.exit("unexpected input-view lease ownership")
    print(json.dumps(configuration.get("derivation", {"drvPath": "unchanged"})))
    if args.input_view is not None:
        record = args.discovery_mode == "record"
        warm = args.discovery_cache.exists()
        args.discovery_cache.mkdir(exist_ok=True)
        audit = {"schema_version": 1, "mode": args.discovery_mode, "planning_complete": True,
                 "hits": 0, "candidate_mismatches": 0, "validation_errors": 0,
                 "namespace": "fixture", "publication": "published" if record else "disabled",
                 "recorded_actions": int(record), "validated_candidates": int(record and warm)}
        audit.update(configuration.get("audit", {}))
        print("NNI_DISCOVERY=" + json.dumps(audit), file=sys.stderr)
