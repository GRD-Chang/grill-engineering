"""路径定位在 Git 追加期间不得改写另一个线程的进程环境。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest

from agent_run import managed_workspace as product_workspace
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.paths import app_data_root
from support import workspace
from support.observer_errors import ObserverErrors


def test_explicit_workspace_lookup_does_not_change_globals_during_git_append(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, directory = os.environ.copy(), Path.cwd()
    streams = (sys.stdout, sys.stderr)
    ready, appended = Event(), Event()
    errors = ObserverErrors()
    overrides = {"XDG_DATA_HOME": str(tmp_path / "other-data"), "OBSERVER_ONLY": "private"}
    located: list[ManagedWorkspace] = []

    def paused_root(environment: Mapping[str, str] | None = None) -> Path:
        ready.set()
        assert appended.wait(5), "Git append did not release the lookup"
        return app_data_root(environment)

    def lookup() -> None:
        try:
            with errors.capture():
                located.append(workspace.managed_workspace(git_repo, extra_env=overrides))
        except BaseException:
            # The independent channel is checked by the main test after join.
            pass

    monkeypatch.setattr(workspace, "app_data_root", paused_root, raising=False)
    monkeypatch.setattr(product_workspace, "app_data_root", paused_root)
    worker = Thread(target=lookup, name="workspace-notification-reader")
    worker.start()
    try:
        assert ready.wait(5)
        assert os.environ.copy() == environment
        assert Path.cwd() == directory and (sys.stdout, sys.stderr) == streams
        (git_repo / "concurrent.txt").write_text("real Git append\n")
        for arguments in (("add", "concurrent.txt"), ("commit", "-m", "concurrent append")):
            subprocess.run(["git", *arguments], cwd=git_repo, env=environment,
                           check=True, capture_output=True, timeout=10)
    finally:
        appended.set()
        worker.join(5)
    assert not worker.is_alive()
    errors.raise_if_any()
    assert len(located) == 1
    assert located[0].state_root == tmp_path / "other-data/agent-run/repositories/example/project/state"
    assert subprocess.check_output(["git", "show", "HEAD:concurrent.txt"], cwd=git_repo,
                                   env=environment, text=True, timeout=10) == "real Git append\n"
    assert os.environ.copy() == environment


@pytest.mark.parametrize("xdg", ["absolute", "fallback", "tilde"], ids=["xdg", "home-fallback", "home-tilde"])
def test_workspace_creation_overrides_are_process_local(
    git_repo: Path, tmp_path: Path, xdg: str,
) -> None:
    before = os.environ.copy()
    overrides = {"HOME": str(tmp_path / "private-home"),
                 "XDG_DATA_HOME": {"absolute": str(tmp_path / "private-data"),
                                   "fallback": "", "tilde": "~/private-data"}[xdg]}
    created = workspace.prepare_workspace(git_repo, extra_env=overrides)
    root = tmp_path / {"absolute": "private-data", "fallback": "private-home/.local/share",
                       "tilde": "private-home/private-data"}[xdg]
    assert created.state_root == root / "agent-run/repositories/example/project/state"
    assert created.repository_root.joinpath("README.md").read_text() == "# fixture\n"
    assert created.repository_root.joinpath(".git").is_dir()
    assert os.environ.copy() == before
