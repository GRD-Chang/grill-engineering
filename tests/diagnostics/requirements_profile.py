"""One fixed requirements story, in two fresh CI processes; never full evidence."""
import argparse
import cProfile
import hashlib
import io
import json
import os
from pathlib import Path
import pstats
import re
import resource
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import pytest


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / ".github" / "ci"))
from ci_evidence import junit_cases, junit_key


NODE = ("tests/test_kernel_requirements.py::"
        "test_real_ticket_close_receipts_create_completion_before_dependency_and_final")
RESULTS = PROJECT / ".test-results" / "native-requirements-profile"
IDENTITY_KEYS = ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
                 "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF")


def identity():
    value = {key: os.environ.get(key, "") for key in IDENTITY_KEYS}
    repository = "GRD-Chang/grill-engineer"
    workflow = repository + "/.github/workflows/ci.yml@"
    if (value["GITHUB_REPOSITORY"] != repository
            or re.fullmatch(r"[0-9a-f]{40}", value["GITHUB_SHA"]) is None
            or any(re.fullmatch(r"[1-9][0-9]*", value[key]) is None
                   for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
            or value["GITHUB_WORKFLOW_REF"] not in {
                workflow + "refs/heads/refactor/unified-integration-20261022",
                workflow + "refs/pull/505/merge"}):
        raise ValueError("Missing or unexpected fixed-story CI identity")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD", "HEAD^{tree}"],
                                    cwd=PROJECT, text=True, timeout=10).splitlines()
    if (len(actual) != 2 or actual[0] != value["GITHUB_SHA"]
            or re.fullmatch(r"[0-9a-f]{40}", actual[1]) is None):
        raise ValueError("Fixed-story checkout identity mismatch")
    return {**value, "tree": actual[1]}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_one_pass(folder):
    if junit_cases(folder) != {junit_key(NODE)}:
        raise ValueError("Fixed story requires its one exact passed node")
    suites = ET.parse(folder / "junit.xml").getroot().iter("testsuite")
    if sum(int(suite.attrib["tests"]) for suite in suites) != 1:
        raise ValueError("Fixed story requires exactly one passed test")


def snapshot():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"wall_ns": time.monotonic_ns(), "process_cpu_ns": time.process_time_ns(),
            "main_thread_cpu_ns": time.thread_time_ns(),
            "waited_children_user_seconds": children.ru_utime,
            "waited_children_system_seconds": children.ru_stime}


def export_profile(profiler, folder):
    # No external timer, SQL callbacks, frame locals or business-body wrappers.
    profiler.dump_stats(str(folder / "profile.pstats"))
    stats = pstats.Stats(profiler)
    reports = {}
    for name, sort, callers, maximum in (("self_wall", "tottime", False, 60),
                                         ("cumulative_wall", "cumtime", False, 60),
                                         ("callers_wall", "tottime", True, 80)):
        stream = io.StringIO()
        stats.stream = stream
        stats.sort_stats(sort)
        if callers:
            stats.print_callers(r"agent_run/storage/(sqlite|preflight|run_scope)\.py", 20)
        else:
            stats.print_stats(40)
        lines = stream.getvalue().splitlines()
        bounded = (lines[:maximum - 1] + ["[table truncated; complete raw pstats retained]"]
                   if len(lines) > maximum else lines)
        text = "\n".join(bounded) + "\n"
        (folder / (name + ".txt")).write_text(text)
        print(name + " (wall time, not function CPU):\n" + text, flush=True)
        reports[name] = {"line_limit": maximum, "truncated": len(lines) > maximum}
    return reports


def run_phase(phase):
    if phase not in {"normal", "profile"}:
        raise ValueError("Only the fixed normal and profile phases are supported")
    binding = identity()
    if phase == "profile":
        normal_folder = RESULTS / "normal"
        normal = json.loads((normal_folder / "summary.json").read_text())
        if (normal.get("phase") != "normal" or normal.get("node") != NODE
                or normal.get("identity") != binding or normal.get("complete_product_pass") is not False
                or type(normal.get("pytest_exit_code")) is not int or normal["pytest_exit_code"] != 0
                or normal.get("junit_sha256") != digest(normal_folder / "junit.xml")):
            raise ValueError("Profile requires this candidate's successful normal phase")
        require_one_pass(normal_folder)
    folder = RESULTS / phase
    folder.mkdir(parents=True, exist_ok=False)  # Never overwrite an earlier phase result.
    state = Path(tempfile.mkdtemp(prefix="requirements-" + phase + "-",
                                  dir=os.environ["RUNNER_TEMP"]))
    arguments = ["-v", "-s", "-n", "0", NODE, "--basetemp=" + str(state / "pytest"),
                 "--junitxml=" + str(folder / "junit.xml")]
    os.environ["PYTEST_ADDOPTS"] = ""
    profiler = cProfile.Profile() if phase == "profile" else None
    if profiler is not None:
        profiler.enable()
    before = snapshot()
    result = pytest.main(arguments)
    if result != 0:
        raise SystemExit(result)  # Preserve pytest's code; no export or next phase.
    after = snapshot()
    if profiler is not None:
        profiler.disable()
    metrics = {name: after[name] - before[name] for name in before}
    if any(value < 0 for value in metrics.values()):
        raise ValueError("Fixed-story timing is not comparable")
    require_one_pass(folder)
    started = time.monotonic_ns()
    reports = export_profile(profiler, folder) if profiler is not None else {}
    export_wall_ns = time.monotonic_ns() - started
    report = {"stage": "native-requirements-profile", "phase": phase, "node": NODE,
              "identity": binding, "pid": os.getpid(), "pytest_exit_code": int(result),
              "metrics": metrics, "profile_timer": "builtin-wall" if profiler is not None else None,
              "profile_tables": reports, "export_wall_ns": export_wall_ns,
              "junit_sha256": digest(folder / "junit.xml"), "complete_product_pass": False,
              "scope": "pytest.main collection/setup/call/teardown plus boundary sampling. "
                       "Profile covers its enabling thread, not Git internals or other threads. "
                       "Child CPU covers only ended/waited children, not the whole process tree. "
                       "Profile wall includes waits; nested cumulative times are not additive. "
                       "Normal/profile differences are not pure profiling overhead or a speedup."}
    if profiler is not None:
        report["pstats_sha256"] = digest(folder / "profile.pstats")
    (folder / "summary.json").write_text(json.dumps(report, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("normal", "profile"))
    arguments = parser.parse_args()
    os.chdir(PROJECT)
    run_phase(arguments.phase)
