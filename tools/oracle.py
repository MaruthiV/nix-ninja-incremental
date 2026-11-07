import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def depfile_paths(text, directory):
    text = text.replace("\\\r\n", "").replace("\\\n", "")
    if not text.startswith("nni:"):
        raise ValueError("unexpected dependency target")
    words = []
    word = []
    index = 4
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 1
            word.append(text[index])
        elif char == "$" and text[index:index + 2] == "$$":
            word.append("$")
            index += 1
        elif char.isspace():
            if word:
                words.append("".join(word))
                word = []
        elif char == "#":
            raise ValueError("unescaped dependency comment")
        else:
            word.append(char)
        index += 1
    if word:
        words.append("".join(word))
    return {str((directory / path).resolve(strict=True)) for path in words}


def capture(argv, directory):
    start = time.monotonic_ns()
    process = subprocess.run(
        argv, cwd=directory, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120,
    )
    return {
        "argv": argv, "returncode": process.returncode,
        "stdout": process.stdout, "stderr": process.stderr,
        "elapsed_ns": time.monotonic_ns() - start,
    }


def reference_arguments(arguments):
    result = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith(("@", "-Wp,", "-save-temps")):
            raise ValueError(f"unsupported reference argument: {argument}")
        if argument in {"-o", "-MF", "-MT", "-MQ"}:
            if index + 1 == len(arguments):
                raise ValueError(f"missing value for {argument}")
            index += 2
            continue
        if argument in {"-MD", "-MMD", "-MP", "-M", "-MM", "-MG"}:
            index += 1
            continue
        if any(argument.startswith(prefix) and len(argument) > len(prefix)
               for prefix in ["-MF", "-MT", "-MQ"]):
            index += 1
            continue
        result.append(argument)
        index += 1
    return result


def compiler_reference(arguments, directory):
    arguments = reference_arguments(arguments)
    directory = Path(directory)
    with tempfile.TemporaryDirectory(prefix="nni-reference-") as temp:
        depfile = Path(temp) / "deps.d"
        command = [*arguments, "-M", "-MF", str(depfile), "-MT", "nni"]
        discovery = capture(command, directory)
        discovery["dependencies"] = []
        if discovery["returncode"] == 0:
            discovery["dependencies"] = sorted(depfile_paths(depfile.read_text(), directory))
        artifact = Path(temp) / "reference.o"
        compilation = capture([
            *arguments, "-o", str(artifact), "-MD", "-MF", str(Path(temp) / "compile.d"),
        ], directory)
        if compilation["returncode"] == 0:
            compilation["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            compilation["artifact_bytes"] = artifact.stat().st_size
    return {"discovery": discovery, "compilation": compilation}


def compare_dependencies(actual, expected):
    actual, expected = set(actual), set(expected)
    return {"missing": sorted(expected - actual), "extra": sorted(actual - expected)}


def check_fixture(case, scanner, compiler, base):
    directory = (base / case["name"]).resolve()
    if not directory.is_relative_to(base.resolve()) or directory == base.resolve():
        raise ValueError("fixture name escapes its directory")
    directory.mkdir()
    for name, content in case["files"].items():
        destination = directory / name
        if not destination.resolve().is_relative_to(directory.resolve()):
            raise ValueError("fixture path escapes its directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    arguments = [compiler, "-nostdinc", *case.get("flags", []), "-c", "main.c", "-o", "main.o"]
    database = directory / "compile_commands.json"
    database.write_text(json.dumps([{
        "directory": str(directory), "file": "main.c", "arguments": arguments,
    }]))
    measured = capture([str(scanner), str(database)], directory)
    if measured["returncode"] != 0:
        return {"name": case["name"], "passed": False, "scanner": measured}
    report = json.loads(measured["stdout"])
    if report["schema_version"] != 1 or len(report["actions"]) != 1:
        raise ValueError("unexpected scanner report")
    actual = report["actions"][0]
    reference = compiler_reference(arguments, directory)
    comparison = compare_dependencies(actual["dependencies"], reference["discovery"]["dependencies"])
    expected = {str((directory / name).resolve()) for name in case.get("expected_dependencies", [])}
    expected_missing = {str((directory / name).resolve()) for name in case.get("expected_missing", [])}
    correct_oracle = expected == set(reference["discovery"]["dependencies"])
    correct_discovery = (reference["discovery"]["returncode"] == 0) == case.get("discovery_succeeds", True)
    correct_compilation = (reference["compilation"]["returncode"] == 0) == case.get("compile_succeeds", True)
    expected_difference = set(comparison["missing"]) == expected_missing
    expected_extra = {str((directory / name).resolve()) for name in case.get("expected_extra", [])}
    permitted_extra = set(comparison["extra"]) == expected_extra
    passed = all([
        correct_oracle, correct_discovery, correct_compilation,
        expected_difference, permitted_extra, actual.get("error") is None,
    ])
    return {
        "name": case["name"],
        "passed": passed,
        "baseline_limitation": case.get("baseline_limitation"),
        "oracle_matches_fixture": correct_oracle,
        "compiler_status_matches_fixture": correct_discovery and correct_compilation,
        "comparison": comparison,
        "scanner": report,
        "reference": reference,
    }


def campaign(scanner, compiler, fixture_file):
    cases = json.loads(fixture_file.read_text())
    with tempfile.TemporaryDirectory(prefix="nni-fixtures-") as temp:
        results = [check_fixture(case, scanner, compiler, Path(temp)) for case in cases]
    return {
        "schema_version": 1,
        "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": platform.platform(),
        "scanner_sha256": hashlib.sha256(scanner.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(fixture_file.read_bytes()).hexdigest(),
        "compiler": capture([compiler, "--version"], ROOT),
        "passed": all(result["passed"] for result in results),
        "cases": results,
    }


def database_campaign(scanner, commands_file, project_roots):
    if not project_roots:
        raise ValueError("database checks require at least one --project-root")
    roots = [path.resolve(strict=True) for path in project_roots]

    def in_project(path):
        return any(Path(path).is_relative_to(root) for root in roots)

    commands = json.loads(commands_file.read_text())
    measured = capture([str(scanner), str(commands_file.resolve())], ROOT)
    report = json.loads(measured["stdout"])
    if len(report["actions"]) != len(commands):
        raise ValueError("scanner action count does not match the compilation database")
    results = []
    for entry, actual in zip(commands, report["actions"]):
        arguments = entry.get("arguments") or shlex.split(entry["command"])
        if any(token in {"&&", "||", ";", "|", ">", "<"} for token in arguments):
            raise ValueError("shell command chains are outside the reference contract")
        reference = compiler_reference(arguments, entry["directory"])
        expected = reference["discovery"]["dependencies"]
        comparison = compare_dependencies(
            [path for path in actual["dependencies"] if in_project(path)],
            [path for path in expected if in_project(path)],
        )
        passed = all([
            not comparison["missing"], actual["error"] is None,
            reference["compilation"]["returncode"] == 0,
            reference["discovery"]["returncode"] == 0,
        ])
        results.append({
            "file": entry["file"], "comparison": comparison,
            "excluded_reference_dependencies": [path for path in expected if not in_project(path)],
            "scanner": actual, "reference": reference,
            "passed": passed,
        })
    return {
        "schema_version": 1, "machine": platform.platform(),
        "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scanner_sha256": hashlib.sha256(scanner.read_bytes()).hexdigest(),
        "commands_sha256": hashlib.sha256(commands_file.read_bytes()).hexdigest(),
        "project_roots": [str(path) for path in roots],
        "passed": measured["returncode"] == 0 and all(result["passed"] for result in results),
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Independent compiler checks for nix-ninja discovery")
    parser.add_argument("--scanner", type=Path, required=True)
    parser.add_argument("--compiler", default=os.environ.get("CC", "cc"))
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures/scanner.json")
    parser.add_argument("--commands", type=Path)
    parser.add_argument("--project-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scanner = args.scanner.resolve(strict=True)
    if args.commands:
        result = database_campaign(scanner, args.commands, args.project_root)
    else:
        result = campaign(scanner, args.compiler, args.fixtures)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    failures = [case.get("name", case.get("file")) for case in result["cases"] if not case["passed"]]
    print(f"{len(result['cases']) - len(failures)}/{len(result['cases'])} cases matched their declared outcomes")
    limitations = [case["name"] for case in result["cases"] if case.get("baseline_limitation")]
    if limitations:
        print("known upstream limitations: " + ", ".join(limitations))
    if failures:
        raise SystemExit("unexpected outcomes: " + ", ".join(failures))


if __name__ == "__main__":
    main()
