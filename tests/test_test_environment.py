"""Exercise pytest with a simulated user home, never the real user's state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


_GIT_LOCATION_VARIABLES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_TEMPLATE_DIR",
)


def _assert_python_and_git_environment() -> None:
    python_directory = Path(sys.executable).parent
    assert Path(os.environ["PATH"].split(os.pathsep)[0]) == python_directory
    executable, prefix, version = json.loads(subprocess.check_output(
        ["env", "python3", "-c",
         "import json, sys; print(json.dumps([sys.executable, sys.prefix, list(sys.version_info)]))"],
        text=True,
    ))
    assert Path(executable).parent == python_directory
    assert prefix == sys.prefix
    assert version == list(sys.version_info)
    assert not set(_GIT_LOCATION_VARIABLES).intersection(os.environ)
    assert {
        key: value for key, value in os.environ.items() if key.startswith("GIT_CONFIG")
    } == {"GIT_CONFIG_NOSYSTEM": "1"}


def test_pytest_isolates_each_case_and_cli_from_host_state(tmp_path: Path) -> None:
    _assert_python_and_git_environment()
    project = Path(__file__).resolve().parents[1]
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(project / "tests/conftest.py", suite / "conftest.py")
    (suite / "test_isolation.py").write_text(
        '''import os
import subprocess
from pathlib import Path
import pytest
from agent_run.run_locator import RunLocatorIndex
from conftest import seed_run, write_fixture
from test_cli import run_cli, stdout_json
from test_test_environment import _assert_python_and_git_environment

@pytest.mark.parametrize("case", ["first", "second"])
def test_case(git_repo, tmp_path, monkeypatch, case):
    _assert_python_and_git_environment()
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[key]).is_relative_to(tmp_path), key
    assert not RunLocatorIndex.default().entries()
    assert (git_repo / "README.md").read_text() == "# fixture\\n"
    assert not (git_repo / "previous-case").exists()
    assert subprocess.check_output(["git", "config", "user.name"], cwd=git_repo, text=True).strip() == "Agent Run Tests"
    inherited = subprocess.run(["git", "config", "--get", "isolation.inherited"], cwd=git_repo)
    assert inherited.returncode == 1
    private_config = Path(os.environ["HOME"]) / ".gitconfig"
    assert subprocess.check_output(["git", "config", "--global", "user.name"], text=True).strip() == "Agent Run Tests"
    private_config.write_text(private_config.read_text() + f"[isolation]\\n    private = {case}\\n")
    assert subprocess.check_output(["git", "config", "isolation.private"], cwd=git_repo, text=True).strip() == case
    with monkeypatch.context() as explicit:
        explicit.setenv("GIT_CONFIG_COUNT", "1")
        explicit.setenv("GIT_CONFIG_KEY_0", "isolation.injected")
        explicit.setenv("GIT_CONFIG_VALUE_0", case)
        assert subprocess.check_output(["git", "config", "isolation.injected"], cwd=git_repo, text=True).strip() == case
    assert "GIT_CONFIG_COUNT" not in os.environ
    fixture = write_fixture(git_repo / "github.json", issues={})
    started = seed_run(git_repo, fixture)
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    assert [entry["run_id"] for entry in RunLocatorIndex.default().entries()] == [run_id]
    # Outside the checkout the subprocess must find the helper's index.
    result = run_cli(tmp_path, fixture, "status", run_id, "--json")
    assert result.returncode == 0, result.stderr
    assert stdout_json(result)["run_id"] == run_id
    (git_repo / "README.md").write_text("changed by this case")
    (git_repo / "previous-case").touch()
    subprocess.run(["git", "config", "user.name", "Changed User"], cwd=git_repo, check=True)
''',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    host = tmp_path / "host"
    for key in (
        "HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    ):
        directory = host / key.lower()
        directory.mkdir(parents=True)
        environment[key] = str(directory)
    locator = Path(environment["XDG_DATA_HOME"]) / "agent-run/run-locator.json"
    locator.parent.mkdir()
    locator.write_text("host locator must not be read or overwritten", encoding="utf-8")
    host_bin = host / "bin"
    host_bin.mkdir()
    wrong_python = host_bin / "python3"
    wrong_python.write_text("#!/bin/sh\necho wrong-host-python\nexit 93\n", encoding="utf-8")
    wrong_python.chmod(0o755)
    environment["PATH"] = str(host_bin) + os.pathsep + environment["PATH"]
    git_overrides = {}
    for key in _GIT_LOCATION_VARIABLES:
        target = host / key.lower()
        if key == "GIT_INDEX_FILE":
            target.write_text("host index must not change", encoding="utf-8")
        else:
            target.mkdir()
            (target / "keep").write_text("host Git directory must not change", encoding="utf-8")
        git_overrides[key] = str(target)
    host_config = host / "host-gitconfig"
    host_config.write_text("[isolation]\n    inherited = host\n", encoding="utf-8")
    shutil.copyfile(host_config, Path(environment["HOME"]) / ".gitconfig")
    git_overrides.update({
        "GIT_CONFIG": str(host_config),
        "GIT_CONFIG_GLOBAL": str(host_config),
        "GIT_CONFIG_SYSTEM": str(host_config),
        "GIT_CONFIG_PARAMETERS": "'isolation.inherited=host'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "isolation.inherited",
        "GIT_CONFIG_VALUE_0": "host",
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_FUTURE_OVERRIDE": "host",
    })
    environment.update(git_overrides)
    before = {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()}
    environment["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(project / "tests")])
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    git_trace = tmp_path / "git-trace.jsonl"
    environment["GIT_TRACE2_EVENT"] = str(git_trace)
    environment["GIT_TRACE2_ENV_VARS"] = ",".join(["HOME", "PATH", *git_overrides])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "nested-tmp"), str(suite)],
        cwd=suite, env=environment, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    events = [json.loads(line) for line in git_trace.read_text(encoding="utf-8").splitlines()]
    template_commands = [
        event for event in events if event.get("event") == "start"
        and event["argv"] in (["git", "init", "-b", "main"], ["git", "commit", "-m", "initial"])
    ]
    assert len(template_commands) == 2
    for command in template_commands:
        observed = {
            event["param"]: event["value"] for event in events
            if event.get("event") == "def_param" and event["sid"] == command["sid"]
        }
        # Git prepends its own exec path; the selected Python still precedes host tools.
        observed_path = observed["PATH"].split(os.pathsep)
        assert observed_path.index(str(Path(sys.executable).parent)) < observed_path.index(str(host_bin))
        assert Path(observed["HOME"]).is_relative_to(tmp_path / "nested-tmp")
        assert observed["GIT_CONFIG_NOSYSTEM"] == "1"
        assert not (set(git_overrides) - {"GIT_CONFIG_NOSYSTEM"}).intersection(observed)
    # Check actual Git child dispatch, including the template's initial commit.
    # Detached maintenance can otherwise keep mutating the template while copied.
    automatic_maintenance = [
        event["argv"]
        for event in events
        if event.get("event") == "child_start"
        and "--auto" in event["argv"]
        and {"maintenance", "gc"}.intersection(event["argv"])
    ]
    assert not automatic_maintenance, automatic_maintenance
    assert {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()} == before
