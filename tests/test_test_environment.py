"""Exercise pytest with a simulated user home, never the real user's state."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_pytest_isolates_each_case_and_cli_from_host_state(tmp_path: Path) -> None:
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

@pytest.mark.parametrize("case", ["first", "second"])
def test_case(git_repo, tmp_path, case):
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[key]).is_relative_to(tmp_path), key
    assert not RunLocatorIndex.default().entries()
    assert (git_repo / "README.md").read_text() == "# fixture\\n"
    assert not (git_repo / "previous-case").exists()
    assert subprocess.check_output(["git", "config", "user.name"], cwd=git_repo, text=True).strip() == "Agent Run Tests"
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
    locator = Path(environment["XDG_STATE_HOME"]) / "agent-run/run-locator.json"
    locator.parent.mkdir()
    locator.write_text("host locator must not be read or overwritten", encoding="utf-8")
    before = {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()}
    environment["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(project / "tests")])
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "nested-tmp"), str(suite)],
        cwd=suite, env=environment, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert {path.relative_to(host): path.read_bytes() for path in host.rglob("*") if path.is_file()} == before
