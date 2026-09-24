from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from support.environment import _apply_test_environment, _inherited_git_override, _user_environment


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Work stealing initially gives each worker a contiguous part of the
    # collection. Start the real offline installer smoke early so its long
    # build does not become the final worker's tail.
    smoke = "tests/test_runner_installer.py::test_public_quickstart_smoke_uses_login_shell_and_cleans_resources"
    for index, item in enumerate(items):
        if item.nodeid == smoke:
            items.insert(0, items.pop(index))
            break


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Helpers and CLI children share one private user environment per case."""

    _apply_test_environment(tmp_path / "user", monkeypatch)


@pytest.fixture(scope="session")
def git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once per worker; tests only receive independent copies."""

    root = tmp_path_factory.mktemp("git-template")
    environment = {
        key: value for key, value in os.environ.items() if not _inherited_git_override(key)
    } | _user_environment(root / "user")
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=environment, check=True, capture_output=True)
    # Automatic maintenance may outlive commit and mutate this shared template
    # during copytree. Disable dispatch before the first commit; copies inherit it.
    subprocess.run(
        ["git", "config", "--local", "maintenance.auto", "false"],
        cwd=repo,
        env=environment,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Agent Run Tests"], cwd=repo, env=environment, check=True)
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=repo,
        env=environment,
        check=True,
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = [\n'
        '  "fixture-ci::fixture-required-check::Run tests",\n'
        '  "CI::quality::Run tests",\n'
        ']\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "pyproject.toml"], cwd=repo, env=environment, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, env=environment, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo(tmp_path: Path, git_template: Path) -> Path:
    return Path(shutil.copytree(git_template, tmp_path / "repo"))
