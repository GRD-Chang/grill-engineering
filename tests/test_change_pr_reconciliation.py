"""Read-only settlement keeps unknown write authority across invalidation."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.change_pr_reconciliation import reconcile_pending_pr_write
from agent_run.github import GitHubReadError
from agent_run.object_state import save_owned


def boundary() -> tuple[dict[str, Any], dict[str, Any]]:
    job = {
        "phase": "publishing", "publication_sha": "original-head",
        "ticket_write_intent": {
            "action": "ensure_ticket_pr", "branch": "source", "base_branch": "main",
            "head_sha": "original-head", "base_sha": "old-base", "pr_number": 12,
            "prior_narrative": {"title": "Old", "body": "Old body"},
            "target_narrative": {"title": "Target", "body": "Target body"},
        },
    }
    return {"repository": "example/project"}, job


@pytest.mark.parametrize("observation", ["prior", "target", "third", "closed", "foreign_head", "read_failure"])
def test_settlement_uses_original_pr_and_text_without_replaying(observation: str) -> None:
    state, job = boundary()
    original = deepcopy(job)
    reads: list[int] = []
    live = {
        "state": "CLOSED" if observation == "closed" else "OPEN",
        "head_branch": "source", "head_sha": "foreign" if observation == "foreign_head" else "original-head",
        "head_repository": "example/project", "base_repository": "example/project",
        "base_branch": "main", "base_sha": "advanced-base",
    }

    def narrative(number: int) -> dict[str, str]:
        reads.append(number)
        if observation == "read_failure":
            raise GitHubReadError("github_timeout", "read unavailable")
        return original["ticket_write_intent"].get(
            f"{observation}_narrative", {"title": "External", "body": "External body"},
        )

    def block(_state: Any, current: Any, code: str, message: str) -> None:
        current.update(phase="blocked", execution_resume_phase=current["phase"])
        state["diagnostics"] = [{"code": code, "message": message}]

    stage = SimpleNamespace(
        github=SimpleNamespace(live_pull_request=lambda number: live, read_run_pr_narrative=narrative),
        save=lambda _state: None, _block=block,
    )  # No remote mutation methods: settlement must use only read APIs.
    settled = reconcile_pending_pr_write(stage, state, job)
    assert settled is (observation in {"prior", "target"})
    assert job["publication_sha"] == "original-head"
    if settled:
        assert job["pr_number"] == 12
        assert "ticket_write_intent" not in job
    else:
        assert job["ticket_write_intent"] == original["ticket_write_intent"]
        assert job["phase"] == "blocked"
        assert job["execution_resume_phase"] == "publishing"
    assert reads == ([] if observation in {"closed", "foreign_head"} else [12])


def test_failed_settlement_save_restores_original_intent_and_pr_identity() -> None:
    state, job = boundary()
    original = deepcopy(job)
    live = {
        "state": "OPEN", "head_branch": "source", "head_sha": "original-head",
        "head_repository": "example/project", "base_repository": "example/project", "base_branch": "main",
    }

    def save(_state: Any) -> None:
        assert "ticket_write_intent" not in job
        raise OSError("settlement checkpoint unavailable")

    stage = SimpleNamespace(
        github=SimpleNamespace(live_pull_request=lambda number: live,
                               read_run_pr_narrative=lambda number: original["ticket_write_intent"]["target_narrative"]),
        save=save,
    )
    with pytest.raises(OSError, match="checkpoint unavailable"):
        reconcile_pending_pr_write(stage, state, job)
    assert job == original


@pytest.mark.parametrize("observation", ["absent", "closed", "ambiguous"])
def test_creation_recovery_lookup_includes_closed_and_ambiguous_objects(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, observation: str,
) -> None:
    from agent_run.git import GitRepository
    from agent_run.github_publish import GhGitHubPublisher

    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    pulls = [] if observation == "absent" else [{"number": 12, "state": "closed"}]
    if observation == "ambiguous":
        pulls.append({"number": 13, "state": "open"})

    def read(branch: str, *, state: str) -> list[dict[str, Any]]:
        assert branch == "original-branch" and state == "all"
        return pulls

    monkeypatch.setattr(publisher, "_change_prs", read)
    if observation == "ambiguous":
        with pytest.raises(GitHubReadError, match="ambiguous"):
            publisher.find_change_pr_number(branch="original-branch")
    else:
        assert publisher.find_change_pr_number(branch="original-branch") == (None if not pulls else 12)


def test_run_repair_checks_pending_write_before_source_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_run.run_repair_lifecycle import RunRepairLifecycle

    intent = {"branch": "run", "base_sha": "base", "candidate_sha": "candidate", "default_base_sha": "default"}
    state: dict[str, Any] = {"run_id": "run-1"}
    job: dict[str, Any] = {"phase": "accepted", "run_branch_write_intent": dict(intent)}
    owner = SimpleNamespace(github=object(), _repair_checkout=lambda _state: Path("unused"))
    lifecycle = RunRepairLifecycle(owner, SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_repair_job", lambda _state, _run: job)

    def reconcile(_owner: Any, _promotion: Any, _state: Any, current: Any) -> bool:
        assert current["run_branch_write_intent"] == intent
        current.update(phase="blocked", blocked_reason="run_branch_write_failed")
        return False

    def forbidden(*_args: Any) -> None:
        raise AssertionError("checkout or integration must not precede unresolved Run Branch write")

    monkeypatch.setattr("agent_run.run_repair_lifecycle.update_run_branch", reconcile)
    monkeypatch.setattr("agent_run.run_repair_lifecycle.require_change_checkout", forbidden)
    monkeypatch.setattr("agent_run.run_repair_lifecycle.prepare_delivery_integration", forbidden)
    assert lifecycle._repair(state, {}) == "blocked"
    assert job["run_branch_write_intent"] == intent


@pytest.mark.parametrize("invalidation", ["publication", "phase"])
def test_invalidated_change_write_survives_reload_until_original_result_is_known(
    tmp_path: Path, invalidation: str,
) -> None:
    from agent_run.state import StateStore

    state, job = boundary()
    if invalidation == "publication":
        job["publication_sha"] = "replacement-head"
    else:
        job["phase"] = "stale"
    state["job"] = job
    states = StateStore(tmp_path / "states")
    states.save_run("test", state)
    original = deepcopy(job)
    reads: list[int] = []
    target = original["ticket_write_intent"]["target_narrative"]
    observed = {"title": "Maintainer", "body": "Unconfirmed text"}

    def read(number: int) -> dict[str, str]:
        reads.append(number)
        return observed

    def block(current: dict[str, Any], current_job: dict[str, Any], code: str, message: str) -> None:
        current_job.update(phase="blocked", execution_resume_phase=current_job["phase"])
        current["diagnostics"] = [{"code": code, "message": message}]
        save_owned(states, "test", current, fields={'diagnostics', 'job'})

    stage = SimpleNamespace(
        github=SimpleNamespace(
            live_pull_request=lambda number: {
                "state": "OPEN", "head_branch": "source", "head_sha": "original-head",
                "head_repository": "example/project", "base_repository": "example/project",
                "base_branch": "main", "base_sha": "advanced-base",
            },
            read_run_pr_narrative=read,
        ),
        save=lambda current: save_owned(states, "test", current, fields={'diagnostics', 'job'}), _block=block,
    )
    loaded = StateStore(states.root).load_run("test")
    assert loaded is not None
    assert not reconcile_pending_pr_write(stage, loaded, loaded["job"])
    reloaded = StateStore(states.root).load_run("test")
    assert reloaded is not None
    assert reloaded["job"]["ticket_write_intent"] == original["ticket_write_intent"]
    assert reloaded["job"]["publication_sha"] == original["publication_sha"]
    assert reloaded["job"]["execution_resume_phase"] == original["phase"]
    # A later exact target observation permits only settlement of the original
    # PR. No write API is available on this Publisher, even after invalidation.
    observed = target
    assert reconcile_pending_pr_write(stage, reloaded, reloaded["job"])
    final = StateStore(states.root).load_run("test")
    assert final is not None
    assert final["job"]["pr_number"] == 12
    assert final["job"]["publication_sha"] == original["publication_sha"]
    assert "ticket_write_intent" not in final["job"]
    assert reads == [12, 12]
