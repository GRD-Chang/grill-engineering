from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_payloads import passing_acceptance, publication
from support.cli_supervision import _resume_repair_agents
from support.published_run import prepare_accepted_run, prepare_published_run
from support.workspace import managed_state


def _two_round_ci_repair_agents(path: Path) -> Path:
    agents = _resume_repair_agents(path, expected_thread_id=None)
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append({
        "expected_thread_id": "run-repair-developer-1",
        "thread_id": "run-repair-developer-1",
        "summary": "Repaired the internal PR check after the final PR failure.",
        "write_files": {"run-repair.txt": "recovered internal CI\n"},
    })
    data["publications"].append(publication())
    data["run_reviews"].append(passing_acceptance(
        "run-reviewer-internal-ci", "The internal PR CI repair passed."
    ))
    agents.write_text(json.dumps(data), encoding="utf-8")
    return agents


def _assert_final_ci_repair_then_approve(
    repo: Path, fixture: Path, state: dict[str, Any],
    paused: dict[str, Any], ordinary_budget: dict[str, Any],
) -> None:
    run = state["run_acceptance"]
    final = state["run_publication"]
    failed = paused["run_publication"]["required_checks_evidence"]
    original_evidence = run["ci_failed_evidence"]
    assert original_evidence["pr_number"] == failed["pr_number"]
    assert original_evidence["checks"][0]["job"]["head_sha"] == failed["head_sha"]
    assert original_evidence["checks"][0]["description"] == "The fixture required check failed."
    assert run["review_budget"] == ordinary_budget
    budget = run["ci_recovery"]
    assert budget["rounds_used"] == 2
    assert budget["repairs_used"] == 1
    assert budget["reviewer_invocations"] == 2
    assert budget["history"] == [{
        "kind": "round", "round": 1, "failure_head": failed["head_sha"], "repairs_used": 1,
    }]

    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    pulls = delivery["pull_requests"]
    final_pulls = [pull for pull in pulls if pull.get("scope") == "final_run"]
    repair_pulls = [pull for pull in pulls if pull.get("scope") == "run_repair"]
    assert len(final_pulls) == len(repair_pulls) == 1
    assert final_pulls[0]["number"] == final["pr_number"] == failed["pr_number"]
    assert final_pulls[0]["state"] == "OPEN"
    assert repair_pulls[0]["state"] == "MERGED"
    final_head = delivery["published_branches"][state["run_branch"]]
    assert final["required_checks_evidence"]["head_sha"] == final_head
    assert final["required_checks_evidence"]["result"] == "pass"
    assert final_head != failed["head_sha"]
    assert "approval_grant" not in final
    archived = run["completed_repair_jobs"][-1]["ci_evidence"]
    assert archived["pr_number"] == repair_pulls[0]["number"]
    assert archived["head_sha"] == budget["failure_head"]
    assert archived["result"] == "fail"
    assert archived["checks"][0]["bucket"] == "fail"

    record = run["acceptance_record"]
    assert run["phase"] == "accepted"
    assert record["acceptance_state"] == "integrated"
    assert record["reviewed_head_sha"] == final_head
    assert record["reviewed_candidate_sha"] == run["candidate_sha"]
    assert all(check["status"] == "pass" for check in record["artifact"]["checks"].values())
    assert run["candidate_acceptance_history"]

    approved = run_cli(repo, fixture, "approve", str(state["run_id"]))
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    delivered = load_only_run_state(repo)
    assert delivered["run_publication"]["approval_grant"]["head_sha"] == final_head
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    final_pull = next(pull for pull in delivery["pull_requests"] if pull["number"] == final["pr_number"])
    assert final_pull["state"] == "MERGED"
    assert 1 in delivery["closed_issues"]


def test_run_supervises_initial_final_check_evidence_reads(
    git_repo: Path,
) -> None:
    fixture, accepted = prepare_accepted_run(
        git_repo,
        delivery={
            "required_checks": ["fail"],
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": "github",
                    "message": "final evidence unavailable",
                }
                for _ in range(3)
            ],
        },
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["supervision_clock_multiplier"] = 120
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = _resume_repair_agents(git_repo / "agents.json", expected_thread_id=None)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert run["ci_recovery"] == accepted["run_acceptance"]["ci_recovery"]
    assert run["review_budget"] == accepted["run_acceptance"]["review_budget"]
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
            "required_checks": ["fail", "fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _two_round_ci_repair_agents(git_repo / "recovery-agents.json")
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
    _assert_final_ci_repair_then_approve(
        git_repo, fixture, completed, paused_state,
        accepted["run_acceptance"]["review_budget"],
    )

def test_run_supervises_post_approval_final_check_evidence_reads(
    git_repo: Path,
) -> None:
    fixture, awaiting = prepare_published_run(git_repo)
    run_id = str(awaiting["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture_data["delivery"].update(
        {
            "required_checks": ["fail"],
            "check_position": 0,
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": "github",
                    "message": "approved evidence unavailable",
                }
                for _ in range(4)
            ],
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert waiting.returncode == 2
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    waiting_state = load_only_run_state(git_repo)
    grant = waiting_state["run_publication"]["approval_grant"]
    assert grant["pr_number"] == awaiting["run_publication"]["pr_number"]
    assert grant["head_sha"] == awaiting["run_publication"]["required_checks_evidence"]["head_sha"]
    assert waiting_state["run_acceptance"]["ci_recovery"] == awaiting["run_acceptance"]["ci_recovery"]
    assert waiting_state["run_acceptance"]["review_budget"] == awaiting["run_acceptance"]["review_budget"]
    assert waiting_state["run_acceptance"].get("repair_generation", 0) == 0
    assert not any(
        invocation.get("work_subject") == f"run-repair:{run_id}"
        for invocation in waiting_state["agent_invocation_history"]
    )

    assert waiting_state["supervision_wait"]["kind"] == "github_convergence"
    assert waiting_state["run_publication"]["approval_grant"] == grant

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "required_check_evidence_failures": [],
            "required_checks": ["fail", "fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _two_round_ci_repair_agents(git_repo / "recovery-agents.json")
    control_path = next((managed_state(git_repo) / "task-control").glob("*.json"))
    control_before = control_path.read_bytes()
    fixture_before = fixture.read_bytes()
    refused = run_cli(git_repo, fixture, "run", "1")
    assert refused.returncode == 2, refused.stdout
    assert stdout_json(refused)["status"] == "supervision_timeout"
    assert load_only_run_state(git_repo) == waiting_state
    assert control_path.read_bytes() == control_before
    assert fixture.read_bytes() == fixture_before
    recovered = run_cli(
        git_repo, fixture, "resume", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 2, recovered.stderr
    invalidated = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_acceptance_pending"
    assert "approval_grant" not in invalidated["run_publication"]
    assert invalidated["agent_invocation_history"] == waiting_state["agent_invocation_history"]
    queued = invalidated["run_acceptance"]
    assert queued["repair_request"]["repair_source"] == "required_checks"
    assert queued["repair_request"]["ci_evidence"] == queued["ci_failed_evidence"]
    assert queued["ci_failed_evidence"]["pr_number"] == grant["pr_number"]
    assert queued["ci_failed_evidence"]["checks"][0]["job"]["head_sha"] == grant["head_sha"]
    assert queued["ci_recovery"]["failure_head"] == grant["head_sha"]
    assert queued["ci_recovery"]["rounds_used"] == 1
    assert queued["ci_recovery"]["repairs_used"] == 0
    assert queued["review_budget"] == awaiting["run_acceptance"]["review_budget"]
    assert "repair_job" not in queued
    assert not any(
        pull.get("scope") == "final_run" and pull.get("state") == "MERGED"
        for pull in json.loads(fixture.read_text())["delivery"]["pull_requests"]
    )

    repaired = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )
    assert repaired.returncode == 0, repaired.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(repaired)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
    _assert_final_ci_repair_then_approve(
        git_repo, fixture, completed, waiting_state,
        awaiting["run_acceptance"]["review_budget"],
    )
