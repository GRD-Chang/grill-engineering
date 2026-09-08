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
from conftest import write_fixture


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
