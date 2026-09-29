"""One durable FIFO and admission boundary for all Ticket Agent roles."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from agent_run.state import StateStore
from agent_run.task_control import TaskControlBusyError


WORK_ROLES = frozenset({"development", "reviewer", "publication", "integration_conflict"})


def capacity(state: dict[str, Any]) -> int:
    value = state["policy_snapshot"]["ticket_development_workers"]
    if type(value) is not int or value < 1:
        raise ValueError("Ticket work capacity must be positive")
    return value


def generation(state: dict[str, Any], number: int) -> int:
    job = state["ticket_jobs"].get(str(number), {})
    return int(job.get("ticket_branch_generation") or (
        state.get("retired_ticket_generations", {}).get(str(number), 0) + 1
    ))


def enqueue(state: dict[str, Any], number: int, role: str) -> dict[str, Any]:
    queue: list[dict[str, Any]] = state["ticket_work_queue"]
    for item in queue:
        if item["ticket_number"] == number:
            return item
    item = {"ticket_number": number, "generation": generation(state, number),
            "role": role, "lease_id": None}
    queue.append(item)
    return item


def reconcile(state: dict[str, Any]) -> None:
    """Cancel obsolete waiters; running work releases only through its token."""
    from agent_run.ticket_state import RUN_STOP_STATUSES

    def keep(item: dict[str, Any]) -> bool:
        if item["lease_id"] is not None:
            return True
        number = item["ticket_number"]
        job = state["ticket_jobs"].get(str(number), {})
        return (
            state.get("status") not in RUN_STOP_STATUSES
            and number in state["frontier"]
            and str(number) not in state["ticket_pause_requests"]
            and item["generation"] == generation(state, number)
            and job.get("phase") not in {
                "blocked", "completed", "publication_pending", "ready_for_integration",
                "waiting_checks", "waiting_merge", "merged",
            }
        )

    state["ticket_work_queue"] = [item for item in state["ticket_work_queue"] if keep(item)]


class TicketWorkAdmission:
    """Short transactions, with no Agent, Git or network work under the lock."""

    def __init__(self, states: StateStore, run_id: str) -> None:
        self.states = states
        self.run_id = run_id

    def seed(self, numbers: list[int]) -> None:
        def commit() -> None:
            state = self._load()
            before = deepcopy(state["ticket_work_queue"])
            reconcile(state)
            for number in numbers:
                if number in state["frontier"] and str(number) not in state["ticket_pause_requests"]:
                    enqueue(state, number, "development")
            if state["ticket_work_queue"] != before:
                self.states._save_run_and_notify_unlocked(self.run_id, state)
        self.states._commit_run(commit)

    def acquire(
        self, number: int, expected_generation: int, role: str, *, allow_start: bool = True,
    ) -> str | None:
        token: str | None = None

        def commit() -> None:
            nonlocal token
            from agent_run.ticket_state import RUN_STOP_STATUSES

            state = self._load()
            previous = deepcopy(state["ticket_work_queue"])
            job = state["ticket_jobs"].get(str(number), {})
            if (
                state["status"] in RUN_STOP_STATUSES
                or generation(state, number) != expected_generation
                or job.get("phase") in {"blocked", "completed", "publication_pending"}
            ):
                raise TaskControlBusyError("Ticket work lost its current Job authorization")
            reconcile(state)
            item = enqueue(state, number, role)
            if item["lease_id"] is not None:
                raise TaskControlBusyError("Ticket already has current Agent work")
            item["role"] = role
            waiting = [entry for entry in state["ticket_work_queue"] if entry["lease_id"] is None]
            running = len(state["ticket_work_queue"]) - len(waiting)
            if (
                allow_start and waiting[0] is item and running < capacity(state)
                and str(number) not in state["ticket_pause_requests"]
            ):
                token = uuid4().hex
                item["lease_id"] = token
            if state["ticket_work_queue"] != previous:
                self.states._save_run_and_notify_unlocked(self.run_id, state)

        self.states._commit_run(commit)
        return token

    def release(self, token: str) -> None:
        def commit() -> None:
            state = self._load()
            queue = state["ticket_work_queue"]
            state["ticket_work_queue"] = [item for item in queue if item["lease_id"] != token]
            if state["ticket_work_queue"] != queue:
                self.states._save_run_and_notify_unlocked(self.run_id, state)
        self.states._commit_run(commit)

    def cancel_waiter(self, number: int, expected_generation: int) -> None:
        """A new external wait is no longer ready to compete for Agent work."""
        def commit() -> None:
            state = self._load()
            queue = state["ticket_work_queue"]
            state["ticket_work_queue"] = [item for item in queue if not (
                item["ticket_number"] == number and item["generation"] == expected_generation
                and item["lease_id"] is None
            )]
            if state["ticket_work_queue"] != queue:
                self.states._save_run_and_notify_unlocked(self.run_id, state)
        self.states._commit_run(commit)

    def _load(self) -> dict[str, Any]:
        state = self.states.load_current_run(self.run_id)
        if state is None:
            raise ValueError("Delivery Run disappeared during work admission")
        return state


def require_queue(state: dict[str, Any]) -> None:
    from agent_run.state_errors import IncompatibleRunStateError

    queue = state.get("ticket_work_queue")
    if not isinstance(queue, list):
        raise IncompatibleRunStateError("legacy state is missing Ticket Work Queue")
    numbers: set[int] = set()
    tokens: set[str] = set()
    for item in queue:
        if not isinstance(item, dict) or set(item) != {"ticket_number", "generation", "role", "lease_id"}:
            raise IncompatibleRunStateError("invalid Ticket Work Queue entry")
        number, token = item["ticket_number"], item["lease_id"]
        if (
            type(number) is not int or str(number) not in state["ticket_graph"]["tickets"]
            or number in numbers or type(item["generation"]) is not int
            or item["generation"] != generation(state, number)
            or not isinstance(item["role"], str) or item["role"] not in WORK_ROLES
            or (token is not None and (not isinstance(token, str) or not token or token in tokens))
        ):
            raise IncompatibleRunStateError("invalid Ticket Work Queue ownership")
        numbers.add(number)
        if token is not None:
            tokens.add(token)
    if len(tokens) > capacity(state):
        raise IncompatibleRunStateError("Ticket work capacity exceeded")
