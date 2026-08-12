from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.change_currentness import stale_change_job_reason, unknown_pr_mutation
from agent_run.controller import Controller
from agent_run.cli_surface import _command_is_ready
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.requeue import RequeueError, requeue_change_job
from agent_run.requeue import close_superseded_pull_request, remove_superseded_worktree
from agent_run.state import StateStore
from conftest import write_fixture
from test_cli import issue, run_cli, stdout_json


def _ticket_state() -> dict[str, Any]:
    job = {
        "ticket_number": 7,
        "ticket_branch": "agent-run/run-1/ticket-7",
        "ticket_branch_generation": 1,
        "phase": "blocked",
        "effective_revision": "old-revision",
        "base_sha": "old-base",
        "candidate_sha": "candidate",
        "acceptance_record": {"artifact": {"verdict": "pass"}},
        "pr_number": 12,
        "development_thread_id": "thread-old",
        "development_thread_history": ["thread-old", "development-earlier"],
        "reviewer_thread_ids": ["reviewer-old"],
    }
    return {
        "run_id": "run-1",
        "status": "requeue_required",
        "terminal_kind": "requeue_required",
        "active_ticket_job": job,
        "ticket_jobs": {"7": job},
        "retired_ticket_generations": {},
        "diagnostics": [{"code": "ticket_requirements_changed"}],
    }


def test_requeue_ticket_archives_the_old_generation_and_releases_a_fresh_job() -> None:
    state = _ticket_state()

    retired = requeue_change_job(state)

    assert retired["generation"] == 1
    assert retired["work_subject"] == "ticket:7"
    assert retired["pr_number"] == 12
    assert retired["thread_ids"] == [
        "development-earlier",
        "reviewer-old",
        "thread-old",
    ]
    assert retired["had_acceptance"] is True
    assert "job" not in retired
    assert state["retired_job_generations"] == [retired]
    assert state["retired_ticket_generations"] == {"7": 1}
    assert state["active_ticket_job"] is None
    assert state["ticket_jobs"] == {}
    assert state["status"] == "active"
    assert state["terminal_kind"] is None
    assert state["diagnostics"] == []


@pytest.mark.parametrize("status", ["active", "execution_failed", "completed"])
def test_requeue_rejects_any_state_except_requeue_required(status: str) -> None:
    state = _ticket_state()
    state["status"] = status

    with pytest.raises(RequeueError, match="only allowed"):
        requeue_change_job(state)


def test_requeue_refuses_ambiguous_change_job_identity() -> None:
    state = _ticket_state()
    state["parent_job"] = deepcopy(state["active_ticket_job"])

    with pytest.raises(RequeueError, match="exactly one"):
        requeue_change_job(state)


def test_requeue_parent_and_run_repair_create_next_generation_inputs() -> None:
    parent_state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "requeue_required",
        "parent_job": {
            "parent_generation": 2,
            "phase": "developing",
            "parent_branch": "agent-run/run-1/parent-generation-2",
        },
    }
    parent_retired = requeue_change_job(parent_state)
    assert parent_retired["work_subject"] == "parent-only:run-1"
    assert parent_state["retired_parent_generation"] == 2
    assert "parent_job" not in parent_state
    assert parent_state["status"] == "parent_delivery_pending"

    repair = {
        "repair_generation": 3,
        "phase": "developing",
        "repair_branch": "agent-run-repair/run-1/2-generation-3",
        "ticket_completion_records": [{"ticket_number": 7}],
    }
    repair_state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "requeue_required",
        "run_acceptance": {"phase": "repairing", "repair_job": repair},
    }
    repair_retired = requeue_change_job(repair_state)
    assert repair_retired["work_subject"] == "run-repair:run-1"
    assert repair_state["run_acceptance"] == {"phase": "pending"}
    assert repair_state["status"] == "run_acceptance_pending"


def test_only_requeue_is_a_lifecycle_action_at_the_stale_generation_boundary() -> None:
    state: dict[str, object] = {"status": "requeue_required"}

    assert _command_is_ready(state, "requeue") is True
    assert _command_is_ready(state, "deliver") is False
    assert _command_is_ready(state, "accept-run") is False
    assert _command_is_ready(state, "publish-run") is False
    assert _command_is_ready(state, "approve") is False
    assert _command_is_ready(state, "revise") is False


def test_requeue_closes_only_the_old_change_pr_and_removes_its_checkout(
    tmp_path: Path,
) -> None:
    class Publisher:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def abandon_change_pr(self, number: int) -> bool:
            self.closed.append(number)
            return True

        def record_agent_run_status(self, _number: int, _status: dict[str, object]) -> None:
            return None

        def has_supersession_close_receipt(
            self, _number: int, _generation: object, _nonce: object
        ) -> bool:
            return False

        def has_supersession_close_record(
            self, _number: int, _generation: object, _nonce: object
        ) -> bool:
            return False

        def has_supersession_close_intent(
            self, _number: int, _generation: object, _nonce: object
        ) -> bool:
            return False

    class Git:
        def __init__(self) -> None:
            self.removed: list[Path] = []

        def remove_worktree(self, checkout: Path) -> None:
            self.removed.append(checkout)

    checkout = tmp_path / "worktrees" / "run-1" / "ticket-7"
    checkout.mkdir(parents=True)
    publisher = Publisher()
    git = Git()
    retired = {"work_subject": "ticket:7", "pr_number": 12}

    close_superseded_pull_request(publisher, retired, "test-nonce")
    remove_superseded_worktree(git, tmp_path, "run-1", retired)

    assert publisher.closed == [12]
    assert git.removed == [checkout]


def test_controller_requeues_a_ticket_from_the_latest_issue_revision(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "7": {
                "number": 7,
                "title": "Ticket",
                "body": "old requirements",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    git = GitRepository.discover(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    active = state["active_ticket_job"]
    assert isinstance(active, dict)
    active.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{state['run_id']}/ticket-7",
            "phase": "developing",
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": active}
    states.save_run(str(state["run_id"]), state)

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["7"]["body"] = "new requirements"
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    stale, _ = controller.resume(str(state["run_id"]))
    assert stale["status"] == "requeue_required"
    assert stale["requeue_required"]["work_subject"] == "ticket:7"

    prepared, retired = controller.requeue(str(state["run_id"]))

    assert retired["generation"] == 1
    assert retired["work_subject"] == "ticket:7"
    assert prepared["status"] == "requeue_required"
    assert prepared["requeue_transition"]["retired"] == retired
    repeated, repeated_retired = controller.requeue(str(state["run_id"]))
    assert repeated["requeue_transition"]["retired"] == retired
    assert repeated_retired == retired
    queued = controller.finalize_requeue(str(state["run_id"]))
    new_job = queued["active_ticket_job"]
    assert isinstance(new_job, dict)
    assert new_job["ticket_number"] == 7
    assert "effective_revision" not in new_job
    assert "candidate_sha" not in new_job
    assert queued["retired_ticket_generations"] == {"7": 1}


def test_requeue_rechecks_an_externally_closed_pr_before_retiring_it(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    branch = f"agent-run/{run_id}/ticket-7"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_ticket_branch(
        branch=branch, base_branch=str(state["run_branch"]), ticket_number=7
    )
    pr_number = publisher.ensure_ticket_pr(
        branch=branch,
        base_branch=str(state["run_branch"]),
        title="old change",
        body="old change",
        primary_ticket=7,
    )
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": branch,
            "phase": "developing",
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
            "pr_number": pr_number,
            "publication_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    stale = run_cli(git_repo, fixture, "deliver", run_id)
    assert stdout_json(stale)["status"] == "requeue_required"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["pull_requests"][0]["state"] = "CLOSED"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    rejected = run_cli(git_repo, fixture, "requeue", run_id)

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "blocked"
    blocked = states.load_run(run_id)
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["terminal_kind"] == "waiting_human"
    assert blocked["diagnostics"][0]["code"] == "change_pr_closed_or_merged_externally"
    assert "requeue_transition" not in blocked

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_ensure_run_branch_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")
    blocked_start = run_cli(git_repo, fixture, "start", "1")

    assert blocked_start.returncode == 2
    assert stdout_json(blocked_start)["status"] == "blocked"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "crash_after_ensure_run_branch_once"
    ] is True
    blocked_delivery = run_cli(git_repo, fixture, "deliver", run_id)

    assert blocked_delivery.returncode == 2
    assert stdout_json(blocked_delivery)["status"] == "blocked"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "crash_after_ensure_run_branch_once"
    ] is True


def test_requeue_blocks_an_external_close_during_retirement(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    branch = f"agent-run/{run_id}/ticket-7"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_ticket_branch(
        branch=branch, base_branch=str(state["run_branch"]), ticket_number=7
    )
    pr_number = publisher.ensure_ticket_pr(
        branch=branch,
        base_branch=str(state["run_branch"]),
        title="old change",
        body="old change",
        primary_ticket=7,
    )
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": branch,
            "phase": "developing",
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
            "pr_number": pr_number,
            "publication_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    assert stdout_json(run_cli(git_repo, fixture, "deliver", run_id))["status"] == (
        "requeue_required"
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["external_close_before_abandon_change_pr_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "requeue", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "blocked"
    persisted = states.load_run(run_id)
    assert persisted is not None
    assert persisted["terminal_kind"] == "waiting_human"
    assert persisted["diagnostics"][0]["code"] == (
        "change_pr_closed_or_merged_externally"
    )
    assert "requeue_transition" not in persisted
    assert persisted["ticket_jobs"]["7"]["ticket_branch_generation"] == 1
    live = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert live["pull_requests"][0]["state"] == "CLOSED"
    assert live["external_close_before_abandon_change_pr_once"] is False


def test_requeue_blocks_an_external_reopen_after_its_close_receipt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    branch = f"agent-run/{run_id}/ticket-7"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_ticket_branch(branch=branch, base_branch=str(state["run_branch"]), ticket_number=7)
    pr_number = publisher.ensure_ticket_pr(branch=branch, base_branch=str(state["run_branch"]), title="old", body="old", primary_ticket=7)
    job.update({"ticket_branch_generation": 1, "ticket_branch": branch, "phase": "developing", "effective_revision": "stale", "base_sha": git.resolve(str(state["run_branch"])), "pr_number": pr_number, "publication_sha": git.resolve(str(state["run_branch"]))})
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)
    assert stdout_json(run_cli(git_repo, fixture, "deliver", run_id))["status"] == "requeue_required"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["external_reopen_after_supersession_receipt_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "requeue", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "blocked"
    assert states.load_run(run_id)["ticket_jobs"]["7"]["ticket_branch_generation"] == 1


def test_requeue_blocks_when_the_persisted_pr_cannot_be_read(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{run_id}/ticket-7",
            "phase": "developing",
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
            "pr_number": 99,
            "publication_sha": "missing-pr-head",
        }
    )
    state["ticket_jobs"] = {"7": job}
    state["status"] = "requeue_required"
    state["terminal_kind"] = "requeue_required"
    state["requeue_required"] = {
        "work_subject": "ticket:7",
        "generation": 1,
        "reason": "ticket_requirements_changed",
    }
    states.save_run(run_id, state)

    rejected = run_cli(git_repo, fixture, "requeue", run_id)

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "blocked"
    blocked = states.load_run(run_id)
    assert blocked is not None
    assert blocked["terminal_kind"] == "waiting_human"
    assert blocked["diagnostics"][0]["code"] == "change_pr_currentness_unknown"


def test_deliver_cannot_restart_a_stale_generation_without_requeue(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    states = StateStore(git_repo / ".agent-run")
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{run_id}/ticket-7",
            "phase": "developing",
            "effective_revision": "old-revision",
            "base_sha": GitRepository(git_repo).resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    blocked = run_cli(git_repo, fixture, "deliver", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "requeue_required"
    updated = states.load_run(run_id)
    assert updated is not None
    updated_job = updated["active_ticket_job"]
    assert isinstance(updated_job, dict)
    assert updated_job["ticket_branch_generation"] == 1
    assert updated_job["effective_revision"] == "old-revision"
    assert "candidate_sha" not in updated_job


def test_stale_human_blocker_cannot_resume_the_old_generation(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{state['run_id']}/ticket-7",
            "phase": "blocked",
            "blocked_reason": "reviewer_requires_human",
            "human_blocker_phase": "candidate",
            "human_blockers": ["need decision"],
            "effective_revision": "old-revision",
            "base_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(str(state["run_id"]), state)

    resumed, _ = controller.resume(str(state["run_id"]), resume_human_blocker=True)

    assert resumed["status"] == "requeue_required"
    assert resumed["active_ticket_job"]["phase"] == "blocked"


def test_pr_base_or_head_mutation_requires_human_not_requeue(git_repo: Path) -> None:
    class Reader:
        def live_pull_request(self, _number: int) -> dict[str, Any]:
            return {
                "state": "OPEN",
                "head_sha": "expected-head",
                "base_branch": "unexpected-base",
                "base_sha": "unexpected-base-sha",
            }

    git = GitRepository(git_repo)
    state = {
        "base": {"branch": "main"},
        "run_branch": "main",
    }
    job = {"pr_number": 2, "publication_sha": "expected-head"}

    assert unknown_pr_mutation(state, "ticket:7", job, Reader(), git) == (
        "change_pr_base_changed_externally"
    )


def test_run_repair_completion_drift_requires_a_new_generation(
    git_repo: Path,
) -> None:
    git = GitRepository(git_repo)
    state = {
        "parent": {"revision": "parent"},
        "ticket_graph": {"revision": "graph", "tickets": {}},
        "ticket_jobs": {},
        "run_branch": "main",
    }
    job = {
        "parent_revision": "parent",
        "ticket_graph_revision": "graph",
        "ticket_completion_records": [{"ticket_number": 7}],
        "base_sha": git.resolve("main"),
    }

    assert stale_change_job_reason(state, "run-repair:run-1", job, git) == (
        "run_repair_ticket_completion_changed"
    )
