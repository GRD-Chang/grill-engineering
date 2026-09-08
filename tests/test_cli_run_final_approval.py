from __future__ import annotations

import json
from pathlib import Path


from cli_fixtures import run_agents
from conftest import seed_run, write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import ticket

from cli_run_supervision_support import (
    _assert_public_wait_projection,
    _interrupt_run,
    _parent_only_agents,
    _run_until_pending_window,
)


def test_run_supervises_pending_final_run_checks_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2

def test_final_run_approval_survives_pending_checks_until_the_same_pr_merges(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")

    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert stdout_json(awaiting_approval)["status"] == "run_approval_pending"
    run_id = str(stdout_json(awaiting_approval)["run_id"])

    completed = run_cli(git_repo, fixture, "approve", run_id)
    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    granted = load_only_run_state(git_repo)["run_publication"]["approval_grant"]
    assert granted["pr_number"] == 2
    assert granted["repository"] == "example/project"
    assert (
        load_only_run_state(git_repo)["run_publication"]["approval_grant"][
            "granted_at"
        ]
        == granted["granted_at"]
    )
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"][1]["state"] == "MERGED"

def test_final_approval_supervises_a_transient_required_checks_read_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")

    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(awaiting_approval)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "required checks unavailable"}
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "approve", run_id)
    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    final_state = load_only_run_state(git_repo)
    assert final_state["diagnostics"] == []
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2


def test_final_approval_supervises_live_pr_read_after_snapshot_wait(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(awaiting_approval)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "snapshot unavailable"}
    ]
    fixture_data["delivery"]["open_live_pull_request_failures"] = [
        {
            "scope": "final_run",
            "type": "github",
            "code": "github_timeout",
            "message": "live PR unavailable",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "approve", run_id)

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    final_state = load_only_run_state(git_repo)
    assert final_state["status"] == "completed"
    assert final_state["diagnostics"] == []
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2


def test_final_approval_required_checks_read_timeout_preserves_its_grant_for_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(awaiting_approval)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 1
    fixture_data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "required checks unavailable"}
        for _ in range(32)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    paused = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(paused)["status"] == "supervision_timeout"
    grant = load_only_run_state(git_repo)["run_publication"]["approval_grant"]

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert (
        paused_state["supervision_wait"]["deadline"]
        - paused_state["supervision_wait"]["started_at"]
        == 10 * 60
    )
    assert paused_state["supervision_wait"]["retry_count"] >= 5
    assert "publication_operation_retry" not in paused_state["run_publication"]
    assert paused_state["run_publication"]["approval_grant"] == grant
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["run_required_checks_read_failures"] = []
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    resumed = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"] == grant
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2

def test_final_run_approval_recovers_a_lost_merge_response_without_reapproval(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_normal_merge_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(completed)["status"] == "completed"
    granted_at = load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"]

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"] == granted_at

def test_final_run_supervises_a_recovery_read_failure_without_reapproval(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update(
        {
            "crash_after_normal_merge_once": True,
            "merged_live_pull_request_failures": [
                {"code": "github_read_failed", "message": "readback unavailable"}
            ],
        }
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(completed)["status"] == "completed"
    granted_at = load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"]

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"] == granted_at

def test_final_run_recovers_a_lost_parent_closeout_response(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_parent_close_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "approve", run_id)

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["closed_issues"].count(1) == 1

def test_run_recovers_parent_only_check_timeout_without_duplicate_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending"]},
        supervision_clock_multiplier=540,
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "required_checks"
    window = paused_state["supervision_window"]
    assert window["deadline"] - window["started_at"] == 45 * 60
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 1

    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "parent_approval_pending"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1

def test_seeded_repository_read_wait_is_immediately_observable_without_run(
    git_repo: Path,
) -> None:
    secret = "ghp_start_wait_secret"
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        repository_read_failures=[
            {
                "code": "github_timeout",
                "message": f"repository read failed with token {secret}",
            }
        ],
    )

    started = seed_run(git_repo, fixture, "1")

    assert started.returncode == 0, started.stderr
    assert stdout_json(started)["status"] == "waiting_external"
    _assert_public_wait_projection(
        git_repo, fixture, str(stdout_json(started)["run_id"]), secret=secret
    )

def test_parent_approval_supervises_pending_checks_to_timeout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["none", "pending"]},
        # Advance the existing clock; keep the real supervision budget.
        supervision_clock_multiplier=120,
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert awaiting.returncode == 0, awaiting.stderr
    assert stdout_json(awaiting)["status"] == "parent_approval_pending"
    waiting = run_cli(git_repo, fixture, "approve", str(stdout_json(awaiting)["run_id"]))

    assert waiting.returncode == 2, waiting.stderr
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    assert load_only_run_state(git_repo)["supervision_wait"]["kind"] == "required_checks"
    wait = load_only_run_state(git_repo)["supervision_wait"]
    assert wait["deadline"] - wait["started_at"] == 45 * 60
    assert wait["elapsed_seconds"] >= 45 * 60
    assert wait["retry_count"] > 1

def test_final_approval_supervises_pending_checks_to_timeout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending"]},
        # Advance the existing clock; keep the real supervision budget.
        supervision_clock_multiplier=120,
    )
    agents = run_agents(git_repo / "agents.json")
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert awaiting.returncode == 0, awaiting.stderr
    assert stdout_json(awaiting)["status"] == "run_approval_pending"
    waiting = run_cli(git_repo, fixture, "approve", str(stdout_json(awaiting)["run_id"]))

    assert waiting.returncode == 2, waiting.stderr
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    assert load_only_run_state(git_repo)["supervision_wait"]["kind"] == "required_checks"
    wait = load_only_run_state(git_repo)["supervision_wait"]
    assert wait["deadline"] - wait["started_at"] == 45 * 60
    assert wait["elapsed_seconds"] >= 45 * 60
    assert wait["retry_count"] > 1

def test_parent_only_pending_window_survives_process_restart(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    first_process, first_state = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(first_process, git_repo)
    first_window = first_state["supervision_window"]
    assert isinstance(first_window, dict)
    first_invocations = first_state["agent_invocation_history"]

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock"] = first_window["started_at"] + 60
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    second_process, _ = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(second_process, git_repo)

    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["supervision_window"] == first_window
    assert resumed_state["agent_invocation_history"] == first_invocations
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1

    delivery.update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps({**fixture_data, "delivery": delivery}), encoding="utf-8")
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "parent_approval_pending"
    final_state = load_only_run_state(git_repo)
    assert final_state["agent_invocation_history"] == first_invocations
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1

def test_run_recovers_final_run_check_timeout_without_duplicate_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending"]},
        supervision_clock_multiplier=540,
    )
    agents = run_agents(git_repo / "agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["run_publication"]["phase"] == "waiting_checks"
    window = paused_state["supervision_window"]
    assert window["deadline"] - window["started_at"] == 45 * 60
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 2

    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2

def test_final_run_pending_window_survives_process_restart(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")

    first_process, first_state = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(first_process, git_repo)
    first_window = first_state["supervision_window"]
    assert isinstance(first_window, dict)
    first_invocations = first_state["agent_invocation_history"]

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock"] = first_window["started_at"] + 60
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    second_process, _ = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(second_process, git_repo)

    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["supervision_window"] == first_window
    assert resumed_state["agent_invocation_history"] == first_invocations
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 2

    delivery.update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps({**fixture_data, "delivery": delivery}), encoding="utf-8")
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    final_state = load_only_run_state(git_repo)
    assert final_state["agent_invocation_history"] == first_invocations
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2
