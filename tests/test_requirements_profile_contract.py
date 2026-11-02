"""Fixed-runner contracts with fake pytest/profiler; no business or profiling run."""
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest


SCRIPT = Path(__file__).parent / "diagnostics" / "requirements_profile.py"
BINDING = {"GITHUB_REPOSITORY": "GRD-Chang/grill-engineer", "GITHUB_SHA": "a" * 40,
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "tree": "b" * 40,
           "GITHUB_WORKFLOW_REF": "GRD-Chang/grill-engineer/.github/workflows/ci.yml@refs/pull/505/merge"}


def junit(path, node, *, fault=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    module, name = node.split("::")
    key = {"classname": module.removesuffix(".py").replace("/", "."), "name": name}
    case = ET.SubElement(suite, "testcase", **key)
    if fault == "missing":
        return
    if fault == "empty":
        suite.remove(case)
        suite.set("tests", "0")
    elif fault in {"failure", "error", "skipped"}:
        ET.SubElement(case, fault)
    elif fault in {"extra", "duplicate"}:
        ET.SubElement(suite, "testcase", **{**key, "name": name if fault == "duplicate" else "unexpected"})
        suite.set("tests", "2")
    elif fault == "count":
        suite.set("tests", "2")
    path.write_text(ET.tostring(root, encoding="unicode"))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    before = list(sys.path)
    module = runpy.run_path(str(SCRIPT))
    sys.path[:] = before
    namespace = module["run_phase"].__globals__
    original_identity = namespace["identity"]
    monkeypatch.setitem(namespace, "RESULTS", tmp_path / "results")
    monkeypatch.setitem(namespace, "identity", lambda: dict(BINDING))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--maxfail=1 -k accidental")
    events, calls, snapshots = [], [], []
    behavior = {"code": 0, "error": None, "junit": None}

    def main(arguments):
        events.append("pytest")
        assert namespace["os"].environ["PYTEST_ADDOPTS"] == ""
        calls.append(arguments)
        path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--junitxml=")))
        junit(path, namespace["NODE"], fault=behavior["junit"])
        if behavior["error"] is not None:
            raise behavior["error"]
        return behavior["code"]

    def snapshot():
        events.append("snapshot")
        tick = len(snapshots) + 1
        result = {"wall_ns": tick * 100, "process_cpu_ns": tick * 20,
                  "main_thread_cpu_ns": tick * 10, "waited_children_user_seconds": tick * 0.1,
                  "waited_children_system_seconds": tick * 0.01}
        snapshots.append(result)
        return result

    profiler = SimpleNamespace(enable=lambda: events.append("enable"),
                               disable=lambda: events.append("disable"))
    constructor = Mock(return_value=profiler)

    def export(value, folder):
        assert value is profiler
        events.append("export")
        (folder / "profile.pstats").write_bytes(b"unit-test placeholder, not actual pstats")
        return {}

    monkeypatch.setattr(namespace["pytest"], "main", main)
    monkeypatch.setattr(namespace["cProfile"], "Profile", constructor)
    monkeypatch.setitem(namespace, "snapshot", snapshot)
    monkeypatch.setitem(namespace, "export_profile", export)
    return SimpleNamespace(run=namespace["run_phase"], namespace=namespace, events=events,
        calls=calls, snapshots=snapshots, behavior=behavior, constructor=constructor,
        identity=original_identity, export=module["export_profile"], root=tmp_path / "results")


def test_two_fixed_phases_keep_scope_identity_separate_state_and_default_timer(runner):
    runner.run("normal")
    assert runner.events == ["snapshot", "pytest", "snapshot"]
    runner.run("profile")
    assert runner.events == ["snapshot", "pytest", "snapshot", "enable", "snapshot", "pytest",
                             "snapshot", "disable", "export"]
    runner.constructor.assert_called_once_with()
    assert len(runner.calls) == 2
    states = []
    for phase, arguments in zip(("normal", "profile"), runner.calls):
        assert arguments[:5] == ["-v", "-s", "-n", "0", runner.namespace["NODE"]]
        states.append(arguments[5])
        report = json.loads((runner.root / phase / "summary.json").read_text())
        assert report["identity"] == BINDING and report["complete_product_pass"] is False
        assert report["metrics"]["process_cpu_ns"] == 20
        assert report["metrics"]["main_thread_cpu_ns"] == 10
        assert report["metrics"]["wall_ns"] == 100
        assert report["node"] == runner.namespace["NODE"] and report["pytest_exit_code"] == 0
    assert states[0] != states[1]


@pytest.mark.parametrize("phase", ["normal", "profile"])
@pytest.mark.parametrize("code", [1, 2, 5])
def test_nonzero_pytest_return_stops_with_original_code_without_export(runner, phase, code):
    if phase == "profile":
        runner.run("normal")
    runner.events.clear()
    runner.behavior["code"] = code
    with pytest.raises(SystemExit) as caught:
        runner.run(phase)
    assert caught.value.code == code
    assert runner.events == (["enable"] if phase == "profile" else []) + ["snapshot", "pytest"]
    assert not (runner.root / phase / "summary.json").exists()


@pytest.mark.parametrize("phase", ["normal", "profile"])
@pytest.mark.parametrize("error", [RuntimeError("original"), TimeoutError("watchdog"),
                                   KeyboardInterrupt("cancel"), SystemExit(17)])
def test_direct_exception_has_no_finally_probe_or_replay(runner, phase, error):
    if phase == "profile":
        runner.run("normal")
    runner.events.clear()
    runner.behavior["error"] = error
    with pytest.raises(type(error)) as caught:
        runner.run(phase)
    assert caught.value is error
    assert runner.events == (["enable"] if phase == "profile" else []) + ["snapshot", "pytest"]
    assert not (runner.root / phase / "summary.json").exists()


@pytest.mark.parametrize("phase", ["normal", "profile"])
@pytest.mark.parametrize("fault", ["missing", "empty", "failure", "error", "skipped", "extra", "duplicate", "count"])
def test_zero_return_without_one_exact_pass_is_rejected(runner, fault, phase):
    if phase == "profile":
        runner.run("normal")
    runner.behavior["junit"] = fault
    with pytest.raises((ValueError, FileNotFoundError)):
        runner.run(phase)
    assert len(runner.calls) == (2 if phase == "profile" else 1) and "export" not in runner.events
    assert not (runner.root / phase / "summary.json").exists()


@pytest.mark.parametrize("field,value", [("identity", {**BINDING, "tree": "c" * 40}),
    ("identity", {**BINDING, "GITHUB_SHA": "c" * 40}),
    ("identity", {**BINDING, "GITHUB_RUN_ATTEMPT": "2"}), ("phase", "profile"),
    ("node", "unexpected"), ("pytest_exit_code", 1), ("pytest_exit_code", False),
    ("complete_product_pass", True),
    ("junit_sha256", "wrong")])
def test_profile_rejects_wrong_normal_binding_before_running(runner, field, value):
    runner.run("normal")
    path = runner.root / "normal" / "summary.json"
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="successful normal phase"):
        runner.run("profile")
    assert len(runner.calls) == 1 and "enable" not in runner.events


def test_profile_cannot_run_without_a_successful_normal_phase_or_overwrite_it(runner):
    with pytest.raises(FileNotFoundError):
        runner.run("profile")
    assert runner.calls == []
    runner.run("normal")
    with pytest.raises(FileExistsError):
        runner.run("normal")
    assert len(runner.calls) == 1


@pytest.mark.parametrize("error", [OSError("export"), TimeoutError("watchdog"), KeyboardInterrupt("cancel")])
def test_export_failure_is_not_a_success_and_does_not_replay(runner, monkeypatch, error):
    runner.run("normal")

    def fail(*_):
        raise error

    monkeypatch.setitem(runner.namespace, "export_profile", fail)
    with pytest.raises(type(error)) as caught:
        runner.run("profile")
    assert caught.value is error and len(runner.calls) == 2
    assert not (runner.root / "profile" / "summary.json").exists()


@pytest.mark.parametrize("field,value", [("GITHUB_REPOSITORY", "other/repository"),
    ("GITHUB_SHA", "bad"), ("GITHUB_RUN_ID", ""), ("GITHUB_RUN_ATTEMPT", "0"),
    ("GITHUB_WORKFLOW_REF", "other-workflow")])
def test_ci_identity_rejects_missing_or_wrong_source(runner, monkeypatch, field, value):
    for key in runner.namespace["IDENTITY_KEYS"]:
        monkeypatch.setenv(key, BINDING[key])
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError, match="CI identity"):
        runner.identity()


def test_actual_checkout_must_match_ci_sha_and_have_an_exact_tree(runner, monkeypatch):
    for key in runner.namespace["IDENTITY_KEYS"]:
        monkeypatch.setenv(key, BINDING[key])
    check = Mock(return_value=BINDING["GITHUB_SHA"] + "\n" + BINDING["tree"] + "\n")
    monkeypatch.setattr(runner.namespace["subprocess"], "check_output", check)
    assert runner.identity() == BINDING
    for output in ("c" * 40 + "\n" + BINDING["tree"], BINDING["GITHUB_SHA"] + "\nbad", "missing"):
        check.return_value = output
        with pytest.raises(ValueError, match="checkout identity"):
            runner.identity()


def test_export_tables_have_hard_line_limits_and_keep_the_raw_profile(runner, monkeypatch):
    class Stats:
        def __init__(self, _):
            self.stream = None
        def sort_stats(self, _):
            return self
        def print_stats(self, count):
            assert count == 40
            self.stream.write("wall row\n" * 300)
        def print_callers(self, focus, count):
            assert focus == r"agent_run/storage/(sqlite|preflight|run_scope)\.py" and count == 20
            self.stream.write("caller row\n" * 300)

    monkeypatch.setattr(runner.namespace["pstats"], "Stats", Stats)
    profiler = SimpleNamespace(dump_stats=lambda path: Path(path).write_bytes(b"raw profile"))
    folder = runner.root / "tables"
    folder.mkdir(parents=True)
    report = runner.export(profiler, folder)
    assert (folder / "profile.pstats").read_bytes() == b"raw profile"
    for name, maximum in (("self_wall", 60), ("cumulative_wall", 60), ("callers_wall", 80)):
        assert report[name] == {"line_limit": maximum, "truncated": True}
        assert len((folder / (name + ".txt")).read_text().splitlines()) == maximum
