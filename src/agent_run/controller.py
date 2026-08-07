from __future__ import annotations

from copy import deepcopy
import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_run.graph import state_from_graph
from agent_run.git import GitError, GitRepository, Publisher
from agent_run.github import GitHubReadError
from agent_run.models import DeliveryGraph, Repository
from agent_run.scope_changes import ScopeImpactAssessor, reconcile_structure
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
        *,
        scope_assessor: ScopeImpactAssessor | None = None,
    ) -> None:
        self.github = github
        self.states = states
        self.checkout = git.root
        self.scope_assessor = scope_assessor
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

    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]:
        with self.states.locked():
            existing = self._load_bound_run(run_id)
            if existing.get("status") == "abandoned":
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
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            return state, True

    def confirm_structure(
        self, run_id: str
    ) -> tuple[dict[str, Any], bool]:
        with self.states.locked():
            existing = self._load_bound_run(run_id)
            if existing.get("status") == "abandoned":
                return existing, True
            before_confirmation = deepcopy(existing)
            pending = _state_mapping(existing, "pending_structure_change")
            proposed = pending.get("proposed_revision")
            if not isinstance(proposed, str):
                raise ValueError("pending structure revision is invalid")
            kind = pending.get("kind", "ticket_graph")
            if kind == "ticket_graph":
                snapshot = _state_mapping(
                    pending, "proposed_ticket_graph"
                )
                if snapshot.get("revision") != proposed:
                    raise ValueError(
                        "pending Ticket Graph snapshot does not match revision"
                    )
                existing["accepted_ticket_graph_revision"] = proposed
                existing["ticket_graph"] = deepcopy(snapshot)
            elif kind == "parent_spec":
                snapshot = _state_mapping(pending, "proposed_parent")
                if snapshot.get("revision") != proposed:
                    raise ValueError(
                        "pending Parent snapshot does not match revision"
                    )
                existing["accepted_parent_spec_revision"] = proposed
                existing["parent"] = deepcopy(snapshot)
            else:
                raise ValueError("pending structure kind is invalid")
            parent = _state_mapping(existing, "parent")
            state = self._refresh(existing, int(parent["number"]))
            self._ensure_delivery_branch(state, str(_state_mapping(state, "base")["sha"]))
            if kind == "ticket_graph":
                self._reconcile_ticket_retirements(
                    before_confirmation,
                    snapshot,
                    state,
                )
            self.states.save_run(run_id, state)
            return state, True

    def record_execution_failure(
        self, run_id: str, message: str
    ) -> bool:
        with self.states.locked():
            state = self.states.load_run(run_id)
            if state is None:
                return False
            if state.get("status") in {"abandoned", "parent_closeout_pending"}:
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

    def _reconcile_ticket_retirements(
        self,
        previous: dict[str, Any],
        proposed_graph: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        retirements = self._ticket_retirement_candidates(
            previous, proposed_graph
        )
        live_graph = _current_ticket_graph(state)
        live_numbers = set(
            _integer_list(live_graph, "ordered_ticket_numbers")
        )
        retirements = {
            key: value
            for key, value in retirements.items()
            if int(key) not in live_numbers
        }
        pending = state.get("pending_structure_change")
        graph_confirmation_pending = (
            state.get("status") == "structure_change_pending"
            and isinstance(pending, dict)
            and pending.get("kind", "ticket_graph") == "ticket_graph"
        )
        if graph_confirmation_pending:
            state["pending_ticket_retirements"] = retirements
            return

        retired = state.setdefault("retired_ticket_generations", {})
        if not isinstance(retired, dict):
            raise ValueError("retired_ticket_generations must be an object")
        for key, retirement in sorted(retirements.items()):
            number = int(key)
            branch = retirement["branch"]
            generation = retirement["generation"]
            checkout = (
                self.states.root
                / "worktrees"
                / str(state["run_id"])
                / f"ticket-{number}"
            )
            self.publisher.retire_ticket_branch(branch, checkout)
            retired[key] = generation
        state.pop("pending_ticket_retirements", None)

    @staticmethod
    def _ticket_retirement_candidates(
        previous: dict[str, Any],
        proposed_graph: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        existing = previous.get("pending_ticket_retirements", {})
        if not isinstance(existing, dict):
            raise ValueError("pending_ticket_retirements must be an object")
        retirements: dict[str, dict[str, Any]] = {}
        for key, value in existing.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, dict)
                or not isinstance(value.get("branch"), str)
                or not isinstance(value.get("generation"), int)
            ):
                raise ValueError(
                    "pending_ticket_retirements entries are invalid"
                )
            retirements[key] = dict(value)

        accepted_graph = _state_mapping(previous, "ticket_graph")
        accepted_numbers = set(
            _integer_list(accepted_graph, "ordered_ticket_numbers")
        )
        proposed_numbers = set(
            _integer_list(proposed_graph, "ordered_ticket_numbers")
        )
        jobs = previous.get("ticket_jobs")
        active = previous.get("active_ticket_job")
        for number in sorted(accepted_numbers - proposed_numbers):
            job = (
                jobs.get(str(number))
                if isinstance(jobs, dict)
                else None
            )
            if (
                not isinstance(job, dict)
                and isinstance(active, dict)
                and active.get("ticket_number") == number
            ):
                job = active
            if not isinstance(job, dict):
                continue
            branch = job.get("ticket_branch")
            if not isinstance(branch, str) or not branch:
                continue
            generation = job.get("ticket_branch_generation", 1)
            retirements[str(number)] = {
                "branch": branch,
                "generation": (
                    generation if isinstance(generation, int) else 1
                ),
            }
        return retirements

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
            return reconcile_structure(
                state,
                projected,
                assessor=self.scope_assessor,
                checkout=self.checkout,
            )
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
        if state.get("status") in {"execution_failed", "abandoned", "completed"}:
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
            if state.get("status") == "abandoned":
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


def _current_ticket_graph(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_structure_change")
    if (
        state.get("status") == "structure_change_pending"
        and isinstance(pending, dict)
        and pending.get("kind", "ticket_graph") == "ticket_graph"
    ):
        return _state_mapping(pending, "proposed_ticket_graph")
    return _state_mapping(state, "ticket_graph")


def _now() -> str:
    return datetime.now(UTC).isoformat()
