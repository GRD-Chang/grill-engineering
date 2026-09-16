from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_run.executor_environment import default_executor_runtime_directory
from agent_run.github_auth_profile import GitHubAppProfileStore
from agent_run.paths import app_config_root, app_data_root, app_runtime_root
from agent_run.run_locator import RunLocatorIndex
from agent_run.runner_installer import InstallPaths
from agent_run.runner_lease import default_runner_lock_path
from agent_run.user_defaults import UserDefaultsStore


def test_all_runner_paths_share_the_selected_roots() -> None:
    data = app_data_root()
    config = app_config_root()
    assert InstallPaths.from_environment().data_root == data
    assert default_runner_lock_path() == data / "install.lock"
    assert RunLocatorIndex.default().path == data / "run-locator.json"
    assert UserDefaultsStore.default_path() == config / "user-defaults.json"
    assert GitHubAppProfileStore.default_path() == config / "github-app.json"
    assert default_executor_runtime_directory() == app_runtime_root() / "executor"
    assert not data.exists()
    assert not config.exists()


@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_xdg_uses_defaults(
    monkeypatch: pytest.MonkeyPatch, value: str | None,
) -> None:
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    assert app_data_root() == Path.home() / ".local" / "share" / "agent-run"
    assert app_config_root() == Path.home() / ".config" / "agent-run"
    assert app_runtime_root() == Path("/tmp") / f"agent-run-{os.geteuid()}"


@pytest.mark.parametrize("variable,function", [
    ("XDG_DATA_HOME", app_data_root),
    ("XDG_CONFIG_HOME", app_config_root),
    ("XDG_RUNTIME_DIR", app_runtime_root),
])
def test_relative_xdg_directories_fail_instead_of_writing_under_cwd(
    monkeypatch: pytest.MonkeyPatch, variable: str, function: Callable[[], Path],
) -> None:
    monkeypatch.setenv(variable, "relative-directory")
    with pytest.raises(ValueError, match="必须是绝对路径"):
        function()
