"""Execution-local Agent admission; delivery stages own business successors."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Token
from typing import Any

from agent_run.agent_return import has_returned_agent_result
from agent_run.delivery_step import AgentWorkRequired
from agent_run.ticket_work import TicketWorkAdmission
from agent_run.worker_control import worker_control, worker_lease


class AgentWorkScope:
    """One stage may call one Agent; cached returns need no new work lease.

    Admission precedes semantic allocation and charging. The stage can still
    commit, reconcile and consume returns when no Agent capacity is available.
    Nothing here selects a role, allocates an Attempt or advances a Job.
    """

    def __init__(self, admission: TicketWorkAdmission | None = None) -> None:
        self.admission = admission
        self.capacity_wait = False
        self._job: dict[str, Any] | None = None
        self._allowed = False
        self._lease: str | None = None
        self._control: Token[Callable[[], None] | None] | None = None
        self._lease_context: Token[str | None] | None = None

    @contextmanager
    def step(self, job: dict[str, Any], *, allow_agent: bool) -> Iterator[None]:
        self._job = job
        self._allowed = allow_agent
        self.capacity_wait = False
        try:
            yield
        finally:
            self.release()
            self._job = None

    def require(self, role: str) -> None:
        job = self._job
        if job is None:
            raise ValueError("Agent admission requires an active delivery step")
        semantic_role = "development" if role == "integration_conflict" else role
        if has_returned_agent_result(job, role=semantic_role):
            # Common execution validates and restores the durable return.
            # Currentness and role-result consumption remain stage concerns.
            return
        if self.admission is None:
            if not self._allowed:
                raise AgentWorkRequired(role)
        else:
            self._lease = self.admission.acquire(
                int(job["ticket_number"]), int(job["ticket_branch_generation"]),
                role, allow_start=self._allowed,
            )
            if self._lease is None:
                self.capacity_wait = self._allowed
                raise AgentWorkRequired(role)
            admission, lease = self.admission, self._lease
            self._control = worker_control.set(lambda: admission.check_control(lease))
            self._lease_context = worker_lease.set(lease)
        self._allowed = False

    def release(self) -> None:
        if self._lease_context is not None:
            worker_lease.reset(self._lease_context)
            self._lease_context = None
        if self._control is not None:
            worker_control.reset(self._control)
            self._control = None
        if self._lease is not None:
            assert self.admission is not None
            self.admission.release(self._lease)
            self._lease = None

    def defer(self) -> None:
        """An external wait no longer competes for an unstarted Agent slot."""
        if self.admission is not None and self._job is not None:
            self.admission.cancel_waiter(
                int(self._job["ticket_number"]), int(self._job["ticket_branch_generation"]),
            )
