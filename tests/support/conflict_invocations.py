"""Small conflict-call fixture: real Controller, resolver, recorder and StateStore."""
from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_run.change_delivery_publication_agent import invocation_boundary, invocation_events
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.parent_only_integration import resolve_parent_conflict
from agent_run.review_budget import new_budget
from agent_run.revisions import effective_revision
from support.run_artifacts import _passing_artifact
from agent_run.state import StateStore
from agent_run.ticket_integration import _resolve_conflict
from support.delivery_agents import issue
from support.seeding import write_fixture


def prepared_conflict(git_repo: Path, kind: str, phase: str = "accepted", *, state_root: Path | None = None) -> tuple[dict[str, Any], StateStore, dict[str, Any], Any]:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)} if kind == "ticket" else {})
    store = StateStore(state_root or git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state, _ = Controller(FixtureGitHubReader(fixture), git, store).start(1)
    base = git.resolve("HEAD")
    common = dict(phase=phase, base_sha=base, candidate_sha=base,
                  review_budget=new_budget(development_attempts=1, reviewer_invocations=0),
                  review_budget_history=[], ci_recovery=new_ci_recovery_budget(),
                  modification_attempts=1, development_thread_id="conflict-thread")
    if phase == "candidate":
        common["attempt_kind"] = "ci_repair"
    if kind == "ticket":
        job = state["ticket_jobs"]["3"]
        job.update(common)
        job["ticket_branch_generation"] = 1
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
        state["ticket_integration_holder"] = 3
        subject = "ticket:3"
        key = "integration_conflict"
    elif kind == "parent_only":
        job = common
        job["parent_generation"] = 1
        if phase == "candidate":
            job["parent_integration"] = dict(
                candidate_sha=base, candidate_tree=git.resolve(f"{base}^{{tree}}"),
                source_sha=base, target_sha=base, head_sha=base,
                head_tree=git.resolve(f"{base}^{{tree}}"), conflict_paths=[],
            )
        state["parent_job"] = job
        subject = f"parent-only:{state['run_id']}"
        key = "parent_integration_conflict"
    else:
        job = common
        job.update(repair_generation=1, repair_mode="squash", policy_snapshot=dict(state["policy_snapshot"]))
        state["run_acceptance"] = dict(phase="repairing", repair_job=job,
            policy_snapshot=dict(state["policy_snapshot"]), ci_recovery=new_ci_recovery_budget())
        subject = f"run-repair:{state['run_id']}"
        key = "run_repair_integration_conflict"
    job[key] = dict(source_sha=base, target_sha=base, candidate_sha=base,
                    stage="source", head_sha=base, merge_head_sha=base, paths=["README.md"], attempts=0)
    stage = SimpleNamespace(
        require_agent_work=lambda _role: None,
        release_agent_work=lambda: None,
        adapter=SimpleNamespace(
            invocation_identity=lambda state, job: (subject, 1),
            development_request=lambda state, job, checkout: {"thread_id": job.get("development_thread_id")},
            development_thread_is_allowed=lambda state, thread: True,
        ),
        contract=SimpleNamespace(label="ticket-3" if kind == "ticket" else kind),
        save=lambda current: store.save_run(str(current["run_id"]), current),
        _invocation_boundary=invocation_boundary,
        _agent_is_current=lambda state, job: True,
        _reject_stale=lambda *args: None,
        git=SimpleNamespace(checkout_head=lambda checkout: base, commit_parents=lambda head: [base, base]),
    )
    stage._invocation_events = partial(invocation_events, stage)
    stage.conflict_key = key
    stage.kind = kind
    stage.save(state)
    return state, store, job, stage


def run_conflict(stage: Any, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
    if stage.kind == "ticket":
        return _resolve_conflict(stage, state, job, checkout)
    return resolve_parent_conflict(stage, state, job, checkout, record_key=stage.conflict_key.removesuffix("_conflict"))
