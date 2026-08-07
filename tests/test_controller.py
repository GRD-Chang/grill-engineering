from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture


def _issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Implement it.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def test_initial_state_failure_happens_before_branch_creation(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture_path),
        GitRepository(git_repo),
        store,
    )
    assert not hasattr(controller, "git")

    def fail_save(_run_id: str, _state: dict[str, Any]) -> None:
        raise OSError("simulated first-write interruption")

    monkeypatch.setattr(store, "save_run", fail_save)

    with pytest.raises(OSError, match="first-write interruption"):
        controller.start(1)

    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-run/"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert branches == []
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))


def test_base_fetch_failure_preserves_a_recoverable_run(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_path = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture_path), GitRepository(git_repo), store
    )

    def fail_resolve(_branch: str, _expected_sha: str | None) -> str:
        raise GitError("simulated exhausted fetch retry budget")

    monkeypatch.setattr(controller.publisher, "resolve_base", fail_resolve)
    failed, resumed = controller.start_or_resume_unfinished(1)

    assert not resumed
    assert failed["status"] == "execution_failed"
    assert failed["base_resolution_pending"] is True
    run_id = str(failed["run_id"])
    persisted = store.load_run(run_id)
    assert persisted is not None
    assert persisted["diagnostics"][0]["code"] == "base_resolution_failed"

    monkeypatch.undo()
    recovered, resumed = controller.resume(run_id)

    assert resumed
    assert recovered["run_id"] == run_id
    assert recovered["status"] == "active"
    assert recovered.get("base_resolution_pending") is None


def test_wrong_repository_cannot_mutate_existing_run_state(
    git_repo: Path,
) -> None:
    store = StateStore(git_repo / ".agent-run")
    correct_fixture = write_fixture(
        git_repo / "correct.json", issues={"2": _issue(2)}
    )
    state, _ = Controller(
        FixtureGitHubReader(correct_fixture),
        GitRepository(git_repo),
        store,
    ).start(1)
    wrong_fixture = write_fixture(
        git_repo / "wrong.json",
        issues={"2": _issue(2)},
        repository="other/project",
    )
    wrong = Controller(
        FixtureGitHubReader(wrong_fixture),
        GitRepository(git_repo),
        store,
    )

    wrong.record_execution_failure(str(state["run_id"]), "wrong repo")

    persisted = store.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["status"] == "active"
    with pytest.raises(ValueError, match="does not match"):
        wrong.confirm_structure(str(state["run_id"]))


class UnavailableRepositoryReader:
    def __init__(self, repository_hint: str) -> None:
        self._repository_hint = repository_hint

    def repository_hint(self) -> str:
        return self._repository_hint

    def repository(self) -> Any:
        raise GitHubReadError(
            "github_read_failed", "simulated repository outage"
        )

    def delivery_graph(self, _parent_number: int) -> Any:
        raise GitHubReadError(
            "github_read_failed", "simulated repository outage"
        )


def test_repository_hint_guards_failure_record_when_remote_is_unavailable(
    git_repo: Path,
) -> None:
    store = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    state, _ = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        store,
    ).start(1)
    run_id = str(state["run_id"])
    before = store.load_run(run_id)
    assert before is not None

    wrong = Controller(
        UnavailableRepositoryReader("other/project"),
        GitRepository(git_repo),
        store,
    )
    assert not wrong.record_execution_failure(run_id, "wrong repo")
    assert store.load_run(run_id) == before

    correct = Controller(
        UnavailableRepositoryReader("example/project"),
        GitRepository(git_repo),
        store,
    )
    assert correct.record_execution_failure(run_id, "correct repo outage")
    recorded = store.load_run(run_id)
    assert recorded is not None
    assert recorded["status"] == "execution_failed"
    assert recorded["terminal_kind"] == "execution_failed"
