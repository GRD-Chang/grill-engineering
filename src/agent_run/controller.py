from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_run.graph import state_from_graph
from agent_run.human_responses import append_human_response
from agent_run.git import GitError, GitRepository, Publisher
from agent_run.github import GitHubReadError
from agent_run.models import DeliveryGraph, Repository
from agent_run.requeue import RequeueError, current_change_job, requeue_change_job
from agent_run.revisions import effective_revision
from agent_run.run_currentness import (
    ticket_completion_records,
    ticket_completion_records_fingerprint,
)
from agent_run.scope_changes import reconcile_structure
from agent_run.state import StateStore


class GitHubReader(Protocol):
    def repository(self) -> Repository: ...

    def delivery_graph(self, parent_number: int) -> DeliveryGraph: ...

    def live_pull_request(self, pr_number: int) -> dict[str, Any]: ...


class Controller:
    def __init__(
        self,
        github: GitHubReader,
        git: GitRepository,
        states: StateStore,
    ) -> None:
        self.github = github
        self.states = states
        self.checkout = git.root
        self.publisher = Publisher(git)

    def start(
        self, parent_number: int, *, reuse_existing: bool = True
    ) -> tuple[dict[str, Any], bool]:
        repository = self.github.repository()
        with self.states.locked():
            existing = (
                self.states.find_run(repository.name_with_owner, parent_number)
                if reuse_existing
                else None
            )
            return self._start_locked(repository, parent_number, existing)

    def start_or_resume_unfinished(
        self, parent_number: int
    ) -> tuple[dict[str, Any], bool]:
        """Atomically select the one live Run for the foreground `run` command."""
        repository = self.github.repository()
        with self.states.locked():
            unfinished = self.states.find_unfinished_runs(
                repository.name_with_owner, parent_number
            )
            if len(unfinished) > 1:
                run_ids = ", ".join(str(state["run_id"]) for state in unfinished)
                raise ValueError(
                    "multiple unfinished Delivery Runs exist for this Parent Issue: "
                    f"{run_ids}"
                )
            existing = unfinished[0] if unfinished else None
            return self._start_locked(repository, parent_number, existing)

    def unfinished_runs(self, parent_number: int) -> list[dict[str, Any]]:
        repository = self.github.repository()
        return self.states.find_unfinished_runs(
            repository.name_with_owner, parent_number
        )

    def resume(
        self,
        run_id: str,
        *,
        resume_human_blocker: bool = False,
        new_thread: bool = False,
        human_response: str | None = None,
        message: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.states.locked():
            existing = self._load_bound_run(run_id)
            if existing.get("status") in {
                "abandoned",
                "abandonment_pending",
                "completed",
            }:
                return existing, True
            parent = _state_mapping(existing, "parent")
            parent_number = int(parent["number"])
            if existing.get("base_resolution_pending") is True:
                return self._start_locked(
                    self.github.repository(), parent_number, existing
                )
            base = _state_mapping(existing, "base")
            base_sha = str(base["sha"])
            state = self._refresh(existing, parent_number)
            if state.get("status") == "unsupported_scope_change" or (
                state.get("status") == "execution_failed"
                and (
                    existing.get("status") != "execution_failed"
                    or state.get("diagnostics") != existing.get("diagnostics")
                )
            ):
                self.states.save_run(run_id, state)
                return state, True
            if state.get("status") == "requeue_required":
                self.states.save_run(run_id, state)
                return state, True
            invocation = state.get("active_agent_invocation")
            if (
                isinstance(invocation, dict)
                and invocation.get("role") in {"reviewer", "final_publication"}
                and not self._run_invocation_boundary_is_current(state, invocation)
            ):
                _invalidate_stale_run_invocation(state, invocation)
                self._ensure_delivery_branch(state, base_sha)
                self.states.save_run(run_id, state)
                return state, True
            if message is not None:
                if human_response is not None:
                    raise ValueError("pass only one Human Blocker response")
                human_response = message
            if human_response is not None:
                human_response = _validated_human_response(human_response)
            resuming_run_acceptance = False
            if resume_human_blocker:
                resuming_run_acceptance = _run_acceptance_human_blocker(state)
                resumed_subject = _resume_agent_human_blocker(state, human_response)
                if human_response is not None and not resumed_subject:
                    raise ValueError("--message requires a current Human Blocker")
            elif human_response is not None:
                raise ValueError("--message requires Human Blocker resume")
            if new_thread:
                if resuming_run_acceptance:
                    _state_mapping(state, "run_acceptance")["review_new_thread"] = True
                else:
                    _clear_current_invocation_thread(state)
            else:
                _restore_current_invocation_thread(state)
            _mark_failed_invocation_resuming(state)
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            return state, True

    def requeue(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create a blank Change Job Generation from facts read *now*.

        Requeue is intentionally separate from ``resume``: it never tries to
        attach an old Thread or retain candidate/acceptance state.
        """
        with self.states.locked():
            existing = self._load_bound_run(run_id)
            parent_number = int(_state_mapping(existing, "parent")["number"])
            transition = existing.get("requeue_transition")
            if isinstance(transition, dict):
                retired = transition.get("retired")
                if existing.get("status") == "requeue_required" and isinstance(retired, dict):
                    return existing, retired
            state = self._refresh(existing, parent_number)
            if state.get("status") != "requeue_required":
                self.states.save_run(run_id, state)
                raise RequeueError("requeue is only allowed in requeue_required state")
            repository = self.github.repository()
            base = _state_mapping(state, "base")
            base["sha"] = self.publisher.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            transition_state = dict(state)
            retired = requeue_change_job(transition_state)
            state["requeue_transition"] = {
                "retired": retired,
                "base_sha": base["sha"],
            }
            self.states.save_run(run_id, state)
            return state, retired

    def finalize_requeue(self, run_id: str) -> dict[str, Any]:
        """Commit a prepared replacement only after old assets are retired."""
        with self.states.locked():
            state = self._load_bound_run(run_id)
            transition = _state_mapping(state, "requeue_transition")
            retired = _state_mapping(transition, "retired")
            base_sha = transition.get("base_sha")
            if not isinstance(base_sha, str):
                raise RequeueError("prepared requeue base is invalid")
            _state_mapping(state, "base")["sha"] = base_sha
            applied = requeue_change_job(state)
            if applied != retired:
                raise RequeueError("prepared requeue no longer matches current Job")
            if retired["work_subject"].startswith("parent-only:"):
                generation = int(retired["generation"]) + 1
                state["parent_branch"] = f"agent-run/{run_id}/parent-generation-{generation}"
            parent_number = int(_state_mapping(state, "parent")["number"])
            state.pop("requeue_transition", None)
            state = self._refresh(state, parent_number)
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            return state

    def _run_invocation_boundary_is_current(
        self, state: dict[str, Any], invocation: dict[str, Any]
    ) -> bool:
        boundary = invocation.get("currentness_boundary")
        if not isinstance(boundary, dict):
            # Invocations recorded before the Run boundary was introduced retain
            # their pre-existing resume behavior. Every #45 Run invocation writes
            # a boundary through invocation_event_recorder.
            return True
        parent = _state_mapping(state, "parent")
        graph = _state_mapping(state, "ticket_graph")
        run_branch = state.get("run_branch")
        repository = self.github.repository()
        if not isinstance(run_branch, str):
            return False
        return (
            boundary.get("reviewed_head_sha") == self.publisher.git.resolve(run_branch)
            and boundary.get("reviewed_default_base_sha")
            == self.publisher.git.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            and boundary.get("parent_revision") == parent.get("revision")
            and boundary.get("ticket_graph_revision") == graph.get("revision")
            and boundary.get("ticket_completion_records_fingerprint")
            == ticket_completion_records_fingerprint(state)
            and boundary.get("expected_merge_tree")
            == self.publisher.git.expected_merge_tree(
                default_head_sha=str(boundary["reviewed_default_base_sha"]),
                run_head_sha=str(boundary["reviewed_head_sha"]),
            )
        )

    def record_execution_failure(
        self, run_id: str, message: str
    ) -> bool:
        with self.states.locked():
            state = self.states.load_run(run_id)
            if state is None:
                return False
            if state.get("status") in {
                "abandoned",
                "abandonment_pending",
                "completed",
                "parent_closeout_pending",
            }:
                return False
            hint_reader = getattr(self.github, "repository_hint", None)
            repository_hint = (
                hint_reader() if callable(hint_reader) else None
            )
            if (
                isinstance(repository_hint, str)
                and repository_hint
                and state.get("repository") != repository_hint
            ):
                return False
            try:
                repository = self.github.repository()
            except (GitHubReadError, OSError, ValueError):
                repository = None
            if (
                repository is not None
                and state.get("repository") != repository.name_with_owner
            ):
                return False
            state.update(
                {
                    "status": "execution_failed",
                    "terminal_kind": "execution_failed",
                    "diagnostics": [
                        {
                            "code": "command_failed",
                            "message": message,
                        }
                    ],
                    "updated_at": _now(),
                }
            )
            self.states.save_run(run_id, state)
            return True

    def _load_bound_run(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        repository = self.github.repository()
        if state.get("repository") != repository.name_with_owner:
            raise ValueError(
                "configured GitHub repository does not match the Delivery Run"
            )
        return state

    def _refresh(
        self, state: dict[str, Any], parent_number: int
    ) -> dict[str, Any]:
        try:
            graph = self.github.delivery_graph(parent_number)
            projected = state_from_graph(state, graph)
            refreshed = reconcile_structure(state, projected)
            self._mark_stale_change_job(refreshed)
            return refreshed
        except GitHubReadError as error:
            failed = dict(state)
            failed.update(
                {
                    "status": "execution_failed",
                    "terminal_kind": "execution_failed",
                    "frontier": [],
                    "active_ticket_job": None,
                    "diagnostics": [
                        {"code": error.code, "message": error.message}
                    ],
                    "updated_at": _now(),
                }
            )
            return failed

    def _mark_stale_change_job(self, state: dict[str, Any]) -> None:
        """Route only mechanically provable Change Job drift to Requeue."""
        if state.get("status") in {
            "unsupported_scope_change",
            "abandoned",
            "abandonment_pending",
            "completed",
            "requeue_required",
        }:
            return
        subject, job, _ = current_change_job(state)
        if job is None or job.get("phase") in {
            "completed",
            "merged",
            "merging",
        }:
            return
        if not any(
            type(job.get(key)) is int
            for key in (
                "ticket_branch_generation",
                "parent_generation",
                "repair_generation",
            )
        ):
            # A graph-selected Ticket is only a frontier projection.  It
            # becomes a Change Job once its normal constructor has bound the
            # first generation and currentness facts.
            return
        external = (
            None
            if job.get("blocked_reason") == "merged_revision_mismatch"
            else _unknown_pr_mutation(
                state, subject, job, self.github, self.publisher.git
            )
        )
        if external is not None:
            state.update(
                {
                    "status": "blocked",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [{"code": external, "message": "Change PR changed outside the current Generation"}],
                }
            )
            return
        if _candidate_or_acceptance_is_inconsistent(job):
            state.update(
                {
                    "status": "blocked",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [{"code": "candidate_or_acceptance_inconsistent", "message": "Candidate or Acceptance cannot be safely requeued"}],
                }
            )
            return
        reason = _stale_change_job_reason(state, subject, job, self.publisher.git)
        if reason is None:
            return
        state.update(
            {
                "status": "requeue_required",
                "terminal_kind": "requeue_required",
                "diagnostics": [{"code": reason, "message": "Change Job Generation is stale; run requeue"}],
                "requeue_required": {
                    "work_subject": subject,
                    "generation": _subject_generation(job),
                    "reason": reason,
                },
            }
        )

    def _initial_state(
        self,
        repository: Repository,
        parent_number: int,
        run_id: str,
        base_sha: str,
    ) -> dict[str, Any]:
        now = _now()
        return {
            "schema_version": 1,
            "run_id": run_id,
            "repository": repository.name_with_owner,
            "parent": {"number": parent_number, "title": None, "revision": None},
            "base": {"branch": repository.default_branch, "sha": base_sha},
            "ticket_graph": {
                "revision": None,
                "ordered_ticket_numbers": [],
                "tickets": {},
            },
            "frontier": [],
            "active_ticket_job": None,
            "ticket_jobs": {},
            "retired_ticket_generations": {},
            "pending_ticket_retirements": {},
            "status": "starting",
            "diagnostics": [],
            "created_at": now,
            "updated_at": now,
        }

    def _ensure_delivery_branch(
        self, state: dict[str, Any], base_sha: str
    ) -> None:
        if state.get("status") in {
            "execution_failed",
            "unsupported_scope_change",
            "abandoned",
            "completed",
        }:
            return
        graph = _state_mapping(state, "ticket_graph")
        ordered = _integer_list(graph, "ordered_ticket_numbers")
        if not ordered:
            state["delivery_type"] = "parent_only"
            branch = state.setdefault(
                "parent_branch", f"agent-run/{state['run_id']}/parent"
            )
        else:
            state["delivery_type"] = "ticket_run"
            branch = state.setdefault("run_branch", f"agent-run/{state['run_id']}/run")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Delivery Run branch is invalid")
        self.publisher.ensure_run_branch(branch, base_sha)

    def _start_locked(
        self,
        repository: Repository,
        parent_number: int,
        existing: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        resumed = existing is not None
        if existing is None:
            identity_sha = repository.default_head_sha or (
                f"unresolved-{repository.default_branch}"
            )
            run_id = self._available_run_id(
                repository.name_with_owner, parent_number, identity_sha
            )
            state = self._initial_state(repository, parent_number, run_id, identity_sha)
            state["base_resolution_pending"] = True
            # Persist identity before a remote fetch.  A timeout can then be
            # resumed against the same durable Run rather than creating a new
            # branch or Worker identity on the next foreground invocation.
            self.states.save_run(run_id, state)
        else:
            state = existing
            run_id = str(state["run_id"])
            if state.get("status") in {"abandoned", "abandonment_pending"}:
                return state, True
        base = _state_mapping(state, "base")
        base_sha = str(base["sha"])
        if state.get("base_resolution_pending") is True:
            try:
                base_sha = self.publisher.resolve_base(
                    repository.default_branch, repository.default_head_sha
                )
            except GitError as error:
                state.update(
                    {
                        "status": "execution_failed",
                        "terminal_kind": "execution_failed",
                        "diagnostics": [
                            {
                                "code": "base_resolution_failed",
                                "message": str(error),
                            }
                        ],
                        "updated_at": _now(),
                    }
                )
                self.states.save_run(run_id, state)
                return state, resumed
            base["sha"] = base_sha
            state.pop("base_resolution_pending", None)
            state["status"] = "starting"
            state["terminal_kind"] = None
            state["diagnostics"] = []
        state = self._refresh(state, parent_number)
        self._ensure_delivery_branch(state, base_sha)
        self.states.save_run(run_id, state)
        return state, resumed

    def _available_run_id(
        self, repository: str, parent_number: int, base_sha: str
    ) -> str:
        original = _run_id(repository, parent_number, base_sha)
        if self.states.load_run(original) is None:
            return original
        sequence = 2
        while self.states.load_run(f"{original}-{sequence}") is not None:
            sequence += 1
        return f"{original}-{sequence}"

def _run_id(repository: str, parent_number: int, base_sha: str) -> str:
    identity = f"{repository}\0{parent_number}\0{base_sha}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return f"run-{parent_number}-{suffix}"

def _state_mapping(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value


def _stale_change_job_reason(
    state: dict[str, Any], subject: str, job: dict[str, Any], git: GitRepository
) -> str | None:
    parent = _state_mapping(state, "parent")
    graph = _state_mapping(state, "ticket_graph")
    if subject.startswith("ticket:"):
        ticket = _state_mapping(_state_mapping(graph, "tickets"), subject.removeprefix("ticket:"))
        expected = effective_revision(
            ticket_revision=str(ticket["content_revision"]),
            parent_revision=str(parent["revision"]),
            graph_revision=str(graph["revision"]),
        )
        if job.get("effective_revision") != expected:
            return "ticket_requirements_changed"
        run_branch = state.get("run_branch")
        if isinstance(run_branch, str) and job.get("base_sha") != git.resolve(run_branch):
            return "ticket_base_changed"
        return None
    if subject.startswith("parent-only:"):
        if job.get("effective_revision") != parent.get("revision"):
            return "parent_requirements_changed"
        base = _state_mapping(state, "base")
        branch = base.get("branch")
        if not isinstance(branch, str) or job.get("base_sha") != git.resolve(branch):
            return "parent_base_changed"
        return None
    if job.get("parent_revision") != parent.get("revision"):
        return "run_repair_parent_changed"
    if job.get("ticket_graph_revision") != graph.get("revision"):
        return "run_repair_graph_changed"
    if job.get("ticket_completion_records") != ticket_completion_records(state):
        return "run_repair_ticket_completion_changed"
    run_branch = state.get("run_branch")
    if isinstance(run_branch, str) and job.get("base_sha") != git.resolve(run_branch):
        return "run_repair_base_changed"
    return None


def _unknown_pr_mutation(
    state: dict[str, Any],
    subject: str,
    job: dict[str, Any],
    github: GitHubReader,
    git: GitRepository,
) -> str | None:
    pr_number = job.get("pr_number")
    if not isinstance(pr_number, int):
        return None
    live = github.live_pull_request(pr_number)
    if live.get("state") != "OPEN":
        return "change_pr_closed_or_merged_externally"
    if live.get("head_sha") != job.get("publication_sha"):
        return "change_pr_head_changed_externally"
    expected_base_branch = (
        state.get("run_branch")
        if subject.startswith(("ticket:", "run-repair:"))
        else _state_mapping(state, "base").get("branch")
    )
    if not isinstance(expected_base_branch, str):
        return "change_pr_base_unknown"
    if live.get("base_branch") != expected_base_branch:
        return "change_pr_base_changed_externally"
    if live.get("base_sha") != git.resolve(expected_base_branch):
        return "change_pr_base_changed_externally"
    return None


def _candidate_or_acceptance_is_inconsistent(job: dict[str, Any]) -> bool:
    candidate = job.get("candidate_sha")
    record = job.get("acceptance_record")
    if record is None:
        return False
    if not isinstance(record, dict) or not isinstance(candidate, str):
        return True
    return record.get("reviewed_candidate_sha") != candidate


def _integer_list(state: dict[str, Any], key: str) -> list[int]:
    value = state.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int) for item in value
    ):
        raise ValueError(f"run state field {key!r} must contain integers")
    return list(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resume_agent_human_blocker(
    state: dict[str, Any], human_response: str | None = None
) -> bool:
    """Re-enter exactly one top-level Agent phase after an explicit resume.

    This is deliberately mechanical: Codex supplied the raw blocker text and
    the maintainer chose to resume.  The controller neither interprets the
    condition nor declares it fixed.
    """
    if _human_blocker_subject_count(state) > 1:
        raise ValueError(
            "multiple current Human Blockers require an unambiguous resume target"
        )
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        for job in ticket_jobs.values():
            if _resume_change_job(state, job, ticket=True, human_response=human_response):
                return True
    parent = state.get("parent_job")
    if _resume_change_job(state, parent, ticket=False, human_response=human_response):
        return True
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return False
    repair = acceptance.get("repair_job")
    if _resume_change_job(state, repair, ticket=False, human_response=human_response):
        acceptance["phase"] = "repairing"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )
        return True
    if (
        acceptance.get("phase") == "ready_for_human"
        and acceptance.get("blocked_reason") in {
            "agent_requires_human",
            "reviewer_requires_human",
        }
    ):
        blockers = _human_blockers(acceptance)
        append_human_response(
            acceptance,
            blockers,
            human_response,
            generation=int(acceptance.get("acceptance_generation", 1)),
        )
        acceptance.update(
            {
                "phase": str(acceptance.get("human_blocker_phase", "pending")),
                "prior_human_blockers": blockers,
            }
        )
        acceptance.pop("blocked_reason", None)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return True
    publication = state.get("run_publication")
    if (
        isinstance(publication, dict)
        and publication.get("phase") == "ready_for_human"
        and publication.get("human_blockers") is not None
    ):
        publication.update(
            {
                "phase": str(publication.get("human_blocker_phase", "pending")),
                "prior_human_blockers": _human_blockers(publication),
            }
        )
        append_human_response(
            publication,
            _human_blockers(publication),
            human_response,
            generation=_publication_generation(state),
        )
        state.update(
            {
                "status": "run_publication_pending",
                "terminal_kind": "run_publication_pending",
                "diagnostics": [],
            }
        )
        return True
    return False


def _clear_current_invocation_thread(state: dict[str, Any]) -> None:
    invocation = state.get("active_agent_invocation")
    if not isinstance(invocation, dict):
        raise ValueError("--new-thread requires a current Agent Invocation")
    role = invocation.get("role")
    if role == "reviewer":
        run = _run_acceptance_for_invocation(state, invocation)
        run.pop("reviewer_resume_thread_id", None)
        run["reviewer_new_thread"] = True
        run["phase"] = "pending"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return
    if role in {"development", "fresh_acceptance"}:
        job = _change_job_for_invocation(state, invocation)
        if role == "development":
            job.pop("development_thread_id", None)
            job["development_new_thread"] = True
        else:
            job.pop("review_resume_thread_id", None)
            job["review_new_thread"] = True
        return
    if role not in {"publication", "final_publication"}:
        raise ValueError("--new-thread requires a current Agent Invocation")
    job, mirror = _publication_job_for_invocation(state, invocation)
    if invocation.get("role") == "final_publication":
        job.pop("thread_id", None)
        job.pop("publication_failure_resume", None)
    else:
        job.pop("publication_thread_id", None)
    job["publication_new_thread"] = True
    job.pop("publication_failure_resume", None)
    if mirror is not None:
        mirror.pop("publication_thread_id", None)
        mirror["publication_new_thread"] = True
        mirror.pop("publication_failure_resume", None)


def _restore_current_invocation_thread(state: dict[str, Any]) -> None:
    invocation = state.get("active_agent_invocation")
    if (
        not isinstance(invocation, dict)
        or invocation.get("status") != "failed"
        or invocation.get("role") not in {
            "development",
            "fresh_acceptance",
            "publication",
            "final_publication",
            "reviewer",
        }
    ):
        return
    role = invocation.get("role")
    if role == "reviewer":
        run = _run_acceptance_for_invocation(state, invocation)
        thread_id = invocation.get("reported_thread_id") or invocation.get(
            "requested_thread_id"
        )
        if isinstance(thread_id, str) and thread_id.strip():
            run["reviewer_resume_thread_id"] = thread_id
        run.pop("reviewer_new_thread", None)
        run["phase"] = "pending"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return
    if role in {"development", "fresh_acceptance"}:
        job = _change_job_for_invocation(state, invocation)
        thread_id = invocation.get("reported_thread_id") or invocation.get(
            "requested_thread_id"
        )
        if not isinstance(thread_id, str) or not thread_id.strip():
            return
        if role == "development":
            job["development_thread_id"] = thread_id
            job.pop("development_new_thread", None)
            job["development_failure_resume"] = True
        else:
            job["review_resume_thread_id"] = thread_id
            job["review_new_thread"] = False
            job["review_failure_resume"] = True
        return
    job, mirror = _publication_job_for_invocation(state, invocation)
    thread_id = invocation.get("reported_thread_id") or invocation.get(
        "requested_thread_id"
    )
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    if invocation.get("role") == "final_publication":
        job["thread_id"] = thread_id
        job["publication_failure_resume"] = True
    else:
        job["publication_thread_id"] = thread_id
        job.pop("publication_new_thread", None)
        job["publication_failure_resume"] = True
        if mirror is not None:
            mirror["publication_thread_id"] = thread_id
            mirror.pop("publication_new_thread", None)
            mirror["publication_failure_resume"] = True


def _mark_failed_invocation_resuming(state: dict[str, Any]) -> None:
    """Make an explicit resume distinguishable from a still-unacknowledged failure."""

    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, dict) and invocation.get("status") == "failed":
        invocation["status"] = "resuming"


def _run_acceptance_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    run_id = state.get("run_id")
    if invocation.get("work_subject") != f"run-acceptance:{run_id}":
        raise ValueError("Run Acceptance Invocation work_subject is invalid")
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        raise ValueError("current Run Acceptance job is missing")
    generation = invocation.get("generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("current Agent Invocation generation is invalid")
    _require_invocation_generation(generation, run.get("validation_attempts"))
    return run


def _invalidate_stale_run_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> None:
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        raise ValueError("current Run Acceptance job is missing")
    for key in (
        "acceptance_record",
        "acceptance_artifact",
        "reviewed_head_sha",
        "reviewer_resume_thread_id",
        "reviewer_new_thread",
    ):
        run.pop(key, None)
    run["phase"] = "pending"
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        publication["phase"] = "stale"
        publication.pop("thread_id", None)
        publication.pop("publication_new_thread", None)
    state.update(
        {
            "status": "requeue_required",
            "terminal_kind": "requeue_required",
            "diagnostics": [
                {
                    "code": "run_invocation_stale",
                    "message": "Run Invocation boundary changed; requeue is required",
                }
            ],
        }
    )


def _publication_job_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve a Publication job from its persisted, unambiguous subject."""

    subject = invocation.get("work_subject")
    generation = invocation.get("generation")
    run_id = state.get("run_id")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("current Publication Invocation work_subject is missing")
    if type(generation) is not int or generation < 1:
        raise ValueError("current Publication Invocation generation is invalid")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Delivery Run ID is missing")

    role = invocation.get("role")
    if role == "final_publication":
        if subject != f"run-publication:{run_id}":
            raise ValueError("Final Publication Invocation work_subject is invalid")
        publication = state.get("run_publication")
        if not isinstance(publication, dict):
            raise ValueError("current Final Publication job is missing")
        acceptance = state.get("run_acceptance")
        current_generation = (
            acceptance.get("acceptance_generation", 1)
            if isinstance(acceptance, dict)
            else None
        )
        _require_invocation_generation(generation, current_generation)
        return publication, None
    if role != "publication":
        raise ValueError("current Publication Invocation role is invalid")

    if subject.startswith("ticket:"):
        ticket_text = subject.removeprefix("ticket:")
        if not ticket_text.isdigit() or str(int(ticket_text)) != ticket_text:
            raise ValueError("Ticket Publication Invocation work_subject is invalid")
        ticket_number = int(ticket_text)
        jobs = state.get("ticket_jobs")
        job = jobs.get(ticket_text) if isinstance(jobs, dict) else None
        if not isinstance(job, dict) or job.get("ticket_number") != ticket_number:
            raise ValueError("current Ticket Publication job is missing")
        _require_invocation_generation(
            generation, job.get("ticket_branch_generation")
        )
        active = state.get("active_ticket_job")
        mirror = (
            active
            if isinstance(active, dict)
            and active.get("ticket_number") == ticket_number
            and active is not job
            else None
        )
        return job, mirror

    if subject == f"parent-only:{run_id}":
        parent = state.get("parent_job")
        if not isinstance(parent, dict):
            raise ValueError("current Parent-only Publication job is missing")
        _require_invocation_generation(generation, parent.get("parent_generation", 1))
        return parent, None

    if subject == f"run-repair:{run_id}":
        acceptance = state.get("run_acceptance")
        repair = (
            acceptance.get("repair_job")
            if isinstance(acceptance, dict)
            else None
        )
        if not isinstance(repair, dict):
            raise ValueError("current Run Repair Publication job is missing")
        _require_invocation_generation(generation, repair.get("repair_generation"))
        return repair, None

    raise ValueError("Publication Invocation work_subject is invalid")


def _change_job_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a Change Job for Development or Fresh Acceptance resume."""

    subject = invocation.get("work_subject")
    generation = invocation.get("generation")
    run_id = state.get("run_id")
    if not isinstance(subject, str) or type(generation) is not int or generation < 1:
        raise ValueError("current Change Job Invocation identity is invalid")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Delivery Run ID is missing")
    if subject.startswith("ticket:"):
        ticket_text = subject.removeprefix("ticket:")
        if not ticket_text.isdigit() or str(int(ticket_text)) != ticket_text:
            raise ValueError("current Ticket Change Job Invocation work_subject is invalid")
        jobs = state.get("ticket_jobs")
        job = jobs.get(ticket_text) if isinstance(jobs, dict) else None
        if not isinstance(job, dict) or job.get("ticket_number") != int(ticket_text):
            raise ValueError("current Ticket Change Job is missing")
        _require_invocation_generation(generation, job.get("ticket_branch_generation"))
        return job
    if subject == f"parent-only:{run_id}":
        job = state.get("parent_job")
        if not isinstance(job, dict):
            raise ValueError("current Parent-only Change Job is missing")
        _require_invocation_generation(generation, job.get("parent_generation", 1))
        return job
    if subject == f"run-repair:{run_id}":
        acceptance = state.get("run_acceptance")
        job = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if not isinstance(job, dict):
            raise ValueError("current Run Repair Change Job is missing")
        _require_invocation_generation(generation, job.get("repair_generation"))
        return job
    raise ValueError("Change Job Invocation work_subject is invalid")


def _require_invocation_generation(invocation: int, current: object) -> None:
    if type(current) is not int or invocation != current:
        raise ValueError("Publication Invocation generation is stale")


def _resume_change_job(
    state: dict[str, Any], value: object, *, ticket: bool, human_response: str | None
) -> bool:
    if not isinstance(value, dict):
        return False
    if (
        value.get("phase") != "blocked"
        or value.get("blocked_reason")
        not in {"agent_requires_human", "reviewer_requires_human"}
    ):
        return False
    blockers = _human_blockers(value)
    reviewer_resume = (
        value.get("blocked_reason") == "reviewer_requires_human"
        and value.get("human_blocker_phase") == "candidate"
    )
    append_human_response(
        value,
        blockers,
        human_response,
        generation=_subject_generation(value),
    )
    value.update(
        {
            "phase": str(value.get("human_blocker_phase", "developing")),
            "prior_human_blockers": blockers,
        }
    )
    if reviewer_resume:
        value["review_human_blocker_resume"] = True
    else:
        value.pop("review_human_blocker_resume", None)
    value.pop("blocked_reason", None)
    if ticket:
        state["active_ticket_job"] = value
        status = "active"
    elif "ticket_number" in value:
        status = "active"
    else:
        status = "parent_delivery_pending"
    state.update({"status": status, "terminal_kind": "waiting_human", "diagnostics": []})
    return True


def _human_blockers(subject: dict[str, Any]) -> list[str]:
    value = subject.get("human_blockers")
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("Agent Human Blocker is missing raw blocker strings")
    return list(value)


def _run_acceptance_human_blocker(state: dict[str, Any]) -> bool:
    acceptance = state.get("run_acceptance")
    return isinstance(acceptance, dict) and (
        acceptance.get("phase") == "ready_for_human"
        and acceptance.get("blocked_reason")
        in {"agent_requires_human", "reviewer_requires_human"}
    )


def _human_blocker_subject_count(state: dict[str, Any]) -> int:
    """Count current top-level Human Blocker subjects without choosing one."""
    subjects: list[dict[str, Any]] = []
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        subjects.extend(job for job in ticket_jobs.values() if isinstance(job, dict))
    for key in ("parent_job", "run_acceptance", "run_publication"):
        value = state.get(key)
        if isinstance(value, dict):
            subjects.append(value)
            if key == "run_acceptance":
                repair = value.get("repair_job")
                if isinstance(repair, dict):
                    subjects.append(repair)
    return sum(1 for subject in subjects if _is_human_blocker(subject))


def _is_human_blocker(subject: dict[str, Any]) -> bool:
    return subject.get("phase") in {"blocked", "ready_for_human"} and (
        subject.get("blocked_reason")
        in {"agent_requires_human", "reviewer_requires_human"}
        or isinstance(subject.get("human_blockers"), list)
    )


def _validated_human_response(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("--message must be non-empty after trimming")
    if len(normalized.encode("utf-8")) > 8192:
        raise ValueError("--message must be at most 8 KiB")
    return normalized


def _subject_generation(subject: dict[str, Any]) -> int:
    for key in (
        "ticket_branch_generation",
        "repair_generation",
        "parent_generation",
    ):
        value = subject.get(key)
        if isinstance(value, int):
            return value
    return 1


def _publication_generation(state: dict[str, Any]) -> int:
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        current = publication.get("human_response_generation")
        if isinstance(current, int):
            return current
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        generation = acceptance.get("acceptance_generation")
        if isinstance(generation, int):
            return generation
    return 1
