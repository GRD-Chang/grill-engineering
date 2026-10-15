"""Local credential selection while a lifecycle Action still owns a Run."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_run.git import GitRepository
from agent_run.github import GhGitHubReader
from agent_run.runner_identity import bind_runner_identity
from agent_run.state import StateStore
from agent_run.task_control import TaskControlError, TaskControlStore
from agent_run.user_defaults import UserDefaultsStore


@pytest.mark.parametrize("source", ["host", "pat", "app"])
@pytest.mark.parametrize("kind,binding", [("run", "action"), ("approve", "action"),
                                          ("resume", "payload")])
def test_pending_action_uses_bound_run_identity_not_current_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, kind: str, binding: str,
) -> None:
    identity = {"source": source}
    if source == "pat":
        identity["pat"] = {"env": "FROZEN_PAT_REFERENCE"}
    if source == "app":
        identity.update(app_profile={"app_id": "123", "installation_id": "456",
                                     "private_key_path": str(tmp_path / "key")},
                        app_read_only=False, app_current_repository_only=False)
    # No token resolution is needed to observe the existing Executor.
    UserDefaultsStore().configure(github={"runner_source": "pat",
                                          "pat": {"env": "DIFFERENT_DEFAULT_REFERENCE"}})
    action = {"status": "applying", "kind": kind, "payload": {"parent": 1}}
    target = action if binding == "action" else action["payload"]
    target["run_id"] = "finished-run"
    monkeypatch.setattr(TaskControlStore, "load", lambda *_: {"action": action})
    states = Mock(spec=StateStore, root=tmp_path / "state")
    states.find_unfinished_runs.return_value = []
    states.load_current_run.return_value = {
        "status": "completed", "runner_github_identity": identity,
    }
    git = GitRepository(tmp_path)
    reader = GhGitHubReader("example/project")
    bind_runner_identity(argparse.Namespace(command="run", repo="example/project", parent=1),
                         states, git, reader, None)
    assert git.runner_identity == reader.runner_identity == identity
    states.load_current_run.assert_called_once_with("finished-run")
    states.save_run.assert_not_called()


@pytest.mark.parametrize("kind", ["run", "approve"])
def test_missing_bound_run_does_not_fall_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    monkeypatch.setattr(TaskControlStore, "load", lambda *_: {"action": {
        "status": "accepted", "kind": kind, "run_id": "missing-run",
        "payload": {"runner_github_identity": {"source": "host"}},
    }})
    states = Mock(spec=StateStore, root=tmp_path / "state")
    states.find_unfinished_runs.return_value = []
    states.load_current_run.return_value = None
    with pytest.raises(TaskControlError, match="bound Run is missing"):
        bind_runner_identity(argparse.Namespace(command="run", repo="example/project", parent=1),
                             states, GitRepository(tmp_path), GhGitHubReader("example/project"), None)


def test_unbound_run_action_uses_frozen_creation_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {"source": "pat", "pat": {"env": "FROZEN_PAT_REFERENCE"}}
    monkeypatch.setattr(TaskControlStore, "load", lambda *_: {"action": {
        "status": "accepted", "kind": "run", "run_id": None,
        "payload": {"runner_github_identity": identity},
    }})
    states = Mock(spec=StateStore, root=tmp_path / "state")
    states.find_unfinished_runs.return_value = []
    git = GitRepository(tmp_path)
    bind_runner_identity(argparse.Namespace(command="run", repo="example/project", parent=1),
                         states, git, GhGitHubReader("example/project"), None)
    assert git.runner_identity == identity
    states.load_current_run.assert_not_called()
