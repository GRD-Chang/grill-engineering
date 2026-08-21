from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import passing_acceptance, publication, ticket

from cli_run_supervision_support import (
    _assert_credential_wait_is_not_public,
    _repair_agents,
    _resume_repair_agents,
)



def test_run_repair_resume_preserves_partial_worker_edits_and_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "fail", "pass", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "unused after the injected failure",
            "write_files": {"partial-repair.txt": "preserve this edit\n"},
            "error_after_writes": "simulated Run Repair Worker failure",
        }
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    interrupted = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert interrupted.returncode == 2
    failed = load_only_run_state(git_repo)
    run = failed["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    generation = run["repair_generation"]
    assert failed["status"] == "execution_failed"
    assert run["repair_cycle"]["status"] == "active"
    assert job["phase"] == "developing"
    assert job["pending_attempt"] == 1
    assert checkout.exists()
    assert (checkout / "partial-repair.txt").read_text(encoding="utf-8") == (
        "preserve this edit\n"
    )

    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json",
        expected_thread_id="run-repair-developer-1",
    )
    recovery_data = json.loads(recovery_agents.read_text(encoding="utf-8"))
    recovery_data["developments"][0]["expected_files"] = {
        "partial-repair.txt": "preserve this edit\n"
    }
    recovery_agents.write_text(json.dumps(recovery_data), encoding="utf-8")
    recovered = run_cli(
        git_repo,
        fixture,
        "resume",
        str(failed["run_id"]),
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    completed_run = completed["run_acceptance"]
    assert stdout_json(recovered)["status"] == "run_publication_pending"
    assert completed_run["repair_generation"] == generation
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1
    assert completed_run["development_thread_history"] == ["run-repair-developer-1"]
    assert not checkout.exists()

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_repair_trigger_live_pr_readback(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    initial_agents = run_agents(git_repo / "initial-agents.json")
    awaiting = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(initial_agents)
    )
    run_id = str(stdout_json(awaiting)["run_id"])
    assert stdout_json(awaiting)["status"] == "run_approval_pending"

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {"required_checks": ["fail"], "check_position": 0}
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    queued = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(queued)["status"] == "run_acceptance_pending"

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture_data["delivery"]["open_live_pull_request_failures"] = [
        {
            "scope": "final_run",
            "type": error_type,
            "code": "github_read_failed",
            "message": "repair trigger readback unavailable",
        }
        for _ in range(3)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )

    paused = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    assert paused_state["status"] == "supervision_timeout"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["repair_trigger_pending"] is True
    assert job["development_thread_id"] is None
    assert "candidate_sha" not in job

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "open_live_pull_request_failures": [],
            "required_checks": ["pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    completed_run = completed["run_acceptance"]
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed_run["repair_generation"] == 1
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_repair_pr_failed_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    failures = [
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
    ]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail", "fail"],
            "required_check_evidence_failures": failures,
        },
        supervision_clock_multiplier=120,
    )
    agents = _repair_agents(git_repo / "agents.json", repair_generations=2)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["retry_count"] >= 2
    assert job["phase"] == "publishing"
    assert job["modification_attempts"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert job["development_thread_id"] == "run-repair-developer-1"
    assert checkout.exists()

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {"required_checks": ["fail", "pass", "pass"], "check_position": 0}
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json",
        expected_thread_id="run-repair-developer-1",
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
    repair_developments = [
        invocation
        for invocation in completed["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{completed['run_id']}"
        and invocation.get("role") == "development"
    ]
    assert [item["reported_thread_id"] for item in repair_developments] == [
        "run-repair-developer-1",
        "run-repair-developer-1",
    ]

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_required_check_trigger_fingerprint_reads(
    git_repo: Path, error_type: str
) -> None:
    failures = [
        {
            "scope": "final_run",
            "type": error_type,
            "message": "fingerprint unavailable",
            "remaining_successes": 1,
        },
        {"scope": "final_run", "type": error_type, "message": "fingerprint unavailable"},
        {"scope": "final_run", "type": error_type, "message": "fingerprint unavailable"},
    ]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail"],
            "required_check_evidence_failures": failures,
        },
        supervision_clock_multiplier=120,
    )
    agents = _repair_agents(git_repo / "agents.json", repair_generations=1)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    assert paused_state["status"] == "supervision_timeout"
    assert job["phase"] == "developing"
    assert job["modification_attempts"] == 0
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["development_thread_id"] is None
    assert checkout.exists()

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_initial_final_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail"],
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": error_type,
                    "message": "final evidence unavailable",
                }
                for _ in range(3)
            ],
        },
        supervision_clock_multiplier=120,
    )
    agents = _repair_agents(git_repo / "agents.json", repair_generations=1)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert run.get("repair_generation", 0) == 0
    assert "repair_job" not in run
    assert not any(
        invocation.get("work_subject") == f"run-repair:{paused_state['run_id']}"
        for invocation in paused_state["agent_invocation_history"]
    )

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "required_check_evidence_failures": [],
            "required_checks": ["fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_post_approval_final_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _repair_agents(git_repo / "agents.json", repair_generations=1)
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(awaiting)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture_data["delivery"].update(
        {
            "required_checks": ["fail"],
            "check_position": 0,
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": error_type,
                    "message": "approved evidence unavailable",
                }
                for _ in range(4)
            ],
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert waiting.returncode == 0
    assert stdout_json(waiting)["status"] == "waiting_external"
    waiting_state = load_only_run_state(git_repo)
    grant = waiting_state["run_publication"]["approval_grant"]
    assert waiting_state["run_acceptance"].get("repair_generation", 0) == 0
    assert not any(
        invocation.get("work_subject") == f"run-repair:{run_id}"
        for invocation in waiting_state["agent_invocation_history"]
    )

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert paused_state["run_publication"]["approval_grant"] == grant
    assert paused_state["run_acceptance"].get("repair_generation", 0) == 0

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "required_check_evidence_failures": [],
            "required_checks": ["fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1

@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase"),
    [
        ("run_reviews:run_repair", "fresh_acceptance", "reviewing"),
        ("publications:run", "publication", "publication"),
    ],
)
def test_run_retries_initial_credential_before_starting_each_run_repair_worker(
    git_repo: Path, fixture_role: str, invocation_role: str, phase: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "fail", "pass", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "Repaired the failed final Run check.",
            "write_files": {"run-repair.txt": "repaired\n"},
        }
    )
    data["publications"].append(publication())
    data["run_reviews"].extend(
        [
            passing_acceptance("run-repair-reviewer-1", "The Run repair passed."),
            passing_acceptance("run-reviewer-2", "The repaired Run passed."),
            passing_acceptance("run-reviewer-3", "The refreshed Run passed."),
        ]
    )
    data["run_publications"].append(data["run_publications"][0])
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"]
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert "credential_availability" not in state
    assert "supervision_window" not in state
    _assert_credential_wait_is_not_public(git_repo, fixture, str(state["run_id"]))
    attempts = [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == invocation_role
        and invocation.get("phase") == phase
        and invocation.get("work_subject") == f"run-repair:{state['run_id']}"
    ]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "completed"
