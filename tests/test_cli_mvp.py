from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_run.semantic_attempt import canonical_fingerprint
from cli_fixtures import run_agents as _run_agents
from conftest import seed_run, write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import (
    git_fetch_failure_wrapper,
    load_only_run_state,
    run_cli,
    stdout_json,
)
from test_cli_delivery import passing_acceptance, publication, repair_acceptance, ticket


def _assert_credential_wait_is_not_public(
    repo: Path, fixture: Path, run_id: str
) -> None:
    for command in ("status", "history"):
        output = stdout_json(invoke_cli_inprocess(repo, fixture, command, run_id, "--json"))
        assert output.get("supervision") is None


def test_run_reaches_explicit_approval_with_status_and_history(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")

    advanced = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert advanced.returncode == 0, advanced.stderr
    output = stdout_json(advanced)
    assert output["result"] == "started"
    assert output["status"] == "run_approval_pending"
    run_id = output["run_id"]
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"
    assert state["parent"]["number"] == 1

    status = run_cli(git_repo, fixture, "status", run_id)
    assert status.returncode == 0, status.stderr
    assert "仓库:       example/project" in status.stdout
    assert "整体需求:   #1 Parent spec" in status.stdout
    assert "状态:       等待人工批准" in status.stdout
    assert "当前对象:   整体交付" in status.stdout
    assert "最近 Agent: 发布 Agent" in status.stdout
    latest_invocation = state["agent_invocation_history"][-1]
    assert str(latest_invocation["model"]) in status.stdout
    assert str(latest_invocation["reasoning_effort"]) in status.stdout
    assert "下一步: agent-run approve" in status.stdout
    assert run_id not in status.stdout
    status_json = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
    assert status_json["active_ticket"] is None
    assert status_json["phase"] == "ready_for_approval"

    history = run_cli(git_repo, fixture, "history", run_id, "--json")
    assert history.returncode == 0, history.stderr
    history_json = stdout_json(history)
    timeline = history_json["timeline"]
    events = history_json["events"]
    assert events == sorted(events, key=lambda event: event["at"])
    assert events[-1]["at"] >= timeline[-1]["at"]
    assert history_json["summary"]["rounds"] == {
        "run_publication": 1,
        "run_review": 1,
        "ticket_development": 1,
        "ticket_publication": 1,
        "ticket_review": 1,
    }
    assert any(
        entry.get("ticket") == 3
        and entry.get("worker") == "开发工作代理"
        and entry.get("attempt") == 1
        and entry.get("thread_id") == "ticket-developer-3"
        for entry in timeline
    )
    assert any(
        entry.get("ticket") == 3
        and entry.get("worker") == "独立验收工作代理"
        and entry.get("attempt") == 1
        and entry.get("thread_id") == "ticket-reviewer-3"
        for entry in timeline
    )
    assert any(entry.get("pr_number") == 2 for entry in timeline)
    assert all("summary" not in entry for entry in timeline)
    history_count = len(timeline)

    replayed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert replayed.returncode == 0, replayed.stderr
    assert stdout_json(replayed)["result"] == "resumed"
    assert stdout_json(replayed)["run_id"] == run_id
    replay_timeline = stdout_json(
        run_cli(git_repo, fixture, "history", run_id, "--json")
    )["timeline"]
    assert len(replay_timeline) == history_count
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(data["delivery"]["pull_requests"]) == 2


def test_displayed_final_approval_selects_the_unique_active_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")

    first = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    first_run_id = str(stdout_json(first)["run_id"])
    first_approved = run_cli(git_repo, fixture, "approve", first_run_id)
    assert first_approved.returncode == 0, first_approved.stderr
    assert stdout_json(first_approved)["status"] == "completed"

    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    second_start = seed_run(git_repo, fixture, reuse_existing=False)
    second_run_id = str(stdout_json(second_start)["run_id"])
    assert second_run_id != first_run_id
    second_agents = _run_agents(git_repo / "second-agents.json")
    second_agent_payload = json.loads(second_agents.read_text(encoding="utf-8"))
    second_agent_payload["developments"][0]["write_files"] = {
        "feature.txt": "done twice\n"
    }
    second_agents.write_text(json.dumps(second_agent_payload), encoding="utf-8")
    second = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(second_agents)
    )
    assert stdout_json(second)["run_id"] == second_run_id
    assert stdout_json(second)["status"] == "run_approval_pending"

    status = run_cli(git_repo, fixture, "status", second_run_id)
    action_line = next(
        line for line in status.stdout.splitlines() if line.startswith("下一步: ")
    )
    displayed_command = action_line.removeprefix("下一步: ")
    command = shlex.split(displayed_command)
    assert command[0] == "agent-run"

    approved = run_cli(git_repo, fixture, *command[1:])

    assert approved.returncode == 0, (approved.stdout, approved.stderr)
    assert stdout_json(approved)["run_id"] == second_run_id
    assert stdout_json(approved)["status"] == "completed"
    runs_dir = git_repo / ".agent-run" / "runs"
    first_state = json.loads(
        (runs_dir / f"{first_run_id}.json").read_text(encoding="utf-8")
    )
    second_state = json.loads(
        (runs_dir / f"{second_run_id}.json").read_text(encoding="utf-8")
    )
    assert first_state["status"] == "completed"
    assert second_state["status"] == "completed"


def test_parent_approval_selector_refuses_two_approval_ready_runs(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    pending = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(pending)["run_id"])
    original_path = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    duplicate_id = f"{run_id}-duplicate"
    duplicate_path = original_path.with_name(f"{duplicate_id}.json")
    duplicate = json.loads(
        original_path.read_text(encoding="utf-8").replace(run_id, duplicate_id)
    )
    attempt_ids: dict[str, str] = {}

    def rebind_attempts(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                rebind_attempts(item)
            return
        if not isinstance(value, dict):
            return
        attempt_id = value.get("attempt_id")
        if isinstance(attempt_id, str) and all(
            key in value
            for key in (
                "role",
                "work_subject",
                "generation",
                "currentness_boundary_fingerprint",
                "ordinal",
                "budget_window",
            )
        ):
            identity = {
                key: value.get(key)
                for key in (
                    "role",
                    "work_subject",
                    "generation",
                    "currentness_boundary_fingerprint",
                    "ordinal",
                    "budget_window",
                )
            }
            rebound = canonical_fingerprint(identity)
            attempt_ids[attempt_id] = rebound
            value["attempt_id"] = rebound
        for item in value.values():
            rebind_attempts(item)

    def replace_attempt_references(value: object) -> object:
        if isinstance(value, str):
            return attempt_ids.get(value, value)
        if isinstance(value, list):
            return [replace_attempt_references(item) for item in value]
        if isinstance(value, dict):
            return {
                key: replace_attempt_references(item) for key, item in value.items()
            }
        return value

    rebind_attempts(duplicate)
    duplicate = replace_attempt_references(duplicate)
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    before = {
        original_path: original_path.read_bytes(),
        duplicate_path: duplicate_path.read_bytes(),
    }

    ambiguous = run_cli(git_repo, fixture, "approve", "1", "--repo", "example/project")

    assert ambiguous.returncode == 2
    assert stdout_json(ambiguous)["diagnostics"][0]["code"] == (
        "run_selector_ambiguous"
    )
    assert {path: path.read_bytes() for path in before} == before


def test_current_long_blocker_is_full_in_status_and_bounded_in_history(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    long_blocker = (
        "发生了什么：受控测试数据的处理边界尚未确认；"
        "尝试了什么：已核对当前策略与仓库文档但没有明确结论；"
        "人必须做什么：维护者必须确认该数据是否可以继续处理。"
        + "补充上下文：该确认会决定当前开发路径。" * 30
    )
    agents = git_repo / "blocked-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer",
                        "human_blockers": [long_blocker],
                    }
                ],
                "reviews": [],
                "publications": [],
            }
        ),
        encoding="utf-8",
    )

    blocked = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(blocked)["run_id"])
    status = invoke_cli_inprocess(git_repo, fixture, "status", run_id)
    history = invoke_cli_inprocess(git_repo, fixture, "history", run_id)
    history_json = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )

    assert status.returncode == history.returncode == 0
    assert long_blocker in status.stdout
    assert long_blocker not in history.stdout
    assert "…（已截断；完整内容见 --json）" in history.stdout
    assert history_json["operator_action"]["reasons"] == [long_blocker]
    assert any(
        event["kind"] == "human_blocker" and event["details"] == [long_blocker]
        for event in history_json["events"]
    )


def test_history_continues_after_timeline_capacity_without_new_invocations(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    pending = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(pending)["run_id"])
    state = load_only_run_state(git_repo)
    invocation_count = len(state["agent_invocation_history"])
    capacity_at = state["timeline"][-1]["at"]
    state["created_at"] = (
        datetime.fromisoformat(capacity_at) - timedelta(seconds=10)
    ).isoformat()
    state["timeline"] = [
        {
            "at": capacity_at,
            "kind": "run_status",
            "status": "active",
            "result": "active",
        }
        for _ in range(255)
    ]
    state.pop("timeline_at_capacity", None)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")

    approved = run_cli(git_repo, fixture, "approve", run_id)
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    completed = load_only_run_state(git_repo)
    assert len(completed["agent_invocation_history"]) == invocation_count

    history = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )
    text = invoke_cli_inprocess(git_repo, fixture, "history", run_id).stdout

    assert history["timeline"][-1]["kind"] == "timeline_capacity"
    assert completed["timeline_at_capacity"] is True
    assert history["timeline_continuation"]
    assert history["events"] == sorted(history["events"], key=lambda event: event["at"])
    later_kinds = {event["kind"] for event in history["timeline_continuation"]}
    assert {"publication", "required_checks", "integration", "completion"} <= later_kinds
    continuation_times = {
        event["at"] for event in history["timeline_continuation"]
    }
    unified_later_kinds = {
        event["kind"]
        for event in history["events"]
        if event["at"] in continuation_times
    }
    assert {"publication", "required_checks", "integration", "completion"} <= (
        unified_later_kinds
    )
    completion = history["events"][-1]
    assert completion["kind"] == "completion"
    assert completion["status"] == "completed"
    expected_elapsed = int(
        (
            datetime.fromisoformat(completion["at"])
            - datetime.fromisoformat(completed["created_at"])
        ).total_seconds()
    )
    assert history["summary"]["elapsed_seconds"] == expected_elapsed
    assert "已完成" in text


def test_history_renders_device_timezone_and_keeps_json_events_in_utc(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["timeline"] = [
        {
            "at": "2026-08-30T00:00:00+00:00",
            "kind": "run_status",
            "status": "active",
            "result": "started",
        }
    ]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")

    local = run_cli(
        git_repo,
        fixture,
        "history",
        run_id,
        extra_env={"TZ": "Asia/Shanghai"},
    )
    fallback = run_cli(
        git_repo,
        fixture,
        "history",
        run_id,
        extra_env={"TZ": "Invalid/Zone"},
    )
    history = run_cli(git_repo, fixture, "history", run_id, "--json")
    assert history.returncode == 0, (history.stdout, history.stderr)
    audit = stdout_json(history)

    assert local.returncode == fallback.returncode == 0
    assert "时区:       Asia/Shanghai (UTC+08:00)" in local.stdout
    assert "08:00" in local.stdout
    assert "时区:       UTC (UTC+00:00)" in fallback.stdout
    assert audit["time_zone"] == "UTC"
    assert audit["events"][0]["at"] == "2026-08-30T00:00:00+00:00"


def test_history_keeps_later_invocations_after_an_early_timeline_tail(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer",
            "summary": "Repaired the accumulated Run.",
            "write_files": {"run-repair.txt": "repaired\n"},
        }
    )
    agent_data["publications"].append(publication())
    agent_data["run_reviews"] = [
        repair_acceptance("run-reviewer-1"),
        passing_acceptance("run-repair-reviewer", "Run repair passed."),
    ]
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    completed = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert completed.returncode == 0, completed.stderr
    run_id = stdout_json(completed)["run_id"]
    state = load_only_run_state(git_repo)
    early_event = state["timeline"][0]
    state["timeline"] = [early_event]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")

    history = invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    assert history.returncode == 0, (history.stdout, history.stderr)
    audit = stdout_json(history)
    text = invoke_cli_inprocess(git_repo, fixture, "history", run_id).stdout

    assert audit["timeline"] == [early_event]
    assert audit["events"][-1]["at"] > early_event["at"]
    later_facts = [
        (event["kind"], event["object"], event["details"])
        for event in audit["events"]
        if event["at"] > early_event["at"]
    ]
    assert any(
        kind == "development" and obj == "Run Acceptance"
        for kind, obj, _ in later_facts
    )
    assert any(
        kind == "review" and obj == "Run Acceptance"
        for kind, obj, _ in later_facts
    )
    assert any(kind == "publication" for kind, _, _ in later_facts)
    assert "整体交付 · 发布 Agent" in text


def test_status_shows_the_current_review_findings(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "finding-agents.json"
    review = repair_acceptance("ticket-reviewer-3")
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer-3",
                        "summary": "Created the first Candidate.",
                        "write_files": {"feature.txt": "first\n"},
                    }
                ],
                "reviews": [review],
                "publications": [],
            }
        ),
        encoding="utf-8",
    )

    failed = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert failed.returncode == 2
    run_id = stdout_json(failed)["run_id"]
    status = invoke_cli_inprocess(git_repo, fixture, "status", run_id)
    finding = review["checks"]["e2e"]["findings"][0]

    assert "当前问题（1）" in status.stdout
    for part in finding.split("；"):
        assert status.stdout.count(part) == 1


def test_run_drives_ticket_lifecycle_through_the_internal_driver(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_run.cli import main

    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")

    monkeypatch.chdir(git_repo)

    exit_code = main(
        [
            "run",
            "1",
            "--github-fixture",
            str(fixture),
            "--agent-fixture",
            str(agents),
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 0, output.err
    assert json.loads(output.out.splitlines()[-1])["status"] == "run_approval_pending"


def test_run_supervises_pending_ticket_checks_in_one_call(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pass", "none"]},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_run_recovers_a_persisted_check_deadline_without_duplicate_delivery(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending"]},
        supervision_clock_multiplier=540,
    )
    agents = _run_agents(git_repo / "agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    wait = paused_state["supervision_wait"]
    assert wait["kind"] == "required_checks"
    assert wait["deadline"] - wait["started_at"] == 45 * 60
    assert wait["identity"] == paused_state["supervision_window"]["identity"]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert fixture_data["supervision_clock"] == 45 * 60
    assert len(fixture_data["delivery"]["pull_requests"]) == 1

    fixture_data["delivery"]["required_checks"] = ["pass", "none"]
    fixture_data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 2
    assert delivery["closed_issues"] == [3]


def test_supervision_timeout_resume_opens_a_new_window_without_duplicate_delivery(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending"]},
        supervision_clock_multiplier=540,
    )
    agents = _run_agents(git_repo / "agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    run_id = str(stdout_json(paused)["run_id"])
    paused_state = load_only_run_state(git_repo)
    # Persisted Runs created before the response-audit protocol remain
    # observable and resumable instead of being rejected as incompatible.
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    paused_state.pop("human_response_audit_protocol")
    paused_state.pop("human_response_audit")
    run_file.write_text(json.dumps(paused_state), encoding="utf-8")
    expected_action = f"agent-run resume {run_id}"
    for command in ("status", "history"):
        output = stdout_json(invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json"))
        wait = output["supervision"]
        assert wait["kind"] == "required_checks"
        assert wait["remaining_seconds"] == 0
        assert wait["timeout_resume_action"] == expected_action

    for command in ("status", "history"):
        text = invoke_cli_inprocess(git_repo, fixture, command, run_id).stdout
        assert "已超时" in text
        assert "agent-run resume 1 --repo example/project" in text
        assert "截止=" not in text
        assert run_id not in text
        if command == "status":
            assert "最近 Agent:" not in text
            assert "等待 PR #1 的合并前检查" in text
        else:
            latest_invocation = paused_state["agent_invocation_history"][-1]
            assert str(latest_invocation["model"]) in text
            assert str(latest_invocation["reasoning_effort"]) in text

    before_rejected_resume = load_only_run_state(git_repo)
    for forbidden_arguments in (("--new-thread",), ("--message", "human response")):
        rejected = run_cli(
            git_repo,
            fixture,
            "resume",
            run_id,
            *forbidden_arguments,
            "--agent-fixture",
            str(agents),
        )

        assert rejected.returncode == 2
        assert load_only_run_state(git_repo) == before_rejected_resume

    resumed = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "supervision_timeout"
    state = load_only_run_state(git_repo)
    assert state["supervision_wait"]["started_at"] > wait["started_at"]
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["closed_issues"] == []

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["required_checks"] = ["pass", "none"]
    fixture_data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 2
    assert delivery["closed_issues"] == [3]


def test_run_routes_a_pending_ticket_check_failure_through_repair(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "fail", "pass", "none"]},
    )
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": "ticket-developer-3",
            "thread_id": "ticket-developer-3",
            "summary": "Repaired the failed required check.",
            "write_files": {"feature.txt": "repaired\n"},
        }
    )
    data["publications"].append(publication())
    data["reviews"].append(
        passing_acceptance("ticket-reviewer-3-repair", "The repaired Ticket flow passed.")
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    assert state["ticket_jobs"]["3"]["modification_attempts"] == 2
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(data["delivery"]["pull_requests"]) == 2
    assert data["delivery"]["closed_issues"] == [3]


@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase"),
    [
        ("developments", "development", "developing"),
        ("reviews", "fresh_acceptance", "reviewing"),
        ("publications", "publication", "publication"),
    ],
)
def test_run_retries_initial_credential_before_starting_each_ticket_worker(
    git_repo: Path, fixture_role: str, invocation_role: str, phase: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"]
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    assert "credential_availability" not in state
    assert "supervision_window" not in state
    _assert_credential_wait_is_not_public(git_repo, fixture, str(state["run_id"]))
    history = state.get("agent_invocation_history", [])
    attempts = [
        invocation
        for invocation in history
        if invocation.get("role") == invocation_role and invocation.get("phase") == phase
    ]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "completed"


@pytest.mark.parametrize(
    ("failure", "http_status"),
    [
        ("temporary issuer outage", None),
        ({"message": "temporary issuer outage", "http_status": 503}, 503),
    ],
)
def test_run_pauses_after_the_initial_worker_credential_window_expires(
    git_repo: Path, failure: object, http_status: int | None
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["initial_credential_failures"] = [failure] * 130
    agents.write_text(json.dumps(data), encoding="utf-8")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    assert stdout_json(paused)["status"] == "supervision_timeout"
    state = load_only_run_state(git_repo)
    availability = {
        "change_job": "ticket-3",
        "failure_class": "credential_unavailable",
        "phase": "developing",
        "resume_status": "active",
        "retry_count": 13,
    }
    if http_status is not None:
        availability["http_status"] = http_status
    assert state["credential_availability"] == availability
    assert state["supervision_wait"]["kind"] == "github_convergence"
    assert "Worker credential availability" in state["supervision_wait"]["waiting_for"]
    assert state["supervision_wait"]["credential_failure_class"] == (
        "credential_unavailable"
    )
    assert state["supervision_wait"].get("credential_http_status") == http_status
    # The final sleep expires the window; it does not authorize another retry.
    assert state["supervision_wait"]["retry_count"] == 13
    assert state["active_agent_invocation"] is None
    assert state["agent_invocation_history"] == []
    run_id = str(state["run_id"])
    for command in ("status", "history"):
        output = stdout_json(invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json"))
        assert output["next_action"] == (
            "agent-run resume 1 --repo example/project"
        )
        snapshot = output["supervision"]
        assert snapshot["credential_failure_class"] == "credential_unavailable"
        assert snapshot.get("credential_http_status") == http_status
        assert snapshot["next_action"] == f"agent-run resume {run_id}"
        text = invoke_cli_inprocess(git_repo, fixture, command, run_id)
        assert text.returncode == 0, text.stderr
        assert "等待工作凭据恢复可用" in text.stdout
        assert "暂时无法取得 GitHub 工作凭据" in text.stdout
        assert "credential_unavailable" not in text.stdout
        assert "重试次数：13" in text.stdout
        assert "已超时" in text.stdout
        assert "截止=" not in text.stdout
        assert "authorization" not in text.stdout.lower()
        assert "凭据 HTTP 状态：" not in text.stdout


@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase"),
    [
        ("run_reviews", "reviewer", "run_acceptance"),
        ("run_publications", "final_publication", "run_publication"),
    ],
)
def test_run_retries_initial_credential_for_final_workers_once(
    git_repo: Path,
    fixture_role: str,
    invocation_role: str,
    phase: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"]
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert "credential_availability" not in state
    attempts = [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == invocation_role and invocation.get("phase") == phase
    ]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "completed"


@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase", "work_subject", "resume_status"),
    [
        (
            "run_reviews",
            "reviewer",
            "run_acceptance",
            "run-acceptance",
            "run_acceptance_pending",
        ),
        (
            "run_publications",
            "final_publication",
            "run_publication",
            "run-publication",
            "run_publication_pending",
        ),
    ],
)
def test_final_worker_credential_timeout_is_recoverable_without_duplicates(
    git_repo: Path,
    fixture_role: str,
    invocation_role: str,
    phase: str,
    work_subject: str,
    resume_status: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"] * 130
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    assert stdout_json(paused)["status"] == "supervision_timeout"
    state = load_only_run_state(git_repo)
    availability = state["credential_availability"]
    assert {
        key: value for key, value in availability.items() if key != "retry_count"
    } == {
        "change_job": work_subject,
        "failure_class": "credential_unavailable",
        "phase": phase,
        "resume_status": resume_status,
    }
    assert availability["retry_count"] >= 1
    assert state["supervision_wait"]["kind"] == "github_convergence"
    assert state["supervision_wait"]["credential_failure_class"] == (
        "credential_unavailable"
    )
    assert state["active_agent_invocation"] is None
    assert not [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == invocation_role and invocation.get("phase") == phase
    ]

    data["initial_credential_failures_by_role"] = {}
    agents.write_text(json.dumps(data), encoding="utf-8")
    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    resumed = load_only_run_state(git_repo)
    attempts = [
        invocation
        for invocation in resumed["agent_invocation_history"]
        if invocation.get("role") == invocation_role and invocation.get("phase") == phase
    ]
    assert len(attempts) == 1
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 2
    assert delivery["closed_issues"] == [3]


def test_run_supervises_initial_repository_read_lag_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        repository_read_failures=[
            {"code": "github_timeout", "message": "repository still converging"},
            {"code": "github_timeout", "message": "repository still converging"},
            {"code": "github_timeout", "message": "repository still converging"},
        ],
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state.get("repository_binding_pending") is None
    assert state["base"]["branch"] == "main"


@pytest.mark.parametrize("legacy_protocol", [None, 1, 2.0, "2"])
def test_legacy_run_is_rejected_before_initial_repository_wait_mutates_it(
    git_repo: Path,
    tmp_path: Path,
    legacy_protocol: object,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _run_agents(git_repo / "agents.json")
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    started = seed_run(git_repo, fixture, "1", extra_env=locator_env)
    assert started.returncode == 0, started.stderr
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    if legacy_protocol is None:
        legacy.pop("branch_authority_protocol")
    else:
        legacy["branch_authority_protocol"] = legacy_protocol
    legacy["locator_registration_pending"] = True
    state_path.write_text(json.dumps(legacy), encoding="utf-8")
    state_before = state_path.read_text(encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery_before = fixture_data.get("delivery")
    fixture_data["repository_read_failures"] = [
        {"code": "github_timeout", "message": "repository still converging"}
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator_path.unlink()
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    branches_before = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    git_config_before = (git_repo / ".git" / "config").read_bytes()
    agent_before = agents.read_text(encoding="utf-8")

    arguments = ("run", "1", "--agent-fixture", str(agents))
    rejected = run_cli(git_repo, fixture, *arguments, extra_env=locator_env)

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert state_path.read_text(encoding="utf-8") == state_before
    assert not locator_path.exists()
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout == head_before
    assert subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout == branches_before
    assert (git_repo / ".git" / "config").read_bytes() == git_config_before
    assert json.loads(fixture.read_text(encoding="utf-8")).get("delivery") == delivery_before
    assert agents.read_text(encoding="utf-8") == agent_before


def test_run_routes_a_structured_graph_contradiction_to_a_typed_human_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery_graph_read_failures"] = [
        {"code": "invalid_parent", "message": "parent graph contradicts itself"}
    ]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "run", "1")

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "deterministic_contradiction"
    state = load_only_run_state(git_repo)
    assert state["terminal_kind"] == "deterministic_contradiction"
    assert state["diagnostics"][0]["code"] == "invalid_parent"
    assert state["active_agent_invocation"] is None


def test_run_reconciles_ticket_close_visibility_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"ticket_close_event_lag_reads": 1},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["ticket_closed_by_run"] is True
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_run_fails_closed_for_unparseable_ticket_close_ownership(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "ticket_close_ownership_read_failures": [
                {
                    "code": "github_invalid_response",
                    "message": "Ticket timeline JSON is incomplete",
                }
            ]
        },
    )
    agents = _run_agents(git_repo / "agents.json")

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    state = load_only_run_state(git_repo)
    assert state["diagnostics"][0]["code"] == "github_invalid_response"
    invocation = state["active_agent_invocation"]
    assert isinstance(invocation, dict)
    assert invocation["status"] == "completed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert [entry["action"] for entry in delivery["mutations"]].count("close_issue") == 1
    assert not any(
        pull.get("scope") == "final_run" for pull in delivery["pull_requests"]
    )


def test_run_supervises_unobserved_ticket_close_intent_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"ticket_close_intent_missing_once": True},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["ticket_closed_by_run"] is True
    mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["mutations"]
    assert [entry["action"] for entry in mutations].count("close_issue") == 1


def test_run_reconciles_unknown_ticket_merge_outcomes_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"squash_merge_outcomes": ["unknown", "unknown", "merge"]},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["3"]
    assert job["merge_intent"]["attempts"] == 3
    assert [entry["attempt"] for entry in job["merge_reconciliation_history"]] == [1, 2]


def test_run_supervises_misclassified_final_checks_read_without_rewriting_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "run_required_checks_read_failures": [
                {
                    "code": "github_write_failed",
                    "message": "temporary gh pr checks failure",
                }
            ]
        },
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 2
    assert all(
        item.get("code") != "github_write_outcome_unknown"
        for item in state.get("diagnostics", [])
    )


def test_run_advances_multiple_tickets_without_final_merge(git_repo: Path) -> None:
    first = ticket()
    first.update({"number": 2, "title": "Complete ticket 2"})
    second = ticket()
    second.update({"number": 3, "title": "Complete ticket 3"})
    fixture = write_fixture(git_repo / "github.json", issues={"2": first, "3": second})
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "ticket-developer-2",
            "summary": "Delivered the second Ticket.",
            "write_files": {"second-feature.txt": "done\n"},
        }
    )
    data["publications"].append(publication())
    data["reviews"].append(
        passing_acceptance("ticket-reviewer-2", "The second Ticket passed.")
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    advanced = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert advanced.returncode == 0, advanced.stderr
    assert stdout_json(advanced)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert set(state["ticket_jobs"]) == {"2", "3"}
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    pulls = fixture_data["delivery"]["pull_requests"]
    assert len(pulls) == 3
    assert fixture_data["delivery"]["closed_issues"] == [2, 3]


def test_status_and_history_show_started_development_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "6",
    )

    assert interrupted.returncode == 2
    status = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")
    )
    assert status["worker"] == {
        "attempt": 1,
        "phase": "developing",
        "role": "开发工作代理",
        "thread_id": None,
    }
    timeline = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )["timeline"]
    assert any(
        entry.get("worker") == "开发工作代理"
        and entry.get("attempt") == 1
        and entry.get("phase") == "developing"
        for entry in timeline
    )


def test_status_keeps_allowed_actions_without_inventing_host_activity(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))

    for status in ("waiting_merge", "parent_closeout_pending"):
        state["status"] = status
        state_path.write_text(json.dumps(state), encoding="utf-8")
        rendered = invoke_cli_inprocess(git_repo, fixture, "status", run_id)
        assert rendered.returncode == 0, rendered.stderr
        if status == "waiting_merge":
            assert "无法确认后台等待是否仍在继续" in rendered.stdout
            assert "agent-run status --repo example/project --parent 1 --json" in rendered.stdout
            assert "先核验原执行的归属和退出状态" in rendered.stdout
            assert "agent-run doctor" not in rendered.stdout
            assert "agent-run run" not in rendered.stdout
        else:
            assert "下一步: agent-run run 1" in rendered.stdout
        audit = stdout_json(invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json"))
        assert audit["next_action"].startswith("agent-run run 1")


def test_premature_approve_does_not_mark_run_as_execution_failed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["error"] = {
        "code": "github_timeout",
        "message": "simulated transient read timeout",
    }
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    premature = run_cli(git_repo, fixture, "approve", run_id)

    assert premature.returncode == 2
    assert stdout_json(premature)["diagnostics"][-1]["code"] == "command_precondition"
    assert load_only_run_state(git_repo)["status"] == "active"

    status = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")
    )
    assert status["repository"] == "example/project"
    assert status["parent"]["number"] == 1
    assert status["worker"] is None
    assert isinstance(status["elapsed_seconds"], int)


def test_run_supervises_initial_graph_read_lag_in_one_call(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert started.returncode == 0, started.stderr
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {
            "code": "github_timeout",
            "message": "simulated exhausted read retry budget",
        }
        for _ in range(3)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    completed = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    consumed = json.loads(fixture.read_text(encoding="utf-8"))
    assert consumed["delivery_graph_read_failures"] == []
    pulls_before_recovery = list(consumed["delivery"]["pull_requests"])
    assert len(pulls_before_recovery) == 2


def test_run_restarts_a_paused_supervision_window(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = seed_run(git_repo, fixture, "1", idle_control=True)
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "temporary GitHub lag"}
        for _ in range(2)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["run_id"] == run_id
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    persisted = load_only_run_state(git_repo)
    assert persisted["status"] == "run_approval_pending"
    assert "supervision_wait" not in persisted


def test_publish_run_retries_final_pr_after_external_wait(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update({"status": "waiting_external", "terminal_kind": "waiting_external"})
    state["run_publication"]["phase"] = "waiting_checks"
    run_file.write_text(json.dumps(state), encoding="utf-8")

    retried = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert retried.returncode == 0, retried.stderr
    assert stdout_json(retried)["status"] == "run_approval_pending"


def test_run_supervises_read_failures_after_supervision_timeout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = seed_run(git_repo, fixture, "1", idle_control=True)
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "temporary GitHub lag"}
        for _ in range(2)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    assert load_only_run_state(git_repo)["status"] == "run_approval_pending"


def test_run_repauses_after_a_fresh_external_wait_window(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "persistent GitHub lag"}
        for _ in range(130)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "supervision_timeout"
    paused = load_only_run_state(git_repo)
    assert paused["status"] == "supervision_timeout"
    assert paused["supervision_wait"]["resume_status"] == "waiting_external"
    assert paused["supervision_wait"]["budget_seconds"] == 10 * 60


def test_public_run_and_resume_reuse_initial_fetch_failure_run(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    updater = tmp_path / "updater"
    subprocess.run(["git", "clone", str(remote), str(updater)], check=True)
    subprocess.run(["git", "config", "user.name", "Updater"], cwd=updater, check=True)
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    (updater / "remote.txt").write_text("remote\n", encoding="utf-8")
    subprocess.run(["git", "add", "remote.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "remote base"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=updater, check=True)
    remote_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    closed_ticket = ticket()
    closed_ticket["state"] = "CLOSED"
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": closed_ticket},
        default_head_sha=remote_head,
    )
    counter, environment = git_fetch_failure_wrapper(tmp_path, failures=3)

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        extra_env=environment,
    )

    assert failed.returncode == 2
    failed_output = stdout_json(failed)
    run_id = failed_output["run_id"]
    assert failed_output["status"] == "execution_failed"
    assert counter.read_text(encoding="utf-8") == "0\n"
    failed_state = load_only_run_state(git_repo)
    assert failed_state["base_resolution_pending"] is True
    assert not list((git_repo / ".git" / "refs" / "heads" / "agent-run").rglob("*"))

    held = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        extra_env=environment,
    )
    assert held.returncode == 2
    assert stdout_json(held)["run_id"] == run_id
    assert stdout_json(held)["status"] == "execution_failed"
    assert load_only_run_state(git_repo) == failed_state
    resumed = run_cli(git_repo, fixture, "resume", run_id, extra_env=environment)
    assert resumed.returncode == 2
    assert stdout_json(resumed)["run_id"] == run_id
    state = load_only_run_state(git_repo)
    assert state["status"] == "progress_exhausted"
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_run_reconciles_an_already_created_ticket_pr_after_response_loss(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"crash_after_ensure_ticket_pr_once": True},
    )
    agents = _run_agents(git_repo / "agents.json")

    interrupted = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert interrupted.returncode == 2
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 1
    interrupted_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    interrupted_retry = interrupted_job["publication_operation_retry"]
    publication_attempt = next(
        attempt
        for attempt in interrupted_job["semantic_attempt_history"]
        if attempt["role"] == "publication"
    )
    assert interrupted_retry == {"attempts": 1, "limit": 5}
    assert publication_attempt["publication_operation_retry"] == interrupted_retry
    interrupted_state = load_only_run_state(git_repo)
    fixture_before = fixture.read_text(encoding="utf-8")

    held = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert held.returncode == 2
    assert stdout_json(held)["status"] == "execution_failed"
    assert load_only_run_state(git_repo) == interrupted_state
    assert fixture.read_text(encoding="utf-8") == fixture_before

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        str(interrupted_state["run_id"]),
        "--agent-fixture",
        str(agents),
    )
    assert resumed.returncode == 0, resumed.stdout
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    recovered_state = load_only_run_state(git_repo)
    recovered_fixture = fixture.read_text(encoding="utf-8")

    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert recovered.returncode == 0, recovered.stdout
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    held_state = load_only_run_state(git_repo)
    assert held_state["ticket_jobs"] == recovered_state["ticket_jobs"]
    assert held_state["run_publication"] == recovered_state["run_publication"]
    assert (
        held_state["agent_invocation_history"]
        == recovered_state["agent_invocation_history"]
    )
    assert held_state["review_budget_protocol"] == recovered_state[
        "review_budget_protocol"
    ]
    assert fixture.read_text(encoding="utf-8") == recovered_fixture
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 2
    assert fixture_data["delivery"]["closed_issues"] == [3]
    recovered_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    recovered_publication = next(
        attempt
        for attempt in recovered_job["semantic_attempt_history"]
        if attempt["role"] == "publication"
    )
    assert recovered_job["publication_attempts"] == 1
    assert recovered_publication["attempt_id"] == publication_attempt["attempt_id"]
    assert recovered_publication["publication_operation_retry"] == interrupted_retry


def test_incompatible_state_preserves_existing_publisher_ledger_and_worktree(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"crash_after_ensure_ticket_pr_once": True},
    )
    agents = _run_agents(git_repo / "agents.json")

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )
    assert interrupted.returncode == 2
    state = load_only_run_state(git_repo)
    run_id = state["run_id"]
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "ticket-3"
    assert checkout.is_dir()
    preserved = checkout / "legacy-recovery.txt"
    preserved.write_text("do not clean or publish\n", encoding="utf-8")
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = state_path.read_text(encoding="utf-8")
    fixture_before = fixture.read_text(encoding="utf-8")
    delivery_before = json.loads(fixture_before)["delivery"]
    assert len(delivery_before["pull_requests"]) == 1
    assert delivery_before["pull_requests"][0]["number"] == 1
    assert delivery_before["pull_requests"][0]["base_branch"] == state["run_branch"]
    assert state["active_ticket_job"]["ticket_branch"] in delivery_before[
        "published_branches"
    ]
    worktrees_before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    rejected = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert state_path.read_text(encoding="utf-8") == state_before
    assert fixture.read_text(encoding="utf-8") == fixture_before
    assert preserved.read_text(encoding="utf-8") == "do not clean or publish\n"
    assert (
        subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == worktrees_before
    )


def test_ticket_linked_branch_display_crash_is_not_retried_on_recovery(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"crash_after_link_issue_branch_display_once": True},
    )
    agents = _run_agents(git_repo / "linked-branch-agents.json")

    interrupted = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    fixture_before = fixture.read_text(encoding="utf-8")

    held = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert held.returncode == 2
    assert stdout_json(held)["status"] == "execution_failed"
    assert load_only_run_state(git_repo) == interrupted_state
    assert fixture.read_text(encoding="utf-8") == fixture_before

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        str(interrupted_state["run_id"]),
        "--agent-fixture",
        str(agents),
    )
    assert resumed.returncode == 0, resumed.stdout
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    recovered_state = load_only_run_state(git_repo)
    recovered_fixture = fixture.read_text(encoding="utf-8")

    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert recovered.returncode == 0, recovered.stdout
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    held_state = load_only_run_state(git_repo)
    assert held_state["ticket_jobs"] == recovered_state["ticket_jobs"]
    assert held_state["run_publication"] == recovered_state["run_publication"]
    assert (
        held_state["agent_invocation_history"]
        == recovered_state["agent_invocation_history"]
    )
    assert held_state["review_budget_protocol"] == recovered_state[
        "review_budget_protocol"
    ]
    assert fixture.read_text(encoding="utf-8") == recovered_fixture
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert job["linked_branch_display"] == {
        "display_attempted": True,
        "status": "indeterminate",
    }
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(
        [
            attempt
            for attempt in delivery["linked_branch_display_attempts"]
            if attempt["issue_number"] == 3
        ]
    ) == 1


def test_completed_run_is_not_reopened_by_run_command(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(
        run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    )["run_id"]
    assert (
        stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"]
        == "completed"
    )

    replayed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert replayed.returncode == 2
    assert stdout_json(replayed)["run_id"] != run_id
    assert stdout_json(replayed)["status"] == "progress_exhausted"
    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (git_repo / ".agent-run" / "runs").glob("*.json")
    ]
    assert any(state["status"] == "completed" for state in states)
    assert any(state["status"] == "progress_exhausted" for state in states)
