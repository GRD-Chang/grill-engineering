"""真实 CLI 验证已结束的 CI 调查暂停与新工作恢复，不依赖 Invocation 中断。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from support.cli import load_only_run_state, run_cli
from support.cli_payloads import final_run_agents, passing_acceptance, publication, ticket
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import write_fixture


@pytest.mark.parametrize(
    ("resume_args", "limit", "review"),
    [((), 3, False), (("--ticket", "3"), 3, True), (("--ticket", "3"), 1, False)],
)
def test_public_resume_after_ci_investigation_starts_new_ci_work(
    git_repo: Path, resume_args: tuple[str, ...], limit: int, review: bool,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"3": ticket()},
        delivery={
            "required_checks": ["fail", "pass", "pass"],
            "required_check_evidence": {"checks": [{
                "name": "platform", "workflow": "CI", "bucket": "fail",
                "state": "FAILURE", "repairability": "unknown",
                "link": "https://example.invalid/checks/platform",
                "logs": "Original CI platform failure",
            }]},
        },
    )
    agents = git_repo / "investigation.json"
    initial = final_run_agents()
    initial["developments"] = [
        {"thread_id": "ci-investigator", "expected_thread_id": None,
         "summary": "Initial Ticket implementation.", "write_files": {"feature.txt": "initial\n"}},
        {"thread_id": "ci-investigator", "expected_thread_id": "ci-investigator",
         "summary": "Investigated the CI failure without changing code."},
    ]
    agents.write_text(json.dumps(initial), encoding="utf-8")
    blocked = run_cli(
        git_repo, fixture, "run", "1", "--ticket-ci-development-limit", str(limit),
        "--ticket-ci-review" if review else "--no-ticket-ci-review",
        "--agent-fixture", str(agents),
    )
    assert blocked.returncode == 2, blocked.stdout
    before = load_only_run_state(git_repo)
    job = before["ticket_jobs"]["3"]
    expected_reason = "ci_development_budget_exhausted" if limit == 1 else "ci_investigation_paused"
    assert job["blocked_reason"] == expected_reason
    assert job["attempt_kind"] == job["next_attempt_kind"] == "ci_repair"
    assert job["ci_recovery"]["developments_used"] == 1
    assert job["ci_recovery"]["window"] == 1
    assert job.get("pending_semantic_attempt") is None
    assert job["review_budget"]["development_attempts"] == 1
    evidence = job["ci_evidence"]
    assert evidence["head_sha"] == job["publication_sha"]
    assert evidence["checks"][0]["logs"] == "Original CI platform failure"
    original_budget = job["review_budget"]
    original_history = job["semantic_attempt_history"]
    assert [item["outcome"] for item in original_history if item["role"] == "development"] == ["candidate", "no_code_changes"]
    for command in ("status", "history"):
        result = invoke_cli_inprocess(git_repo, fixture, command, before["run_id"])
        assert result.returncode == 0, result.stdout
        assert "agent-run resume" in result.stdout
    empty = git_repo / "empty.json"
    empty.write_text(json.dumps({"developments": [], "reviews": [], "publications": []}), encoding="utf-8")
    # An ordinary poll must not clear this pause or allocate new CI work.
    polled = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(empty))
    assert polled.returncode == 2, polled.stdout
    unchanged = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    for key in ("ci_recovery", "review_budget", "semantic_attempt_history", "ci_evidence"):
        assert unchanged[key] == job[key]

    recovered = final_run_agents()
    recovered["developments"] = [{
        "thread_id": "ci-investigator", "expected_thread_id": "ci-investigator",
        "summary": "Implemented the fix after the completed CI investigation.",
        "write_files": {"feature.txt": "CI fixed\n"},
    }]
    recovered["publications"] = [publication()]
    recovered["reviews"] = [passing_acceptance("ci-reviewer", "CI repair passed.")] if review else []
    agents.write_text(json.dumps(recovered), encoding="utf-8")
    resumed = run_cli(git_repo, fixture, "resume", before["run_id"], *resume_args, "--agent-fixture", str(agents))
    assert resumed.returncode == 0, resumed.stdout
    completed = load_only_run_state(git_repo)
    final = completed["ticket_jobs"]["3"]
    assert final["phase"] == "completed"
    assert final["review_budget"] == original_budget
    assert final["ci_evidence"] == evidence
    assert final["attempt_kind"] == "ci_repair"
    assert final["ci_recovery"]["window"] == (2 if limit == 1 else 1)
    assert final["ci_recovery"]["developments_used"] == (1 if limit == 1 else 2)
    assert final["ci_recovery"]["reviewer_invocations"] == int(review)
    attempts = [item for item in final["semantic_attempt_history"] if item["role"] == "development"]
    assert [item["outcome"] for item in attempts] == ["candidate", "no_code_changes", "candidate"]
    assert len({item["attempt_id"] for item in attempts}) == 3
    assert attempts[1] == next(item for item in original_history if item["outcome"] == "no_code_changes")
    assert final["required_checks_evidence"]["head_sha"] == final["publication_sha"]
    assert final["required_checks_evidence"]["result"] == "pass"
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
    assert pulls[0]["base_branch"] == completed["run_branch"]
    assert pulls[0]["state"] == "MERGED"
