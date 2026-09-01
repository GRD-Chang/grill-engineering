from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.change_currentness import stale_change_job_reason, unknown_pr_mutation
from agent_run.cli_presentation import _next_action
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.cli_surface import _command_is_ready
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.requeue import RequeueError, requeue_change_job
from agent_run.requeue import close_superseded_pull_request, remove_superseded_worktree
from agent_run.revisions import effective_revision
from agent_run.state import StateStore
from conftest import seed_run, write_fixture
from test_cli import issue, run_cli, stdout_json


def _canonical_budget() -> dict[str, Any]:
    return {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }


def test_fixture_ticket_pr_rejects_same_sha_on_wrong_base_branch(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    git = GitRepository(git_repo)
    publisher = FixtureGitHubPublisher(fixture, git)
    base_sha = git.resolve("HEAD")
    publisher.ensure_ticket_branch(
        ticket_number=7,
        branch="agent-run/run-1/ticket-7",
        base_branch="main",
        expected_base_sha=base_sha,
        expected_remote_sha=base_sha,
        recovery_remote_sha=base_sha,
    )
    publisher.ensure_ticket_pr(
        branch="agent-run/run-1/ticket-7",
        base_branch="main",
        title="title",
        body="body",
        primary_ticket=7,
        expected_head_sha=base_sha,
        expected_base_sha=base_sha,
    )
    data = publisher.data
    data["delivery"]["pull_requests"][0]["base_branch"] = "foreign"

    with pytest.raises(ValueError, match="foreign identity"):
        publisher.ensure_ticket_pr(
            branch="agent-run/run-1/ticket-7",
            base_branch="main",
            title="title",
            body="body",
            primary_ticket=7,
            expected_head_sha=base_sha,
            expected_base_sha=base_sha,
        )


def test_fixture_closed_ticket_pr_does_not_block_new_publication(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    git = GitRepository(git_repo)
    publisher = FixtureGitHubPublisher(fixture, git)
    base_sha = git.resolve("HEAD")
    branch = "agent-run/run-1/ticket-7"
    publisher.ensure_ticket_branch(
        ticket_number=7,
        branch=branch,
        base_branch="main",
        expected_base_sha=base_sha,
        expected_remote_sha=base_sha,
        recovery_remote_sha=base_sha,
    )
    publisher.ensure_ticket_pr(
        branch=branch,
        base_branch="main",
        title="old title",
        body="old body",
        primary_ticket=7,
        expected_head_sha=base_sha,
        expected_base_sha=base_sha,
    )
    publisher.data["delivery"]["pull_requests"][0]["state"] = "CLOSED"
    publisher.verify_ticket_pr_before_publish(
        branch=branch,
        base_branch="main",
        expected_head_sha=base_sha,
        expected_base_sha=base_sha,
    )
    publisher.publish_branch(
        branch, "new-publication-sha", expected_remote_sha=base_sha
    )
    publisher.ensure_ticket_pr(
        branch=branch,
        base_branch="main",
        title="new title",
        body="new body",
        primary_ticket=7,
        expected_head_sha="new-publication-sha",
        expected_base_sha=base_sha,
    )

    pulls = publisher.data["delivery"]["pull_requests"]
    assert [(pull["state"], pull["head_sha"]) for pull in pulls] == [
        ("CLOSED", base_sha),
        ("OPEN", "new-publication-sha"),
    ]


def test_ticket_ref_recovery_preserves_a_publish_intent_before_its_push(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    git = GitRepository(git_repo)
    publisher = FixtureGitHubPublisher(fixture, git)
    with states.locked():
        current = states.load_current_run(str(state["run_id"]))
        assert current is not None
        engine = TicketDeliveryEngine(
            git=git, states=states, github=publisher, agents=object()  # type: ignore[arg-type]
        )
        job = engine._job(current)
        base_sha = str(job["base_sha"])
        publisher.ensure_ticket_branch(
            ticket_number=7,
            branch=str(job["ticket_branch"]),
            base_branch=str(current["run_branch"]),
            expected_base_sha=base_sha,
            expected_remote_sha=base_sha,
            recovery_remote_sha=base_sha,
        )
        job["ticket_write_intent"] = {
            "action": "publish_ticket_ref",
            "branch": str(job["ticket_branch"]),
            "expected_remote_sha": base_sha,
            "head_sha": "pending-publication-sha",
        }
        engine._ensure_ticket_branch(current, job)

    assert job["ticket_write_intent"]["expected_remote_sha"] == base_sha
    assert job["ticket_write_intent"]["head_sha"] == "pending-publication-sha"


def _ticket_state() -> dict[str, Any]:
    job = {
        "ticket_number": 7,
        "ticket_branch": "agent-run/run-1/ticket-7",
        "ticket_branch_generation": 1,
        "phase": "blocked",
        "effective_revision": "old-revision",
        "base_sha": "old-base",
        "candidate_sha": "candidate",
        "acceptance_record": {"artifact": {"checks": {}}},
        "pr_number": 12,
        "development_thread_id": "thread-old",
        "development_thread_history": ["thread-old", "development-earlier"],
        "reviewer_thread_ids": ["reviewer-old"],
        "review_budget": _canonical_budget(),
        "review_budget_history": [],
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
    assert state["active_agent_invocation"] is None
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
        },
    }
    parent_retired = requeue_change_job(parent_state)
    assert parent_retired["work_subject"] == "parent-only:run-1"
    assert parent_state["retired_parent_generation"] == 2
    assert "parent_job" not in parent_state
    assert parent_state["active_agent_invocation"] is None
    assert parent_state["status"] == "parent_delivery_pending"

    repair = {
        "repair_generation": 3,
        "phase": "developing",
        "review_budget": _canonical_budget(),
        "review_budget_history": [],
        "repair_branch": "agent-run-repair/run-1/2-generation-3",
        "ticket_completion_records": [{"ticket_number": 7}],
    }
    repair_state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "requeue_required",
        "run_acceptance": {
            "phase": "repairing",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "repair_job": repair,
        },
    }
    repair_retired = requeue_change_job(repair_state)
    assert repair_retired["work_subject"] == "run-repair:run-1"
    assert repair_state["run_acceptance"] == {
        "phase": "pending",
        "review_budget": _canonical_budget(),
        "review_budget_history": [],
    }
    assert repair_state["active_agent_invocation"] is None
    assert repair_state["status"] == "run_acceptance_pending"


def test_only_requeue_is_a_lifecycle_action_at_the_stale_generation_boundary() -> None:
    state: dict[str, object] = {"status": "requeue_required"}

    assert _command_is_ready(state, "requeue") is True
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
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


def test_requeue_waits_for_unparseable_transition_facts_before_closing_old_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
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
    prepared, retired = controller.requeue(str(state["run_id"]))
    assert prepared["requeue_transition"]["retired"] == retired

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {
            "code": "github_invalid_response",
            "message": "requeue graph JSON is incomplete",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    blocked = run_cli(git_repo, fixture, "requeue", str(state["run_id"]))

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "deterministic_contradiction"
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["requeue_transition"]["retired"] == retired
    assert persisted["diagnostics"][0]["code"] == "github_invalid_response"
    assert persisted.get("github_refresh_pending") is None
    assert "agent-run run" not in _next_action(persisted)
    assert json.loads(fixture.read_text(encoding="utf-8")).get("delivery", {}).get(
        "mutations", []
    ) == []


def test_requeue_transition_restores_its_exact_gate_after_graph_read_recovers(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
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
    prepared, retired = controller.requeue(str(state["run_id"]))
    assert prepared["requeue_transition"]["retired"] == retired

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {
            "code": "github_read_failed",
            "message": "repository graph is converging",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    waiting, waiting_retired = controller.requeue(str(state["run_id"]))
    assert waiting["status"] == "waiting_external"
    assert waiting_retired == retired

    recovered, recovered_retired = controller.requeue(str(state["run_id"]))

    assert recovered["status"] == "requeue_required"
    assert recovered_retired == retired
    assert recovered["diagnostics"] == [
        {
            "code": recovered["requeue_required"]["reason"],
            "message": "Change Job Generation is stale; run requeue",
        }
    ]
    assert states.load_run(str(state["run_id"])) == recovered



def test_requeue_rechecks_an_externally_closed_pr_before_retiring_it(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    started = seed_run(git_repo, fixture, "1")
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
        branch=branch, base_branch=str(state["run_branch"]), ticket_number=7,
        expected_base_sha=git.resolve(str(state["run_branch"])),
        expected_remote_sha=git.resolve(str(state["run_branch"])),
        recovery_remote_sha=git.resolve(str(state["run_branch"])),
    )
    pr_number = publisher.ensure_ticket_pr(
        branch=branch,
        base_branch=str(state["run_branch"]),
        title="old change",
        body="old change",
        primary_ticket=7,
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve(str(state["run_branch"])),
    )
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": branch,
            "phase": "developing",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
            "pr_number": pr_number,
            "publication_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    stale = run_cli(git_repo, fixture, "run", "1")
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
    blocked_start = seed_run(git_repo, fixture, "1")

    assert blocked_start.returncode == 2
    assert stdout_json(blocked_start)["status"] == "blocked"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "crash_after_ensure_run_branch_once"
    ] is True
    blocked_delivery = run_cli(git_repo, fixture, "run", "1")

    assert blocked_delivery.returncode == 2
    assert stdout_json(blocked_delivery)["status"] == "blocked"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "crash_after_ensure_run_branch_once"
    ] is True


def test_requeue_blocks_an_external_close_during_retirement(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    branch = f"agent-run/{run_id}/ticket-7"
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.ensure_ticket_branch(
        branch=branch, base_branch=str(state["run_branch"]), ticket_number=7,
        expected_base_sha=git.resolve(str(state["run_branch"])),
        expected_remote_sha=git.resolve(str(state["run_branch"])),
        recovery_remote_sha=git.resolve(str(state["run_branch"])),
    )
    pr_number = publisher.ensure_ticket_pr(
        branch=branch,
        base_branch=str(state["run_branch"]),
        title="old change",
        body="old change",
        primary_ticket=7,
        expected_head_sha=git.resolve(str(state["run_branch"])),
        expected_base_sha=git.resolve(str(state["run_branch"])),
    )
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": branch,
            "phase": "developing",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "effective_revision": "stale",
            "base_sha": git.resolve(str(state["run_branch"])),
            "pr_number": pr_number,
            "publication_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    assert stdout_json(run_cli(git_repo, fixture, "run", "1"))["status"] == (
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    branch = f"agent-run/{run_id}/ticket-7"
    publisher = FixtureGitHubPublisher(fixture, git)
    base_sha = git.resolve(str(state["run_branch"]))
    publisher.ensure_ticket_branch(branch=branch, base_branch=str(state["run_branch"]), ticket_number=7, expected_base_sha=base_sha, expected_remote_sha=base_sha, recovery_remote_sha=base_sha)
    pr_number = publisher.ensure_ticket_pr(branch=branch, base_branch=str(state["run_branch"]), title="old", body="old", primary_ticket=7, expected_head_sha=base_sha, expected_base_sha=base_sha)
    job.update({"ticket_branch_generation": 1, "ticket_branch": branch, "phase": "developing", "review_budget": _canonical_budget(), "review_budget_history": [], "effective_revision": "stale", "base_sha": git.resolve(str(state["run_branch"])), "pr_number": pr_number, "publication_sha": git.resolve(str(state["run_branch"]))})
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)
    assert stdout_json(run_cli(git_repo, fixture, "run", "1"))["status"] == "requeue_required"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["external_reopen_after_supersession_receipt_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "requeue", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "blocked"
    assert states.load_run(run_id)["ticket_jobs"]["7"]["ticket_branch_generation"] == 1


def test_requeue_supervises_an_unreadable_persisted_pr(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"7": issue(7)},
        delivery={"pull_requests": []},
    )
    started = seed_run(git_repo, fixture, "1")
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
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
    state["diagnostics"] = [
        {
            "code": "ticket_requirements_changed",
            "message": "Ticket requirements changed; run requeue",
        }
    ]
    states.save_run(run_id, state)

    waiting = run_cli(git_repo, fixture, "requeue", run_id)

    assert waiting.returncode == 2, waiting.stderr
    output = stdout_json(waiting)
    assert output["status"] == "supervision_timeout"
    assert output["action"]["status"] == "applied"
    persisted = states.load_run(run_id)
    assert persisted is not None
    assert persisted["terminal_kind"] == "supervision_timeout"
    assert persisted["diagnostics"][0]["code"] == "supervision_timeout"
    assert persisted["diagnostics"][0]["last_error"]["code"] == "missing_pull_request"
    receipt = persisted["action_application_receipt"]
    assert receipt["kind"] == "requeue"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery.get("mutations", []) == []


def test_requeue_supervises_a_repository_binding_read_failure(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "effective_revision": "stale",
            "base_sha": GitRepository(git_repo).resolve(str(state["run_branch"])),
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
    state["diagnostics"] = [
        {
            "code": "ticket_requirements_changed",
            "message": "Ticket requirements changed; run requeue",
        }
    ]
    states.save_run(run_id, state)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["supervision_clock_multiplier"] = 1
    data["repository_read_failures"] = [
        {"code": "github_read_failed", "message": "repository unavailable"}
        for _ in range(32)
    ]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "requeue", run_id)

    assert waiting.returncode == 2, waiting.stderr
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    persisted = states.load_run(run_id)
    assert persisted is not None
    assert persisted["github_refresh_pending"] is True
    assert persisted["diagnostics"][0]["code"] == "supervision_timeout"
    assert persisted["diagnostics"][0]["last_error"]["code"] == "github_read_failed"


def test_deliver_cannot_restart_a_stale_generation_without_requeue(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    started = seed_run(git_repo, fixture, "1")
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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "effective_revision": "old-revision",
            "base_sha": GitRepository(git_repo).resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)

    blocked = run_cli(git_repo, fixture, "run", "1")

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
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "blocked_reason": "reviewer_requires_human",
            "human_blocker_phase": "candidate",
            "human_blockers": ["need decision"],
            "effective_revision": "old-revision",
            "base_sha": git.resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"7": job}
    state.update(
        {
            "status": "ready_for_human",
            "terminal_kind": "waiting_human",
        }
    )
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


def test_public_views_keep_currentness_contradiction_bound_to_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"7": issue(7)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    publisher = FixtureGitHubPublisher(fixture, git)
    base_sha = git.resolve(str(state["run_branch"]))
    branch = f"agent-run/{run_id}/ticket-7"
    publisher.ensure_ticket_branch(
        ticket_number=7,
        branch=branch,
        base_branch=str(state["run_branch"]),
        expected_base_sha=base_sha,
        expected_remote_sha=base_sha,
        recovery_remote_sha=base_sha,
    )
    pr_number = publisher.ensure_ticket_pr(
        branch=branch,
        base_branch=str(state["run_branch"]),
        title="Ticket change",
        body="Ticket change",
        primary_ticket=7,
        expected_head_sha=base_sha,
        expected_base_sha=base_sha,
    )
    graph = state["ticket_graph"]
    parent = state["parent"]
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": branch,
            "phase": "developing",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "effective_revision": effective_revision(
                ticket_revision=graph["tickets"]["7"]["content_revision"],
                parent_revision=parent["revision"],
                graph_revision=graph["revision"],
            ),
            "base_sha": base_sha,
            "pr_number": pr_number,
            "publication_sha": base_sha,
        }
    )
    state["ticket_jobs"] = {"7": job}
    states.save_run(run_id, state)
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["pull_requests"][0]["base_branch"] = "foreign"
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "run", "1")

    assert blocked.returncode == 2
    persisted = states.load_run(run_id)
    assert persisted is not None
    assert persisted["status"] == "blocked"
    assert persisted["diagnostics"][0]["operator_gate"] == {
        "work_subject": "ticket:7",
        "action_kind": "deterministic_contradiction",
        "phase": "developing",
        "reason": "change_pr_base_changed_externally",
    }
    for command in ("status", "history"):
        view = run_cli(git_repo, fixture, command, run_id)
        assert view.returncode == 0, view.stderr
        assert "类型: Deterministic Contradiction" in view.stdout
        assert "对象: Ticket #7" in view.stdout
        assert "阶段: developing" in view.stdout
        assert "原因: Change PR changed outside the current Generation" in view.stdout
        assert f"已保留成果: PR #{pr_number}" in view.stdout
        assert (
            "唯一下一步: 修复诊断中的确定性外部矛盾后执行 "
            "agent-run run 1 --repo example/project"
        ) in view.stdout


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
