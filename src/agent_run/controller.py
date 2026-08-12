from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_run.graph import state_from_graph
from agent_run.git import GitError, GitRepository, Publisher
from agent_run.github import GitHubReadError
from agent_run.models import DeliveryGraph, Repository
from agent_run.scope_changes import reconcile_structure
from agent_run.state import StateStore


class GitHubReader(Protocol):
    def repository(self) -> Repository: ...

    def delivery_graph(self, parent_number: int) -> DeliveryGraph: ...


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
            if state.get("status") == "unsupported_scope_change":
                self.states.save_run(run_id, state)
                return state, True
            if resume_human_blocker:
                _resume_agent_human_blocker(state)
            if new_thread:
                _clear_current_publication_thread(state)
            else:
                _restore_current_publication_thread(state)
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            return state, True

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
            return reconcile_structure(state, projected)
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


def _integer_list(state: dict[str, Any], key: str) -> list[int]:
    value = state.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, int) for item in value
    ):
        raise ValueError(f"run state field {key!r} must contain integers")
    return list(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resume_agent_human_blocker(state: dict[str, Any]) -> None:
    """Re-enter exactly one top-level Agent phase after an explicit resume.

    This is deliberately mechanical: Codex supplied the raw blocker text and
    the maintainer chose to resume.  The controller neither interprets the
    condition nor declares it fixed.
    """
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        for job in ticket_jobs.values():
            if _resume_change_job(state, job, ticket=True):
                return
    parent = state.get("parent_job")
    if _resume_change_job(state, parent, ticket=False):
        return
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return
    repair = acceptance.get("repair_job")
    if _resume_change_job(state, repair, ticket=False):
        acceptance["phase"] = "repairing"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )
        return
    if (
        acceptance.get("phase") == "ready_for_human"
        and acceptance.get("blocked_reason") in {
            "agent_requires_human",
            "reviewer_requires_human",
        }
    ):
        blockers = _human_blockers(acceptance)
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
        return
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
        state.update(
            {
                "status": "run_publication_pending",
                "terminal_kind": "run_publication_pending",
                "diagnostics": [],
            }
        )


def _clear_current_publication_thread(state: dict[str, Any]) -> None:
    invocation = state.get("active_agent_invocation")
    if not isinstance(invocation, dict) or invocation.get("role") not in {
        "publication",
        "final_publication",
    }:
        raise ValueError("--new-thread requires a current Publication Invocation")
    job, mirror = _publication_job_for_invocation(state, invocation)
    if invocation.get("role") == "final_publication":
        job.pop("thread_id", None)
    else:
        job.pop("publication_thread_id", None)
    job["publication_new_thread"] = True
    if mirror is not None:
        mirror.pop("publication_thread_id", None)
        mirror["publication_new_thread"] = True


def _restore_current_publication_thread(state: dict[str, Any]) -> None:
    invocation = state.get("active_agent_invocation")
    if (
        not isinstance(invocation, dict)
        or invocation.get("status") != "failed"
        or invocation.get("role") not in {"publication", "final_publication"}
    ):
        return
    job, mirror = _publication_job_for_invocation(state, invocation)
    thread_id = invocation.get("reported_thread_id") or invocation.get(
        "requested_thread_id"
    )
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    if invocation.get("role") == "final_publication":
        job["thread_id"] = thread_id
    else:
        job["publication_thread_id"] = thread_id
    job.pop("publication_new_thread", None)
    if mirror is not None:
        mirror["publication_thread_id"] = thread_id
        mirror.pop("publication_new_thread", None)


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
            acceptance.get("validation_attempts")
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
        _require_invocation_generation(generation, 1)
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


def _require_invocation_generation(invocation: int, current: object) -> None:
    if type(current) is not int or invocation != current:
        raise ValueError("Publication Invocation generation is stale")


def _resume_change_job(
    state: dict[str, Any], value: object, *, ticket: bool
) -> bool:
    if not isinstance(value, dict):
        return False
    if (
        value.get("phase") != "blocked"
        or value.get("blocked_reason")
        not in {"agent_requires_human", "reviewer_requires_human"}
    ):
        return False
    value.update(
        {
            "phase": str(value.get("human_blocker_phase", "developing")),
            "prior_human_blockers": _human_blockers(value),
        }
    )
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
