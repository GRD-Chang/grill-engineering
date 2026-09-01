from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_locator import RunLocatorIndex
from agent_run.state_contract import IncompatibleRunStateError
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


def test_locator_registration_retries_only_for_new_pending_run(git_repo: Path) -> None:
    fixture_path = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    store = StateStore(git_repo / ".agent-run")

    class FailingLocator:
        def register(self, **_kwargs: object) -> None:
            raise OSError("simulated locator write failure")

    with pytest.raises(OSError, match="locator write failure"):
        Controller(
            FixtureGitHubReader(fixture_path),
            GitRepository(git_repo),
            store,
            locator=FailingLocator(),  # type: ignore[arg-type]
        ).start(1)

    pending = store.find_run("example/project", 1)
    assert pending is not None
    assert pending["locator_registration_pending"] is True
    locator = RunLocatorIndex(git_repo / "locator.json")
    recovered, resumed = Controller(
        FixtureGitHubReader(fixture_path),
        GitRepository(git_repo),
        store,
        locator=locator,
    ).start(1)

    assert resumed
    assert recovered.get("locator_registration_pending") is None
    assert locator.resolve_state_dir(str(recovered["run_id"])) == store.root


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
    recovered, resumed = controller.resume(run_id, explicit_resume=True)

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
        wrong.resume(str(state["run_id"]))


def test_legacy_state_fails_closed_before_controller_mutates_it(
    git_repo: Path,
) -> None:
    store = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    )
    state, _ = controller.start(1)
    state["schema_version"] = 1
    store.save_run(str(state["run_id"]), state)
    before = deepcopy(store.load_run(str(state["run_id"])))

    with pytest.raises(IncompatibleRunStateError, match="legacy state"):
        controller.resume(str(state["run_id"]))

    assert store.load_run(str(state["run_id"])) == before


def test_state_missing_active_invocation_fails_closed_before_controller_mutates_it(
    git_repo: Path,
) -> None:
    store = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    )
    state, _ = controller.start(1)
    state.pop("active_agent_invocation")
    store.save_run(str(state["run_id"]), state)
    before = deepcopy(store.load_run(str(state["run_id"])))

    with pytest.raises(IncompatibleRunStateError, match="active_agent_invocation"):
        controller.resume(str(state["run_id"]))

    assert store.load_run(str(state["run_id"])) == before


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


def test_credential_renewal_failure_uses_a_recoverable_diagnostic(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _issue(2)})
    store = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    ).start(1)
    run_id = str(state["run_id"])

    assert Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    ).record_execution_failure(
        run_id,
        "Worker GitHub read credential error: worker_credential_renewal_failed: retries exhausted; token=[REDACTED]",
    )

    recorded = store.load_run(run_id)
    assert recorded is not None
    assert recorded["terminal_kind"] == "execution_failed"
    assert recorded["diagnostics"] == [
        {
            "code": "worker_credential_renewal_failed",
            "message": "Worker GitHub read credential error: worker_credential_renewal_failed: retries exhausted; token=[REDACTED]",
            "operator_gate": {
                "work_subject": "ticket:2",
                "action_kind": "execution_failure",
                "phase": "active",
                "reason": "worker_credential_renewal_failed",
            },
        }
    ]


def test_execution_failure_producer_persists_one_bounded_safe_diagnostic(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _issue(2)})
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    )
    state, _ = controller.start(1)
    run_id = str(state["run_id"])
    credential = "ghp_1234567890abcdef"

    assert controller.record_execution_failure(
        run_id,
        f"command failed; token={credential}; " + ("raw tool output " * 20_000),
    )

    persisted_bytes = (store.runs_directory / f"{run_id}.json").read_bytes()
    recorded = store.load_run(run_id)
    assert recorded is not None
    assert len(recorded["diagnostics"]) == 1
    assert credential.encode() not in persisted_bytes
    assert len(recorded["diagnostics"][0]["message"].encode()) <= 8 * 1024
    assert b"raw tool output raw tool output raw tool output" in persisted_bytes
    assert len(persisted_bytes) < 64 * 1024


def test_resume_waits_for_repository_binding_to_recover(git_repo: Path) -> None:
    store = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(git_repo / "github.json", issues={"2": _issue(2)})
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    ).start(1)

    waiting, resumed = Controller(
        UnavailableRepositoryReader("example/project"),
        GitRepository(git_repo),
        store,
    ).resume(str(state["run_id"]))

    assert resumed
    assert waiting["status"] == "waiting_external"
    assert waiting["diagnostics"][0]["waiting_for"] == "GitHub repository binding"
    persisted = store.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["status"] == "waiting_external"
