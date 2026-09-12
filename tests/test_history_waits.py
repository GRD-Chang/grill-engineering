from __future__ import annotations

from copy import deepcopy
from typing import Any
from pathlib import Path

import pytest

from agent_run.delivery_history import history_records


def check_event(minute: int, result: str = "pending", **changes: Any) -> dict[str, Any]:
    event = {
        "at": f"2026-09-12T04:{minute:02}:00+00:00",
        "kind": "run_publication",
        "status": "waiting_checks" if result == "pending" else "run_approval_pending",
        "phase": "waiting_checks" if result == "pending" else "ready_for_approval",
        "semantic_attempt_id": "publication-1",
        "pr_number": 236,
        "required_checks_result": result,
        "required_checks_observed_at": f"2026-09-12T04:{minute:02}:00+00:00",
        "required_checks_head_sha": "accepted-head",
        "required_checks_run_identity": "checks-run-1",
        "required_checks_signature": f"checks-run-1:{result}",
        "checks_wait_identity": "window-1",
        "history_work_subject": "run-publication:run-example",
        "history_generation": 1,
        "history_budget_window": 1,
    }
    event.update(changes)
    return event


def history(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    state = {"run_id": "run-example", "parent": {"number": 226}}
    audit = {
        "semantic_agent_attempts": [{
            "attempt_id": "publication-1", "role": "publication",
            "work_subject": "run-publication:run-example", "ordinal": 1,
            "status": "completed", "outcome": "publication_artifact",
        }],
        "timeline": events,
    }
    return state, audit


def test_continuous_checks_form_one_independent_observed_wait() -> None:
    state, audit = history([check_event(0), check_event(4), check_event(13, "pass")])
    before = deepcopy((state, audit))

    records = history_records(state, audit)

    agent = next(record for record in records if not record["event_record"])
    assert agent["turning_points"] == []
    waits = [record for record in records if record["event_record"]]
    assert len(waits) == 2
    assert waits[1]["status_text"] == "等待人工批准"
    assert waits[0]["status_text"] == "自动检查通过"
    assert waits[0]["started_at"] == "2026-09-12T04:00:00+00:00"
    assert waits[0]["ended_at"] == "2026-09-12T04:13:00+00:00"
    assert waits[0]["span_seconds"] == 780
    assert (state, audit) == before


def test_state_store_retains_future_check_identity_without_repeating_details(tmp_path: Path) -> None:
    from agent_run.state import StateStore

    store = StateStore(tmp_path)
    state = {
        "run_id": "run-example", "status": "waiting_checks",
        "run_acceptance": {"phase": "accepted", "acceptance_generation": 1, "review_budget": {"window": 1}},
        "run_publication": {
            "phase": "waiting_checks", "pr_number": 236,
            "required_checks_observed_at": "2026-09-12T04:00:00+00:00",
            "required_checks_evidence": {
                "pr_number": 236, "head_sha": "accepted-head", "result": "pending",
                "checks": [{"name": "unit", "workflow": "CI", "link": "https://github.com/o/r/actions/runs/1/job/2", "bucket": "pending"}],
            },
        },
        "supervision_window": {"identity": "required-checks-236", "started_at": 1234.5},
    }
    store.save_run("run-example", state)
    state["run_publication"]["required_checks_observed_at"] = "2026-09-12T04:04:00+00:00"
    store.save_run("run-example", state)
    persisted = store.load_run("run-example")
    first, second = persisted["timeline"]
    assert first["required_checks_head_sha"] == "accepted-head"
    assert first["required_checks_run_identity"] == second["required_checks_run_identity"]
    assert first["checks_wait_identity"] == second["checks_wait_identity"]
    assert first["required_checks_evidence"]["checks"][0]["name"] == "unit"
    assert "required_checks_evidence" not in second
    state["run_publication"]["required_checks_evidence"]["checks"][0]["link"] = "https://github.com/o/r/actions/runs/1/job/3"
    store.save_run("run-example", state)
    timeline = store.load_run("run-example")["timeline"]
    assert timeline[-1]["required_checks_run_identity"] != first["required_checks_run_identity"]
    records = history_records(state, {"timeline": timeline})
    assert len(records) == 2


def test_history_separates_approval_merge_completion_from_completed_publication() -> None:
    events = [check_event(0), check_event(4, "pass")]
    events.extend([
        check_event(14, "pass", approval_granted_at="2026-09-12T04:14:00+00:00"),
        check_event(15, "pass", approval_granted_at="2026-09-12T04:14:00+00:00"),
        check_event(16, "pass", status="parent_closeout_pending", phase="merged", commit_sha="squashed"),
        check_event(17, "pass", status="completed", phase="completed", commit_sha="squashed"),
    ])
    records = history_records(*history(events))
    nodes = [record for record in records if record["event_record"]]
    assert [node["turning_points"][0]["kind"] for node in nodes] == [
        "required_checks", "approval", "integration", "completion",
    ]
    assert nodes[1]["status_text"] == "已批准"
    assert nodes[1]["started_at"] == "2026-09-12T04:04:00+00:00"
    assert nodes[1]["ended_at"] == "2026-09-12T04:14:00+00:00"


@pytest.mark.parametrize("boundary", [
    "failure", "unknown", "unavailable", "timeout", "stop", "resume",
    "different_pr", "different_head", "different_window", "rerun", "missing_identity",
    "generation", "budget",
])
def test_check_waits_preserve_real_boundaries(boundary: str) -> None:
    first, second = check_event(0), check_event(8)
    middle: list[dict[str, Any]] = []
    if boundary in {"failure", "unknown"}:
        middle = [check_event(4, "fail" if boundary == "failure" else "unknown", status="waiting_checks", phase="waiting_checks")]
    elif boundary == "unavailable":
        middle = [check_event(4, required_checks_observation_status="unavailable")]
    elif boundary in {"timeout", "stop"}:
        middle = [check_event(4, status="supervision_timeout" if boundary == "timeout" else "operator_stopped")]
    elif boundary == "resume":
        middle = []
    else:
        key, value = {
            "different_pr": ("pr_number", 237),
            "different_head": ("required_checks_head_sha", "new-head"),
            "different_window": ("checks_wait_identity", "window-2"),
            "rerun": ("required_checks_run_identity", "new-check-execution"),
            "missing_identity": ("required_checks_run_identity", None),
            "generation": ("history_generation", 2),
            "budget": ("history_budget_window", 2),
        }[boundary]
        second[key] = value
    state, audit = history([first, *middle, second])
    if boundary == "resume":
        audit["agent_resumes"] = [{
            "resume_id": "resume-1", "requested_at": "2026-09-12T04:04:00+00:00",
            "source_status": "supervision_timeout", "generation": 1,
            "work_subject": "run-publication:run-example",
            "semantic_attempt_id": "publication-1",
        }]
    records = history_records(state, audit)
    waits = [record for record in records if record["event_record"] and record["turning_points"][0]["kind"] == "required_checks"]
    assert len(waits) >= 2
    assert all(wait["span_seconds"] == 0 for wait in waits)


@pytest.mark.parametrize("reason,action,expected_waits", [
    ("github_write_pending", "refresh_final_pr_narrative", 1),
    ("github_write_pending", "ensure_final_run_ref", 1),
    ("github_read_failed", "refresh_final_pr_narrative", 2),
    ("github_write_pending", "create_final_pr", 2),
])
def test_repeated_checks_hide_only_proven_routine_write_intents(
    reason: str, action: str, expected_waits: int,
) -> None:
    bridge = check_event(3, status="waiting_external", phase="waiting_external",
                         external_wait_reason={"code": reason, "message": "GitHub operation"},
                         github_write_action=action)
    records = history_records(*history([check_event(0), bridge, check_event(4)]))
    nodes = [record for record in records if record["event_record"]]
    assert len([node for node in nodes if node["turning_points"][0]["kind"] == "required_checks"]) == expected_waits
    assert any(node["turning_points"][0]["kind"] == "supervision" for node in nodes) == (expected_waits == 2)


def test_public_history_outputs_share_compact_facts_and_preserve_raw_audit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from contextlib import redirect_stdout
    from io import StringIO

    from rich.text import Text
    from agent_run.cli import main
    from conftest import seed_run, write_fixture

    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    fixture = write_fixture(git_repo / "github.json", issues={})
    started = seed_run(git_repo, fixture, idle_control=True)
    assert started.returncode == 0, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    state_path = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text())
    events = [check_event(0), check_event(4), check_event(13, "pass")]
    events[-1]["required_checks_evidence"] = {"checks": [{
        "name": "quality", "bucket": "pass", "link": "https://github.com/o/r/actions/runs/1/job/2",
    }]}
    state["timeline"] = events
    state_path.write_text(json.dumps(state))
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "120")
    before = {path: path.read_bytes() for path in (git_repo / ".agent-run").rglob("*") if path.is_file()}
    for arguments in (["--plain"], ["--plain", "--details"], [], ["--details"]):
        output = Terminal()
        with redirect_stdout(output):
            assert main(["history", run_id, *arguments]) == 0
        text = Text.from_ansi(output.getvalue()).plain
        assert text.count("事件：自动检查") == 1
        assert "自动检查通过" in text
        assert "Runner 观测到的等待：13 分钟" in text
        assert "事件：人工批准" in text
        assert "04:04" not in text
        if "--details" in arguments:
            assert "quality" in text
            assert "https://github.com/o/r/actions/runs/1/job/2" in text
    output = StringIO()
    with redirect_stdout(output):
        assert main(["history", run_id, "--json"]) == 0
    audit = json.loads(output.getvalue())
    assert audit["timeline"] == events
    assert [event["kind"] for event in audit["events"]] == ["required_checks"] * 3
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("changed", ["history_work_subject", "history_generation", "history_budget_window", "missing_scope", "revoked_approval"])
def test_approval_waits_keep_generation_budget_and_revocation_boundaries(changed: str) -> None:
    first = check_event(0, "pass")
    second = check_event(4, "pass")
    if changed == "missing_scope":
        first.pop("history_generation", None)
        second.pop("history_generation", None)
    elif changed == "revoked_approval":
        first["approval_granted_at"] = "2026-09-12T04:00:00+00:00"
    else:
        second[changed] = "other-subject" if changed == "history_work_subject" else 2
    records = history_records(*history([first, second]))
    approvals = [record for record in records if record["event_record"] and record["turning_points"][0]["kind"] == "approval"]
    assert len(approvals) == 2
    assert approvals[-1]["status_text"] == "等待人工批准"


@pytest.mark.parametrize("owner,expected_subject", [
    ("active_ticket_job", "ticket:3"),
    ("parent_job", "parent-only:run-example"),
    ("repair_job", "run-repair:run-example"),
    ("run_publication", "run-publication:run-example"),
])
def test_persisted_waits_bind_the_actual_owner_generation_and_budget(
    tmp_path: Path, owner: str, expected_subject: str,
) -> None:
    from agent_run.state import StateStore

    job = {
        "phase": "waiting_external", "pr_number": 236,
        "ticket_branch_generation": 2, "parent_generation": 2, "repair_generation": 2,
        "review_budget": {"window": 3},
        "required_checks_evidence": {
            "pr_number": 236, "head_sha": "head-1", "result": "pending", "checks": [],
        },
    }
    state = {
        "run_id": "run-example", "status": "waiting_external",
        "run_acceptance": {"phase": "accepted", "acceptance_generation": 2, "review_budget": {"window": 3}},
    }
    if owner == "active_ticket_job":
        job["ticket_number"] = 3
    if owner == "repair_job":
        state["run_acceptance"]["repair_job"] = job
    else:
        state[owner] = job
    store = StateStore(tmp_path)
    store.save_run("run-example", state)
    event = store.load_run("run-example")["timeline"][-1]
    assert event["history_work_subject"] == expected_subject
    assert event["history_generation"] == 2
    assert event["history_budget_window"] == 3
    assert event["required_checks_head_sha"] == "head-1"


def test_active_repair_checks_do_not_inherit_a_stale_publication_owner(tmp_path: Path) -> None:
    from agent_run.state import StateStore

    stale_publication = {
        "phase": "stale", "pr_number": 236,
        "required_checks_evidence": {"pr_number": 236, "head_sha": "stale-head", "result": "pass", "checks": []},
    }
    repair = {
        "phase": "waiting_checks", "repair_generation": 2, "pr_number": 237,
        "review_budget": {"window": 3},
        "required_checks_evidence": {"pr_number": 237, "head_sha": "repair-head", "result": "pending", "checks": []},
    }
    state = {
        "run_id": "run-example", "status": "waiting_checks",
        "run_publication": stale_publication,
        "run_acceptance": {
            "phase": "repairing", "acceptance_generation": 1,
            "review_budget": {"window": 3}, "repair_job": repair,
        },
    }
    store = StateStore(tmp_path)
    store.save_run("run-example", state)
    event = store.load_run("run-example")["timeline"][-1]
    assert event["pr_number"] == 237
    assert event["required_checks_head_sha"] == "repair-head"
    assert event["history_work_subject"] == "run-repair:run-example"
    assert event["history_generation"] == 2
