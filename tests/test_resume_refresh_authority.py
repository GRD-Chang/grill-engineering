from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_invocation import record_operator_stop
from agent_run.controller import Controller
from agent_run.external_supervision import wait_for_github_refresh
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.resume_intent import (
    ResumeIntentError,
    bind_resume_intent,
    validate_resume_intent,
)
from agent_run.state import StateStore
from support.seeding import write_fixture


@pytest.mark.parametrize("pause", ["operator_stopped", "supervision_timeout"])
@pytest.mark.parametrize("wait_at", ["binding", "refresh"])
@pytest.mark.parametrize(
    "drift", [None, "new_action", "persisted_pause", "refreshed_invocation"]
)
def test_resume_preserves_authority_across_persisted_refresh_wait(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    pause: str,
    wait_at: str,
    drift: str | None,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    run_id = state["run_id"]
    if pause == "operator_stopped":
        record_operator_stop(state, save=lambda value: states.save_run(run_id, value))
        pause_key = "operator_stop"
    else:
        state.update(
            status="supervision_timeout",
            supervision_wait={
                "resume_status": "waiting_external",
                "kind": "github_convergence",
                "waiting_for": "GitHub graph",
            },
        )
        states.save_run(run_id, state)
        pause_key = "supervision_wait"
    original_pause = deepcopy(state[pause_key])
    intent = bind_resume_intent(state)
    calls = 0
    original_binding = controller._load_bound_run
    original_refresh = controller._refresh

    def refresh(current: dict[str, Any], parent_number: int) -> dict[str, Any]:
        nonlocal calls
        if wait_at == "refresh":
            calls += 1
            if calls <= 2:
                wait_for_github_refresh(
                    current, code="github_read_failed", message="temporary outage",
                    waiting_for="GitHub graph",
                )
                return current
        refreshed = original_refresh(current, parent_number)
        if drift == "refreshed_invocation":
            refreshed["active_agent_invocation"] = {"binding_id": "replacement"}
        return refreshed

    def load_bound(run_id: str, *, state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        if wait_at == "binding":
            calls += 1
            if calls <= 2:
                raise GitHubReadError("github_read_failed", "temporary outage")
        return original_binding(run_id, state=state)

    monkeypatch.setattr(controller, "_refresh", refresh)
    monkeypatch.setattr(controller, "_load_bound_run", load_bound)

    def resume() -> dict[str, Any]:
        resumed, _ = controller.resume(
            run_id, explicit_resume=True, record_explicit_resume_audit=False,
            validate_resume_state=lambda current: validate_resume_intent(current, intent),
        )
        return resumed

    first = resume()
    assert first["status"] == "waiting_external"
    assert states.load_run(run_id)[pause_key] == original_pause
    first_window = deepcopy(first["supervision_window"])

    second = resume()
    assert second["status"] == "waiting_external"
    persisted = states.load_run(run_id)
    assert persisted[pause_key] == original_pause
    assert second["supervision_window"]["started_at"] == first_window["started_at"]
    assert second["supervision_window"]["deadline"] == first_window["deadline"]
    assert calls == 2

    if drift == "new_action":
        # A new Action after Executor loss binds the persisted wait, rather
        # than reusing an in-memory payload from the original Resume.
        intent = bind_resume_intent(persisted)
        assert intent["pause_reason"] == pause
    if drift == "persisted_pause":
        persisted[pause_key]["waiting_for"] = "a different pause"
        states.save_run(run_id, persisted)
    if drift in {"persisted_pause", "refreshed_invocation"}:
        with pytest.raises(ResumeIntentError, match="暂停对象或授权已变化"):
            resume()
        assert calls == (2 if drift == "persisted_pause" else 3)
        assert states.load_run(run_id)["status"] == "waiting_external"
    else:
        final = resume()
        assert calls == 3
        assert final["status"] not in {"operator_stopped", "supervision_timeout"}
        assert pause_key not in states.load_run(run_id)
        assert final.get("github_refresh_pending") is not True


def test_resume_preserves_unchanged_markdown_requirements(git_repo: Path) -> None:
    body = '- [ ] 保持任务正文\r\n- [x] 已确认\r\n示例：{ "enabled": true }'
    fixture = write_fixture(git_repo / "github.json", issues={
        "2": {"number": 2, "title": "Ticket", "body": body, "state": "OPEN",
              "labels": ["ready-for-agent"], "blocked_by": []},
    }, parent={"number": 1, "title": "Parent", "body": body,
               "sub_issues": [2], "sub_issue_order_reliable": True})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    record_operator_stop(state, save=lambda current: states.save_run(run_id, current))
    paused = states.load_current_run(run_id)
    assert paused is not None
    intent = bind_resume_intent(paused)

    resumed, _ = controller.resume(
        run_id, explicit_resume=True,
        validate_resume_state=lambda current: validate_resume_intent(current, intent),
    )

    assert resumed["status"] == "active"
    assert resumed["accepted_parent_spec_revision"] == state["accepted_parent_spec_revision"]
    assert resumed["accepted_ticket_graph_revision"] == state["accepted_ticket_graph_revision"]
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved["parent"]["body"] == body
    assert saved["ticket_graph"]["tickets"]["2"]["body"] == body


@pytest.mark.parametrize("wait_at", ["binding", "refresh"])
@pytest.mark.parametrize("drift", [False, True])
def test_ticket_executor_resume_keeps_authority_through_refresh_wait(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, wait_at: str, drift: bool,
) -> None:
    from support.ticket_parallel import ticket_run

    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    assert controller.record_execution_failure(run_id, "Controller failed before Agent startup")
    failed = states.load_current_run(run_id)
    assert failed is not None and failed["active_agent_invocation"] is None
    intent = bind_resume_intent(failed)
    calls = 0
    original_binding = controller._load_bound_run
    original_refresh = controller._refresh

    def load_bound(run_id: str, *, state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        if wait_at == "binding":
            calls += 1
            if calls <= 2:
                raise GitHubReadError("github_read_failed", "temporary outage")
        return original_binding(run_id, state=state)

    def refresh(current: dict[str, Any], parent_number: int) -> dict[str, Any]:
        nonlocal calls
        if wait_at == "refresh":
            calls += 1
            if calls <= 2:
                wait_for_github_refresh(
                    current, code="github_read_failed", message="temporary outage",
                    waiting_for="GitHub graph",
                )
                return current
        return original_refresh(current, parent_number)

    monkeypatch.setattr(controller, "_load_bound_run", load_bound)
    monkeypatch.setattr(controller, "_refresh", refresh)

    def resume() -> dict[str, Any]:
        resumed, _ = controller.resume(
            run_id, explicit_resume=True, record_explicit_resume_audit=False,
            validate_resume_state=lambda current: validate_resume_intent(current, intent),
        )
        return resumed

    first = resume()
    assert first["status"] == "waiting_external"
    second = resume()
    assert second["status"] == "waiting_external"
    assert second["supervision_window"]["started_at"] == first["supervision_window"]["started_at"]
    assert second["supervision_window"]["deadline"] == first["supervision_window"]["deadline"]
    if drift:
        changed = states.load_current_run(run_id)
        assert changed is not None
        changed["ticket_jobs"]["2"]["ticket_branch_generation"] = 2
        changed["active_ticket_job"] = deepcopy(changed["ticket_jobs"]["2"])
        states.save_run(run_id, changed)
        with pytest.raises(ResumeIntentError, match="暂停对象或授权已变化"):
            resume()
        assert calls == 2
    else:
        resumed = resume()
        assert resumed["status"] == "active"
        for number, job in failed["ticket_jobs"].items():
            assert {key: resumed["ticket_jobs"][number][key] for key in job} == job
        assert resumed.get("github_refresh_pending") is not True
        assert calls == 3
