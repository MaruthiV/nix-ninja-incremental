import argparse
import copy
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

from build_oracle import mutate


KINDS = ["implementation", "header", "preserved-mtime", "optional", "shadow", "search-order",
         "retarget", "generated", "generator", "invalid", "recovery", "graph", "command",
         "irrelevant", "noop", "query-alias"]


def snapshot(state, name):
    outer = state["outer"]
    files = {
        "main.c": f'#include "{outer}"\nint value(void);\n'
                  + ("int extra(void);\n" if state["extra"] else "")
                  + "int main(void) { return (value()"
                  + (" + extra()" if state["extra"] else "") + ") % 128; }\n",
        outer: '#include <value.h>\n',
        "late/value.h": f'#define VALUE {state["value"]}\n',
        "one/alias.h": f'#define ALIAS {state["alias"]}\n',
        "two/alias.h": f'#define ALIAS {state["alias"] + 1}\n',
        "unused.txt": str(state["irrelevant"]) + "\n",
    }
    if state["shadow"]:
        files["early/value.h"] = f'#define VALUE {state["shadow_value"]}\n'
    if state["bonus"]:
        files["bonus.h"] = f'#define BONUS {state["bonus"]}\n'
        files["query.marker"] = 'not a c header\n'
    implementation = f'#include "{outer}"\n#include "alias.h"\n'
    implementation += '#if __has_include("bonus.h")\n#include "bonus.h"\n#else\n#define BONUS 0\n#endif\n'
    probe = "__has_include" if state["query"] == 0 else "PROBE"
    if state["query"] in {1, 2}:
        implementation += '#include "query/defs.h"\n'
        files["query/defs.h"] = ('#define PROBE __has_include\n' if state["query"] == 1
                                  else '#define ROOT __has_include\n#define PROBE ROOT\n')
    implementation += f'#if {probe}("query.marker")\n#define QUERY 7\n#else\n#define QUERY 0\n#endif\n'
    if state["generated"]:
        implementation += '#include "generated.h"\n'
    expression = f'VALUE + ALIAS + BONUS + QUERY + MODE + {state["offset"]}'
    if state["generated"]:
        expression += " + GENERATED"
    files["lib.c"] = implementation + "int value(void) { return " + expression
    files["lib.c"] += " + ; }\n" if state["invalid"] else "; }\n"
    files["extra.c"] = f'int extra(void) {{ return {state["extra_value"]}; }}\n'
    includes = ["late", "early"] if state["reverse"] else ["early", "late"]
    flags = " ".join(f"-I../source/{name}" for name in includes)
    if state["query"] == 3:
        flags += " -DPROBE=__has_include"
    ninja = "rule cc\n  command = cc -O2 -g0 -frandom-seed=0 -fdiagnostics-color=never "
    ninja += f'{flags} -I../source -I. -DMODE={state["mode"]} -MD -MF $out.d -o $out -c $in\n'
    ninja += "  depfile = $out.d\n  deps = gcc\nrule link\n  command = cc -frandom-seed=0 $in -o $out\n"
    ninja += "build main.o: cc ../source/main.c\nbuild lib.o: cc ../source/lib.c"
    if state["generated"]:
        ninja += " || generated.h"
    ninja += "\n"
    if state["generated"]:
        ninja += f"rule generate\n  command = printf '#define GENERATED {state['generator']}\\n' > $out\n"
        ninja += "build generated.h: generate\n"
    if state["extra"]:
        ninja += "build extra.o: cc ../source/extra.c\n"
    ninja += "build app: link main.o lib.o" + (" extra.o" if state["extra"] else "") + "\n"
    value = state["shadow_value"] if state["shadow"] and not state["reverse"] else state["value"]
    value += state["alias"] + int(state["retarget"]) + state["bonus"] + state["mode"] + state["offset"]
    value += 7 if state["bonus"] else 0
    value += state["generator"] if state["generated"] else 0
    value += state["extra_value"] if state["extra"] else 0
    return {"name": name, "files": files, "symlink": {"alias.h": "two/alias.h" if state["retarget"] else "one/alias.h"},
            "build_ninja": ninja, "expected_exit": None if state["invalid"] else value % 128,
            "diagnostic_file": "lib.c", "preserve_mtime": name.endswith("preserved-mtime")}


def fixture(states, seed):
    if not states:
        raise ValueError("a trace needs an initial state")
    first = states[0]
    result = {"schema_version": 1, "name": f"seeded-edits-{seed}", "seed": seed, "target": "app",
              "outputs": ["main.o", "lib.o", "extra.o", "app", "main.o.d", "lib.o.d", "extra.o.d", "generated.h"],
              "directories": ["early"], "files": first["files"], "build_ninja": first["build_ninja"], "edits": []}
    previous = {"files": first["files"], "symlink": {}, "build_ninja": first["build_ninja"]}
    previous_exit = None
    for state in states:
        edit = {key: state[key] for key in ["name", "expected_exit", "diagnostic_file"]}
        removed = [key for kind in ["files", "symlink"] for key, value in previous[kind].items()
                   if key not in state[kind] or (kind == "symlink" and state[kind][key] != value)]
        written = {key: value for key, value in state["files"].items() if previous["files"].get(key) != value}
        links = {key: value for key, value in state["symlink"].items() if previous["symlink"].get(key) != value}
        if removed:
            edit["remove"] = removed
        if written:
            edit["write"] = written
        if links:
            edit["symlink"] = links
        if state["preserve_mtime"]:
            edit["preserve_mtime"] = [key for key in written if key in previous["files"]]
        if state["build_ninja"] != previous["build_ninja"]:
            edit["build_ninja"] = state["build_ninja"]
        if previous_exit is not None and state["expected_exit"] != previous_exit:
            edit["stale_probe"] = True
        result["edits"].append(edit)
        previous = state
        previous_exit = state["expected_exit"]
    return result


def generate(seed, edits=16):
    if edits < len(KINDS):
        raise ValueError(f"at least {len(KINDS)} edits are needed for required coverage")
    rng = random.Random(seed)
    state = {"outer": "outer-" + str(rng.randrange(1000)) + ".h", "value": rng.randrange(1, 15),
             "alias": rng.randrange(1, 8), "offset": 0, "mode": 0, "bonus": 0, "shadow": False,
             "shadow_value": rng.randrange(20, 30), "retarget": False, "reverse": False,
             "generated": False, "generator": rng.randrange(1, 8), "invalid": False,
             "extra": False, "extra_value": rng.randrange(1, 8), "irrelevant": 0, "query": 0}
    states = [snapshot(state, "baseline")]
    kinds = KINDS[:3]
    rng.shuffle(kinds)
    middle = KINDS[3:7]
    rng.shuffle(middle)
    kinds += middle + KINDS[7:]
    kinds += [rng.choice(KINDS) for _ in range(edits - len(kinds))]
    for index, kind in enumerate(kinds):
        if kind == "implementation":
            state["offset"] += rng.randrange(1, 5)
        elif kind in {"header", "preserved-mtime"}:
            state["value"] += rng.randrange(1, 5)
        elif kind == "optional":
            state["bonus"] = rng.randrange(1, 8) if not state["bonus"] else 0
        elif kind in {"shadow", "search-order", "retarget", "generated", "invalid", "graph"}:
            key = {"search-order": "reverse", "graph": "extra"}.get(kind, kind)
            state[key] = not state[key]
        elif kind == "generator":
            state["generator"] += rng.randrange(1, 5)
        elif kind == "recovery":
            state["invalid"] = False
        elif kind == "command":
            state["mode"] += rng.randrange(1, 5)
        elif kind == "irrelevant":
            state["irrelevant"] += 1
        elif kind == "query-alias":
            state["query"] = (state["query"] + rng.randrange(1, 4)) % 4
        states.append(snapshot(state, f"{index:03d}-{kind}"))
    return states


def minimize(states, reproduces):
    retained = copy.deepcopy(states)
    size = max(1, (len(retained) - 2) // 2)
    while size:
        index = 1
        while index < len(retained) - 1:
            end = min(index + size, len(retained) - 1)
            candidate = retained[:index] + retained[end:]
            if reproduces(candidate):
                retained = candidate
                index = 1
            else:
                index = end
        size //= 2
    return retained


def check_reference(value):
    records = []
    with tempfile.TemporaryDirectory(prefix="nni-trace-reference-") as temporary:
        root = Path(temporary).resolve()
        source, build = root / "source", root / "build"
        source.mkdir()
        build.mkdir()
        for directory in value["directories"]:
            (source / directory).mkdir()
        mutate(source, {"write": value["files"]})
        (build / "build.ninja").write_text(value["build_ninja"])
        for edit in value["edits"]:
            mutate(source, edit)
            if "build_ninja" in edit:
                (build / "build.ninja").write_text(edit["build_ninja"])
            for name in value["outputs"]:
                (build / name).unlink(missing_ok=True)
            result = subprocess.run(["ninja", "-C", str(build), "-j1", value["target"]], text=True, capture_output=True)
            record = {"name": edit["name"], "argv": result.args, "returncode": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}
            records.append(record)
            if edit["expected_exit"] is None:
                pattern = re.escape(edit.get("diagnostic_file", "lib.c")) + r":\d+:\d+: (?:fatal )?error:"
                if result.returncode == 0 or not re.search(pattern, result.stdout + result.stderr):
                    raise RuntimeError(f"expected compiler failure: {edit['name']}")
            else:
                if result.returncode:
                    raise RuntimeError(f"reference build failed: {edit['name']}\n{result.stdout}{result.stderr}")
                actual = subprocess.run([str(build / value["target"])]).returncode
                record.update(program_exit=actual, artifact_sha256=hashlib.sha256((build / value["target"]).read_bytes()).hexdigest())
                if actual != edit["expected_exit"]:
                    raise RuntimeError(f"reference result differs: {edit['name']}: {actual} != {edit['expected_exit']}")
    return records


def main():
    parser = argparse.ArgumentParser(description="Deterministic edit traces with independently executable references")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--edits", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-reference", action="store_true")
    args = parser.parse_args()
    states = generate(args.seed, args.edits)
    value = fixture(states, args.seed)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    args.output.with_suffix(".states.json").write_text(json.dumps(states, indent=2) + "\n")
    if args.check_reference:
        references = check_reference(value)
        args.output.with_suffix(".reference.json").write_text(json.dumps(references, indent=2) + "\n")


if __name__ == "__main__":
    main()
