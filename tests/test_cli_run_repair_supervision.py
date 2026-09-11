from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_fixtures import run_agents
from conftest import write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import load_only_run_state, run_cli, run_internal_stage, stdout_json
from test_cli_delivery import (
    HUMAN_BLOCKER,
    passing_acceptance,
    publication,
    repair_acceptance,
    ticket,
)
from cli_run_supervision_support import (
    _assert_credential_wait_is_not_public,
    _repair_agents,
    _resume_repair_agents,
)


def _run_repair_human_artifact() -> dict[str, object]:
    artifact = passing_acceptance(
        "run-repair-reviewer-blocked", "The Run repair needs maintainer access."
    )
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": (
            "发生：GitHub 拒绝访问 Parent Issue；尝试：执行 gh issue view；"
            "人必须：授予 Issue 读取权限。"
        ),
        "findings": [],
    }
    artifact["expected_thread_id"] = None
    return artifact


def _run_repair_human_blocker_agents(path: Path, role: str) -> Path:
    agents = run_agents(path)
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["run_reviews"] = [repair_acceptance("run-reviewer-initial")]
    development: dict[str, object] = {
        "expected_thread_id": None,
        "thread_id": "run-repair-development-blocked",
        "summary": "Prepared the Run repair candidate.",
        "write_files": {"run-repair.txt": "repaired\n"},
    }
    if role == "development":
        development = {
            "expected_thread_id": None,
            "thread_id": "run-repair-development-blocked",
            "write_files": {
                "feature.txt": "blocked repair edit\n",
                "partial-repair.txt": "unfinished repair\n",
            },
            "human_blockers": [HUMAN_BLOCKER],
        }
    data["developments"].append(development)
    if role in {"reviewer", "publication"}:
        review = (
            _run_repair_human_artifact()
            if role == "reviewer"
            else passing_acceptance(
                "run-repair-reviewer", "The Run repair candidate passed."
            )
        )
        data["run_reviews"].append(review)
    if role == "publication":
        data["publications"].append(
            {
                "expected_thread_id": "run-repair-development-blocked",
                "thread_id": "run-repair-development-blocked",
                "human_blockers": [HUMAN_BLOCKER],
            }
        )
    agents.write_text(json.dumps(data), encoding="utf-8")
    return agents


def _run_repair_human_resume_agents(
    path: Path, role: str, *, expected_thread_id: str | None
) -> Path:
    successor_thread = expected_thread_id or f"run-repair-{role}-successor"
    data: dict[str, object] = {
        "developments": [],
        "publications": [],
        "reviews": [],
        "run_reviews": [],
        "run_publications": [
            {
                "commit_message": "fix(run): publish resumed repair",
                "pr_title": "fix(run): publish resumed repair",
                "pr_body_markdown": (
                    "## What Problem This Solves\n\nThe Run repair was blocked.\n\n"
                    "## Why This Change Was Made\n\nThe authorized Attempt resumed.\n\n"
                    "## User Impact\n\nThe complete delivery remains reviewable.\n\n"
                    "## Evidence\n\nThe public Resume contract passed."
                ),
            }
        ],
    }
    if role == "development":
        data["developments"] = [
            {
                "expected_thread_id": expected_thread_id,
                "thread_id": successor_thread,
                "summary": "Completed the resumed Run repair.",
                "expected_files": {
                    "feature.txt": "blocked repair edit\n",
                    "partial-repair.txt": "unfinished repair\n",
                },
                "write_files": {"run-repair.txt": "resumed\n"},
            }
        ]
        data["run_reviews"] = [
            passing_acceptance(
                "run-repair-reviewer-after-resume", "The resumed repair passed."
            )
        ]
        data["publications"] = [publication()]
    elif role == "reviewer":
        review = passing_acceptance(successor_thread, "The resumed repair passed.")
        review["expected_thread_id"] = expected_thread_id
        data["run_reviews"] = [review]
        data["publications"] = [publication()]
    else:
        data["publications"] = [
            {
                **publication(),
                "expected_thread_id": expected_thread_id,
                "thread_id": successor_thread,
            }
        ]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("role", ["development", "reviewer", "publication"])
@pytest.mark.parametrize(
    ("resume_args", "expected_thread_id", "expected_mode"),
    [
        ((), "blocked", "resume"),
        (("--new-thread",), None, "new-thread"),
    ],
)
def test_public_run_repair_human_blocker_resume_reuses_semantic_attempt(
    git_repo: Path,
    role: str,
    resume_args: tuple[str, ...],
    expected_thread_id: str | None,
    expected_mode: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    blocked_agents = _run_repair_human_blocker_agents(
        git_repo / "blocked-agents.json", role
    )

    blocked_result = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(blocked_agents)
    )

    assert blocked_result.returncode == 2, blocked_result.stderr
    blocked_state = load_only_run_state(git_repo)
    blocked_run = blocked_state["run_acceptance"]
    blocked_job = blocked_run["repair_job"]
    pending = blocked_job["pending_semantic_attempt"]
    generation = blocked_job["repair_generation"]
    assert pending["role"] == role
    assert pending["work_subject"] == f"run-repair:{blocked_state['run_id']}"
    assert pending["generation"] == generation
    assert pending["ordinal"] == 1
    assert pending["budget_window"] == (None if role == "publication" else 1)
    assert pending["status"] == "pending"
    repair_branch = blocked_job["repair_branch"]
    checkout = Path(blocked_job["repair_checkout"])
    if role == "development":
        assert checkout.exists()
        assert (checkout / "feature.txt").read_text(encoding="utf-8") == (
            "blocked repair edit\n"
        )
        assert (checkout / "partial-repair.txt").read_text(encoding="utf-8") == (
            "unfinished repair\n"
        )
    else:
        assert not checkout.exists()
    blocked_delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert not any(
        pull.get("scope") == "run_repair"
        for pull in blocked_delivery["pull_requests"]
    )
    if role == "development":
        role_thread = blocked_job["development_thread_id"]
    elif role == "reviewer":
        role_thread = blocked_job["reviewer_thread_ids"][-1]
    else:
        role_thread = blocked_job["publication_thread_id"]
    if expected_thread_id == "blocked":
        expected_thread_id = role_thread

    recovery_agents = _run_repair_human_resume_agents(
        git_repo / "recovery-agents.json",
        role,
        expected_thread_id=expected_thread_id,
    )
    resumed_result = run_cli(
        git_repo,
        fixture,
        "resume",
        str(blocked_state["run_id"]),
        "--message",
        "The blocking access has been restored.",
        "--agent-fixture",
        str(recovery_agents),
        *resume_args,
    )

    assert resumed_result.returncode == 0, (
        resumed_result.stdout + "\n" + resumed_result.stderr
    )
    resumed_state = load_only_run_state(git_repo)
    resumed_run = resumed_state["run_acceptance"]
    assert resumed_run["repair_generation"] == generation
    assert resumed_run["repair_cycle"]["status"] == "promoted"
    assert not checkout.exists()
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    repair_pulls = [
        pull
        for pull in delivery["pull_requests"]
        if pull.get("scope") == "run_repair"
    ]
    assert len(repair_pulls) == 1
    assert repair_pulls[0]["state"] == "MERGED"
    assert repair_branch not in delivery["published_branches"]
    assert [
        mutation
        for mutation in delivery["mutations"]
        if mutation.get("action") == "delete_managed_branch"
        and mutation.get("branch") == repair_branch
    ] == [{"action": "delete_managed_branch", "branch": repair_branch}]
    successor = next(
        invocation
        for invocation in reversed(resumed_state["agent_invocation_history"])
        if invocation.get("resume_id") is not None
        and invocation["semantic_attempt"]["attempt_id"] == pending["attempt_id"]
    )
    assert successor["mode"] == expected_mode
    assert successor["requested_thread_id"] == expected_thread_id
    assert successor["reported_thread_id"] == (
        expected_thread_id or f"run-repair-{role}-successor"
    )
    assert successor["generation"] == generation
    assert successor["semantic_attempt"]["attempt_id"] == pending["attempt_id"]
    attempt_invocations = [
        invocation
        for invocation in resumed_state["agent_invocation_history"]
        if invocation.get("work_subject")
        == f"run-repair:{blocked_state['run_id']}"
        and invocation.get("semantic_attempt", {}).get("attempt_id")
        == pending["attempt_id"]
    ]
    assert len(attempt_invocations) == 2
    assert {item["semantic_attempt"]["ordinal"] for item in attempt_invocations} == {
        pending["ordinal"]
    }
    assert {
        item["semantic_attempt"]["budget_window"] for item in attempt_invocations
    } == {pending["budget_window"]}
    resume_event = resumed_state["resume_audit"]["history"][-1]
    assert resume_event["semantic_attempt_id"] == pending["attempt_id"]
    assert resume_event["new_thread"] is (expected_mode == "new-thread")
    assert resume_event["human_response_supplied"] is True
    assert successor["resume_id"] == resume_event["resume_id"]
    history = stdout_json(
        invoke_cli_inprocess(
            git_repo,
            fixture,
            "history",
            str(blocked_state["run_id"]),
            "--json",
        )
    )
    assert history["agent_resumes"][-1] == resume_event
    for detail_args in ((), ("--details",)):
        human_history = invoke_cli_inprocess(
            git_repo,
            fixture,
            "history",
            str(blocked_state["run_id"]),
            "--plain",
            *detail_args,
        )
        assert human_history.returncode == 0, human_history.stderr
        assert human_history.stdout.count("人工阻塞：") == 1


def test_public_run_repair_human_blocker_currentness_drift_retires_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    blocked_agents = _run_repair_human_blocker_agents(
        git_repo / "blocked-agents.json", "development"
    )
    blocked_result = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(blocked_agents)
    )
    assert blocked_result.returncode == 2, blocked_result.stderr
    blocked_state = load_only_run_state(git_repo)
    blocked_job = blocked_state["run_acceptance"]["repair_job"]
    attempt_id = blocked_job["pending_semantic_attempt"]["attempt_id"]
    checkout = Path(blocked_job["repair_checkout"])

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["parent"]["body"] += "\nFresh parent requirement."
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    no_agents = git_repo / "no-agents.json"
    no_agents.write_text("{}", encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        str(blocked_state["run_id"]),
        "--message",
        "The old blocker has been resolved.",
        "--new-thread",
        "--agent-fixture",
        str(no_agents),
    )

    assert resumed.returncode == 0, resumed.stdout + "\n" + resumed.stderr
    refreshed = load_only_run_state(git_repo)
    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["terminal_kind"] == "run_acceptance_stale"
    assert refreshed["active_agent_invocation"] is None
    assert "repair_job" not in refreshed["run_acceptance"]
    retired = refreshed["retired_semantic_attempt_owners"][-1]
    matching = [
        attempt
        for attempt in retired["semantic_attempt_history"]
        if attempt["attempt_id"] == attempt_id
    ]
    assert len(matching) == 1
    assert matching[0]["outcome"] == "currentness_invalidated"
    resume_event = refreshed["resume_audit"]["history"][-1]
    assert resume_event["semantic_attempt_id"] == attempt_id
    assert resume_event["human_response_supplied"] is True
    assert resume_event["new_thread"] is True
    assert resume_event["successor_invocation_started_at"] is None
    assert checkout.exists()
    assert (checkout / "feature.txt").read_text(encoding="utf-8") == (
        "blocked repair edit\n"
    )
    assert (checkout / "partial-repair.txt").read_text(encoding="utf-8") == (
        "unfinished repair\n"
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
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed_run["repair_generation"] == generation
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1
    assert completed_run["development_thread_history"] == ["run-repair-developer-1"]
    assert not checkout.exists()

    # Query the persisted promotion produced by real repair/Git/CLI execution.
    run_id = str(completed["run_id"])
    record = completed_run["acceptance_record"]
    assert record["acceptance_state"] == "integrated"
    assert record["reviewed_candidate_sha"] != record["reviewed_head_sha"]
    for args in (("--plain",), ("--json",)):
        status = invoke_cli_inprocess(git_repo, fixture, "status", run_id, *args)
        assert status.returncode == 0, status.stderr
        assert "验收通过，等待发布" in status.stdout
        assert "尚无有效验收结论" not in status.stdout
    approved = run_cli(git_repo, fixture, "approve", run_id)
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    status = invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--plain")
    assert status.returncode == 0, status.stderr
    assert "当前有效通过" in status.stdout
    assert "尚无有效验收结论" not in status.stdout

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
    queued = run_internal_stage(git_repo, fixture, "approve", run_id)
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
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(recovery_agents),
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
    assert job["phase"] == "waiting_checks"
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
    assert waiting.returncode == 2
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    waiting_state = load_only_run_state(git_repo)
    grant = waiting_state["run_publication"]["approval_grant"]
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
