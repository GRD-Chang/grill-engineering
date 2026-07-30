from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_run.graph import state_from_graph
from agent_run.git import GitRepository, Publisher
from agent_run.github import GitHubReadError
from agent_run.models import DeliveryGraph, Repository
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
        self.publisher = Publisher(git)

    def start(self, parent_number: int) -> tuple[dict[str, Any], bool]:
        repository = self.github.repository()
        with self.states.locked():
            existing = self.states.find_run(repository.name_with_owner, parent_number)
            resumed = existing is not None
            if existing is None:
                base_sha = self.publisher.resolve_base(
                    repository.default_branch, repository.default_head_sha
                )
                run_id = _run_id(
                    repository.name_with_owner, parent_number, base_sha
                )
                state = self._initial_state(
                    repository, parent_number, run_id, base_sha
                )
                # 先持久化稳定身份；即使此处中断，也不会遗留无状态的分支。
                self.states.save_run(run_id, state)
            else:
                state = existing
                run_id = str(state["run_id"])
                base = _state_mapping(state, "base")
                base_sha = str(base["sha"])
            branch = str(state["run_branch"])
            self.publisher.ensure_run_branch(branch, base_sha)
            state = self._refresh(state, parent_number)
            self.states.save_run(run_id, state)
            return state, resumed

    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]:
        with self.states.locked():
            existing = self.states.load_run(run_id)
            if existing is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            repository = self.github.repository()
            if existing.get("repository") != repository.name_with_owner:
                raise ValueError(
                    "configured GitHub repository does not match the Delivery Run"
                )
            parent = _state_mapping(existing, "parent")
            parent_number = int(parent["number"])
            base = _state_mapping(existing, "base")
            base_sha = str(base["sha"])
            self.publisher.ensure_run_branch(str(existing["run_branch"]), base_sha)
            state = self._refresh(existing, parent_number)
            self.states.save_run(run_id, state)
            return state, True

    def _refresh(
        self, state: dict[str, Any], parent_number: int
    ) -> dict[str, Any]:
        try:
            graph = self.github.delivery_graph(parent_number)
            return state_from_graph(state, graph)
        except GitHubReadError as error:
            failed = dict(state)
            failed.update(
                {
                    "status": "blocked",
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
            "run_branch": f"agent-run/{run_id}",
            "ticket_graph": {
                "revision": None,
                "ordered_ticket_numbers": [],
                "tickets": {},
            },
            "frontier": [],
            "active_ticket_job": None,
            "ticket_jobs": {},
            "status": "starting",
            "diagnostics": [],
            "created_at": now,
            "updated_at": now,
        }

def _run_id(repository: str, parent_number: int, base_sha: str) -> str:
    identity = f"{repository}\0{parent_number}\0{base_sha}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return f"run-{parent_number}-{suffix}"

def _state_mapping(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
