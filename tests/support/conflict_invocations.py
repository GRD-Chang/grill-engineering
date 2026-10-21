"""Small conflict-call fixture: real Controller, resolver, recorder and StateStore."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.ci_recovery_budget import new_ticket_ci_recovery_budget
from agent_run.controller import Controller
from agent_run.development_review import DevelopmentReview
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_currentness import ticket_completion_records
from agent_run.git import GitRepository
from agent_run.delivery_step import DeliveryStepKind
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_integration import (
    finish_ticket_snapshot, finish_parent_snapshot, finish_repair_snapshot,
)
from agent_run.ticket_work import TicketWorkAdmission
from agent_run.review_budget import new_budget
from agent_run.revisions import effective_revision
from support.run_artifacts import _passing_artifact
from support.run_scenarios import _completed_run
from agent_run.object_state import object_store
from agent_run.state import StateStore
from support.delivery_agents import issue
from support.seeding import write_fixture
from support.state import persist_run_fixture


def prepared_conflict(git_repo: Path, kind: str, phase: str = "accepted", *, state_root: Path | None = None) -> tuple[dict[str, Any], StateStore, dict[str, Any], Any]:
    if kind == "run_repair":
        # A Run Repair belongs to the completed Ticket Run, with its real Run
        # ref and completion facts; an empty graph selects Parent-only delivery.
        state, store, git = _completed_run(git_repo, state_root=state_root)
        fixture = git_repo / "github.json"
        base = git.resolve(str(state["run_branch"]))
    else:
        fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)} if kind == "ticket" else {})
        store = StateStore(state_root or git_repo / ".agent-run")
        git = GitRepository(git_repo)
        state, _ = Controller(FixtureGitHubReader(fixture), git, store).start(1)
        base = git.resolve("HEAD")
    common = dict(phase=phase, base_sha=base, candidate_sha=base,
                  modification_attempts=1, development_thread_id="conflict-thread")
    budgets = dict(
        review_budget=new_budget(development_attempts=1, reviewer_invocations=0),
        review_budget_history=[],
        ci_recovery=new_ticket_ci_recovery_budget() if kind == "ticket" else new_ci_recovery_budget(),
    )
    if kind != "run_repair":
        common.update(budgets)
    if phase == "candidate":
        common["attempt_kind"] = "ci_repair"
    if kind == "ticket":
        job = state["ticket_jobs"]["3"]
        job.update(common)
        job["ticket_branch_generation"] = 1
        # First-generation naming from DeliveryEngine._new_job_fields; this
        # accepted Ticket fixture owns a real local ref at its recorded base.
        job["ticket_branch"] = f"agent-run/{state['run_id']}/ticket-3"
        git.ensure_run_branch(job["ticket_branch"], base)
        job["policy_snapshot"] = dict(state["policy_snapshot"])
        ticket = state["ticket_graph"]["tickets"]["3"]
        job["effective_revision"] = effective_revision(
            ticket_revision=ticket["content_revision"], parent_revision=state["parent"]["revision"],
            graph_revision=state["ticket_graph"]["revision"],
        )
        if phase == "accepted":
            artifact = _passing_artifact()
            job["acceptance_artifact"] = artifact
            job["acceptance_record"] = dict(
                acceptance_scope="change_job", reviewed_base_sha=base,
                reviewed_candidate_sha=base, reviewed_candidate_tree=git.resolve(f"{base}^{{tree}}"),
                effective_revision=job["effective_revision"], reviewer_thread_id="reviewer-thread", artifact=artifact,
            )
            job["review_budget"]["reviewer_invocations"] = 1
            job["review_budget"]["review_artifacts"] = [dict(
                reviewer_thread_id="reviewer-thread", candidate_sha=base, reviewed_base_sha=base,
                review_identity={key: job["acceptance_record"][key] for key in (
                    "reviewed_base_sha", "reviewed_candidate_sha", "reviewed_candidate_tree")}, artifact=artifact,
            )]
        state["active_ticket_job"] = job
        state["ticket_integration_holder"] = 3 if phase == "accepted" else None
        key = "integration_conflict"
    elif kind == "parent_only":
        job = common
        job["parent_generation"] = 1
        job["parent_branch"] = "parent"
        job["effective_revision"] = state["parent"]["revision"]
        job["policy_snapshot"] = dict(state["policy_snapshot"])
        if phase == "candidate":
            job["parent_integration"] = dict(
                candidate_sha=base, candidate_tree=git.resolve(f"{base}^{{tree}}"),
                source_sha=base, target_sha=base, head_sha=base,
                head_tree=git.resolve(f"{base}^{{tree}}"), conflict_paths=[],
            )
        state["parent_job"] = job
        key = "parent_integration_conflict"
    else:
        job = common
        job.update(
            repair_generation=1, repair_attempt=1, repair_branch="run-repair",
            repair_mode="squash", policy_snapshot=dict(state["policy_snapshot"]),
            default_base_sha=state["base"]["sha"],
            parent_revision=state["parent"]["revision"],
            ticket_graph_revision=state["ticket_graph"]["revision"],
            ticket_completion_records=ticket_completion_records(state),
        )
        state["run_acceptance"] = dict(phase="repairing", repair_job=job,
            reviewer_thread_ids=[], development_thread_history=[],
            policy_snapshot=dict(state["policy_snapshot"]),
            **budgets,
            repair_integration_budgets={"1": {"policy_snapshot": dict(state["policy_snapshot"])}})
        key = "run_repair_integration_conflict"
    job[key] = dict(source_sha=base, target_sha=base, candidate_sha=base,
                    stage="source", head_sha=base, merge_head_sha=base, paths=["README.md"], attempts=0,
                    resolved_stages=[], conflict_paths=["README.md"])
    persist_run_fixture(store, str(state["run_id"]), state)
    scoped = object_store(store, parent_scope=kind != "ticket")
    assert scoped.load_current_run(str(state["run_id"])) is not None
    # Only Git effects and remote reads are controlled here. Identity, request
    # construction, invocation recording and role-result consumption belong to
    # the same DevelopmentReview used by delivery callers.
    fixture_git = SimpleNamespace(
        checkout_head=lambda checkout: base,
        commit_parents=lambda head: [base, base],
        commit_integration_work=lambda *args, **kwargs: base,
        is_ancestor=lambda *args: True,
        resolve=git.resolve,
        integrate_remote_heads=lambda *args, **kwargs: SimpleNamespace(status="clean", head_sha=base),
    )
    github = FixtureGitHubPublisher(fixture, git)
    stage: Any = DevelopmentReview(
        git=fixture_git, states=scoped, agents=None, github=github,
        candidate_acceptance=CandidateRunAcceptance(fixture_git),
        repair_currentness=RunRepairCurrentness(fixture_git, github, scoped),
    )
    stage._bind_change(state, job, {"ticket": "ticket", "parent_only": "parent-only", "run_repair": "repair"}[kind])
    # Invocation corruption tests exclude scheduler state: their deliberately
    # invalid durable input must remain readable only by the rejection oracle,
    # not by an unrelated lease-release transaction on leaving the role step.
    if kind == "ticket":
        stage.agent_work.admission = Mock(
            spec=TicketWorkAdmission, acquire=Mock(return_value="conflict-fixture-lease"),
        )
    stage.conflict_key = key
    stage.kind = kind
    stage.save(state)
    return state, store, job, stage


def run_conflict(stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
    scope = {"ticket": "ticket", "parent_only": "parent-only", "run_repair": "repair"}[stage.kind]
    step = stage.develop_integration(state, job, checkout, scope=scope, allow_agent=True)
    if step.kind != DeliveryStepKind.CONTINUE:
        return False
    finish = {
        "ticket": finish_ticket_snapshot,
        "parent_only": finish_parent_snapshot,
        "run_repair": finish_repair_snapshot,
    }[stage.kind]
    result = finish(git=stage.git, github=stage.github, state_store=stage.state_store,
                    state=state, job=job, checkout=checkout)
    return result.status == "clean"
