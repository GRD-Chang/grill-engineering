from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureScopeImpactAssessor
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.graph import state_from_graph
from agent_run.revisions import ticket_graph_revision
from agent_run.state import StateStore
from agent_run.worker_sandbox import WorkerSandboxError
from conftest import write_fixture


def _ticket(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Implement it.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def _assessment(structural: bool) -> dict[str, Any]:
    return {
        "structural_change": structural,
        "summary": "Parent clarification only."
        if not structural
        else "Delivery boundary changed.",
        "ticket_set_impact": "none",
        "dependency_impact": "none",
        "delivery_boundary_impact": "none"
        if not structural
        else "changed",
        "completed_work_impact": "none",
    }


class RecordingScopeAssessor:
    def __init__(self, *results: dict[str, Any]) -> None:
        self.results = list(results)
        self.requests: list[dict[str, Any]] = []

    def assess_scope(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return self.results.pop(0)


def test_ticket_graph_revision_ignores_parent_execution_order(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    reader = FixtureGitHubReader(fixture)
    original = reader.delivery_graph(1)
    original_state = state_from_graph({}, original)

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [3, 2]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    reordered = reader.delivery_graph(1)
    reordered_state = state_from_graph(original_state, reordered)

    assert ticket_graph_revision(reordered) == ticket_graph_revision(original)
    assert (
        reordered_state["ticket_graph"]["revision"]
        == original_state["ticket_graph"]["revision"]
    )
    assert reordered_state["ticket_graph"]["ordered_ticket_numbers"] == [3, 2]


def test_parent_clarification_is_assessed_once_and_absorbed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    assessor = RecordingScopeAssessor(_assessment(False))
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
        scope_assessor=assessor,
    )
    started, _ = controller.start(1)

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] += "\nThis sentence only clarifies wording."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed, _ = controller.resume(str(started["run_id"]))

    assert resumed["status"] == "active"
    assert "pending_structure_change" not in resumed
    assert resumed["accepted_parent_spec_revision"] == resumed["parent"]["revision"]
    assert len(assessor.requests) == 1
    request = assessor.requests[0]
    assert request["accepted_parent"]["body"] == "Deliver the ticket set."
    assert "clarifies wording" in request["proposed_parent"]["body"]
    assert request["ticket_graph"]["ordered_ticket_numbers"] == [2]
    assert request["completed_work"] == []
    assert request["checkout"] == str(git_repo)


def test_structural_parent_change_waits_for_exact_confirmation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    assessor = RecordingScopeAssessor(
        _assessment(True),
        _assessment(True),
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
        scope_assessor=assessor,
    )
    started, _ = controller.start(1)
    run_id = str(started["run_id"])

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Change the overall delivery boundary."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    paused, _ = controller.resume(run_id)
    repeated, _ = controller.resume(run_id)

    assert paused["status"] == "structure_change_pending"
    assert repeated["pending_structure_change"]["kind"] == "parent_spec"
    assert repeated["pending_structure_change"]["scope_impact_assessment"][
        "structural_change"
    ]
    assert len(assessor.requests) == 1
    first_proposed = repeated["pending_structure_change"]["proposed_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "A later and different delivery boundary."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    later, _ = controller.confirm_structure(run_id)

    assert later["status"] == "structure_change_pending"
    assert later["pending_structure_change"]["kind"] == "parent_spec"
    assert later["pending_structure_change"]["proposed_revision"] != first_proposed
    assert len(assessor.requests) == 2
    assert assessor.requests[1]["accepted_parent"]["body"] == (
        "Change the overall delivery boundary."
    )
    assert assessor.requests[1]["proposed_parent"]["body"] == (
        "A later and different delivery boundary."
    )

    confirmed, _ = controller.confirm_structure(run_id)

    assert confirmed["status"] == "active"
    assert "pending_structure_change" not in confirmed
    assert (
        confirmed["accepted_parent_spec_revision"]
        == confirmed["parent"]["revision"]
    )


def test_codex_scope_assessment_is_one_shot_and_uses_yolo_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    captured: dict[str, Any] = {}

    def fake_bubblewrap(
        command: list[str],
        **options: Any,
    ) -> list[str]:
        captured["writable_checkout"] = options["writable_checkout"]
        captured["command"] = command
        return command

    def fake_run(
        arguments: list[str],
        **options: Any,
    ) -> subprocess.CompletedProcess[str]:
        captured["prompt"] = options["prompt"]
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(_assessment(False)),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"thread.started","thread_id":"scope-1"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_run.codex.bubblewrap_command", fake_bubblewrap)
    monkeypatch.setattr("agent_run.codex.run_worker_process", fake_run)
    result = CodexCliBackend(
        credential_provider=lambda: "reader-secret"
    ).assess_scope(
        {
            "checkout": str(checkout),
            "accepted_parent": {"body": "before"},
            "proposed_parent": {"body": "after"},
            "ticket_graph": {"ordered_ticket_numbers": [2]},
            "completed_work": [],
        }
    )

    assert result == _assessment(False)
    assert captured["writable_checkout"] is True
    assert "exec" in captured["command"]
    assert "resume" not in captured["command"]
    assert "--output-schema" in captured["command"]
    assert "新旧 Parent Spec" in captured["prompt"]
    assert "既有已完成工作" in captured["prompt"]


def test_fixture_scope_assessor_returns_configured_deterministic_result(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "github.json"
    configured = _assessment(True)
    fixture.write_text(
        json.dumps({"scope_impact_assessment": configured}),
        encoding="utf-8",
    )

    assert FixtureScopeImpactAssessor(fixture).assess_scope({}) == configured


def test_codex_backend_converts_worker_sandbox_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def fail_worker(*args: Any, **kwargs: Any) -> None:
        raise WorkerSandboxError("Codex worker timed out")

    monkeypatch.setattr("agent_run.codex.run_worker_process", fail_worker)
    monkeypatch.setattr(
        "agent_run.codex.bubblewrap_command",
        lambda command, **options: command,
    )

    with pytest.raises(CodexProcessError, match="worker timed out"):
        CodexCliBackend(
            credential_provider=lambda: "reader-secret"
        ).assess_scope(
            {
                "checkout": str(checkout),
                "accepted_parent": {"body": "before"},
                "proposed_parent": {"body": "after"},
                "ticket_graph": {"ordered_ticket_numbers": [2]},
                "completed_work": [],
            }
        )
