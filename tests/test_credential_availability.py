"""凭据等待的字段组合在模块边界验证，真实 CLI 保留代表用例。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.credential_availability import wait_for_initial_credential
from agent_run.external_supervision import (
    ExternalSupervisor,
    public_supervision_snapshot,
    supervision_scope,
)
from agent_run.state import StateStore
from agent_run.waiting_presentation import waiting_presentation


@pytest.mark.parametrize("http_status", [None, 503], ids=["without-status", "http-503"])
def test_credential_timeout_preserves_optional_http_status_after_reopen(
    tmp_path: Path, http_status: int | None,
) -> None:
    now = [123.456789]
    sleeps: list[float] = []

    def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    supervisor = ExternalSupervisor(now=lambda: now[0], sleeper=advance)
    state: dict[str, Any] = {
        "run_id": "credential-run", "language": "zh", "status": "active",
        "active_agent_invocation": None, "agent_invocation_history": [],
    }
    with supervision_scope(supervisor):
        wait_for_initial_credential(
            state, work_subject="ticket-3", phase="developing",
            resume_status="active", http_status=http_status,
        )
        for _ in range(12):
            assert supervisor.before_retry(state)
        assert not supervisor.before_retry(state)

    assert len(sleeps) == 13
    assert sum(sleeps) == pytest.approx(600)
    assert state["status"] == "supervision_timeout"
    assert state["active_agent_invocation"] is None
    assert state["agent_invocation_history"] == []

    store = StateStore(tmp_path / "state")
    store.save_run("credential-run", state)
    loaded = StateStore(store.root).load_run("credential-run")
    assert loaded is not None
    availability = {
        "change_job": "ticket-3", "phase": "developing", "resume_status": "active",
        "failure_class": "credential_unavailable", "retry_count": 13,
    }
    if http_status is not None:
        availability["http_status"] = http_status
    assert loaded["credential_availability"] == availability
    wait = loaded["supervision_wait"]
    assert wait["kind"] == "github_convergence"
    assert "Worker credential availability" in wait["waiting_for"]
    assert wait["credential_failure_class"] == "credential_unavailable"
    assert wait["retry_count"] == 13
    snapshot = public_supervision_snapshot(loaded, now=now[0])
    assert snapshot is not None
    assert snapshot["credential_failure_class"] == "credential_unavailable"
    assert snapshot["retry_count"] == 13
    assert snapshot["next_action"] == "agent-run resume credential-run"
    for record in (wait, snapshot, loaded["diagnostics"][0]):
        if http_status is None:
            assert "credential_http_status" not in record
        else:
            assert record["credential_http_status"] == http_status

    presentation = waiting_presentation(loaded, {"supervision": snapshot})
    assert presentation is not None
    assert presentation.work == "等待工作凭据恢复可用"
    assert ("重试次数", "13") in presentation.details
    assert ("原因", "暂时无法取得 GitHub 工作凭据") in presentation.details
    assert any(value == "已超时" for _, value in presentation.details)
    text = str(presentation)
    for hidden in ("credential_unavailable", "凭据 HTTP 状态", "authorization", "截止="):
        assert hidden not in text
