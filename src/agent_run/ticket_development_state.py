"""Ticket-local projection with atomic commits to the one Run StateStore."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from agent_run.agent_invocation import _sync_invocation_history
from agent_run.state import StateStore
from agent_run.error_safety import bounded_error
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_eligibility import DISQUALIFYING_LABELS
from agent_run.ticket_queue import enqueue, reconcile


# Serialize controller selection with lane commits. Integration I/O runs outside
# this lock; StateStore rebases each integration save onto concurrent lane commits.
parallel_delivery_state_lock = RLock()
_SCHEDULER_JOB_FIELDS = frozenset({"selection_reason", "source_revision_changed"})


def _execution_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {key: value for key, value in job.items() if key not in _SCHEDULER_JOB_FIELDS}


def _demand_identity(state: dict[str, Any], number: str) -> tuple[Any, Any, Any, Any]:
    ticket = state["ticket_graph"]["tickets"].get(number)
    if not isinstance(ticket, dict):
        return (state["parent"]["revision"], state["ticket_graph"]["revision"], ticket, ())
    # Ordinary labels do not change the Ticket contract or work-entry gate.
    labels = set(ticket["labels"])
    return (
        state["parent"]["revision"],
        state["ticket_graph"]["revision"],
        {key: value for key, value in ticket.items() if key != "labels"},
        tuple(sorted(labels & (DISQUALIFYING_LABELS | {"ready-for-agent"}))),
    )


class TicketDevelopmentState:
    """Let a development lane use the existing Engine without snapshot clobbering.

    Only the lane's Job, Invocation and history are writable here. The Run
    scheduler remains the writer of the graph, selected Job and global gates.
    """

    def __init__(self, states: StateStore, run_id: str, ticket_number: int) -> None:
        self.states = states
        self.run_id = run_id
        self.ticket_number = ticket_number
        self.root = states.root
        self._last_job: dict[str, Any] | None = None
        self._last_invocation: dict[str, Any] | None = None
        self._last_demand: tuple[Any, Any, Any, Any] | None = None

    def load_current_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id != self.run_id:
            raise ValueError("Ticket development state belongs to another Run")
        state = self.states.load_current_run(run_id)
        if state is None:
            return None
        number = str(self.ticket_number)
        self._last_demand = deepcopy(_demand_identity(state, number))
        job = state["ticket_jobs"].get(number)
        self._last_job = deepcopy(job) if isinstance(job, dict) else None
        if not isinstance(job, dict):
            job = {"ticket_number": self.ticket_number}
            state["ticket_jobs"][number] = job
        state["active_ticket_job"] = job
        invocation = state["ticket_agent_invocations"].get(number)
        self._last_invocation = deepcopy(invocation) if isinstance(invocation, dict) else None
        state["active_agent_invocation"] = deepcopy(invocation)
        if state["status"] not in {
            "operator_stopped", "abandoned", "requeue_required",
            "unsupported_scope_change", "deterministic_contradiction",
        }:
            state["status"] = "active"
            state["diagnostics"] = []
        return state

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        if run_id != self.run_id:
            raise ValueError("Ticket development state belongs to another Run")
        job = state.get("active_ticket_job")
        if not isinstance(job, dict) or job.get("ticket_number") != self.ticket_number:
            raise ValueError("Ticket development attempted to save another Job")
        number = str(self.ticket_number)

        def commit() -> None:
            latest = self.states.load_current_run(run_id)
            if latest is None:
                raise ValueError("Delivery Run disappeared during Ticket development")
            if latest.get("status") in {
                "operator_stopped", "abandoned", "completed", "requeue_required",
                "unsupported_scope_change", "deterministic_contradiction",
            }:
                raise TaskControlBusyError("Ticket development lost Run authorization")
            current = latest["ticket_jobs"].get(number)
            if _execution_job(current) != _execution_job(self._last_job):
                raise TaskControlBusyError("Ticket Job changed while its development lane ran")
            if latest["ticket_agent_invocations"].get(number) != self._last_invocation:
                raise TaskControlBusyError("Ticket Invocation changed while its development lane ran")
            if _demand_identity(latest, number) != _demand_identity(state, number):
                raise TaskControlBusyError("Ticket demand changed while its development lane ran")
            if (
                isinstance(current, dict)
                and current.get("ticket_branch_generation") is not None
                and current.get("ticket_branch_generation") != job.get("ticket_branch_generation")
            ):
                raise TaskControlBusyError("Ticket Job generation changed")
            saved_job = deepcopy(job)
            if isinstance(current, dict):
                for field in _SCHEDULER_JOB_FIELDS:
                    if field in current:
                        saved_job[field] = deepcopy(current[field])
                    else:
                        saved_job.pop(field, None)
            latest["ticket_jobs"][number] = saved_job
            invocation = state.get("active_agent_invocation")
            if isinstance(invocation, dict):
                latest["ticket_agent_invocations"][number] = deepcopy(invocation)
            else:
                latest["ticket_agent_invocations"].pop(number, None)
            _merge_invocation_history(latest, state, self.ticket_number)
            _merge_resume_successors(latest, state)
            if job.get("phase") == "ready_for_integration":
                enqueue(latest, latest["ticket_jobs"][number])
            else:
                reconcile(latest, latest["ticket_jobs"][number])
            if job.get("blocked_reason") == "ticket_paused":
                latest["ticket_pause_requests"].pop(number, None)
            selected = latest.get("active_ticket_job")
            if isinstance(selected, dict) and selected.get("ticket_number") == self.ticket_number:
                latest["active_ticket_job"] = latest["ticket_jobs"][number]
                latest["active_agent_invocation"] = deepcopy(invocation)
                latest["status"] = state["status"]
                latest["diagnostics"] = deepcopy(state.get("diagnostics", []))
            if state.get("status") == "requeue_required":
                for key in ("status", "terminal_kind", "diagnostics", "requeue_required"):
                    latest[key] = deepcopy(state.get(key))
            self.states._save_run_and_notify_unlocked(run_id, latest)
            self._last_job = deepcopy(latest["ticket_jobs"][number])
            self._last_invocation = deepcopy(invocation) if isinstance(invocation, dict) else None

        with parallel_delivery_state_lock:
            self.states._commit_run(commit)

    def block_execution_failure(self, error: Exception) -> None:
        """Pause only this Ticket after its lane fails, preserving its work."""

        def commit() -> None:
            latest = self.states.load_current_run(self.run_id)
            if latest is None:
                raise ValueError("Delivery Run disappeared during Ticket failure")
            if latest.get("status") in {
                "operator_stopped", "abandoned", "requeue_required",
                "unsupported_scope_change", "deterministic_contradiction",
            }:
                raise TaskControlBusyError("Ticket development lost Run authorization")
            number = str(self.ticket_number)
            job = latest["ticket_jobs"].get(number)
            if not isinstance(job, dict) or _execution_job(job) != _execution_job(self._last_job):
                raise TaskControlBusyError("Ticket Job changed while its development lane ran")
            if latest["ticket_agent_invocations"].get(number) != self._last_invocation:
                raise TaskControlBusyError("Ticket Invocation changed while its development lane ran")
            if _demand_identity(latest, number) != self._last_demand:
                raise TaskControlBusyError("Ticket demand changed while its development lane ran")
            if job.get("phase") == "blocked":
                return
            job["execution_resume_phase"] = job.get("phase") or "developing"
            job["phase"] = "blocked"
            job["blocked_reason"] = "ticket_execution_failed"
            job["execution_failure"] = bounded_error(str(error))
            invocation = latest["ticket_agent_invocations"].get(number)
            if isinstance(invocation, dict) and invocation.get("status") in {
                "running", "resuming",
            }:
                invocation = dict(invocation)
                invocation.update({
                    "status": "failed",
                    "interruption_observed_at": datetime.now(UTC).isoformat(),
                    "error": "ticket_execution_failed",
                })
                latest["ticket_agent_invocations"][number] = invocation
                _sync_invocation_history(latest, invocation)
            selected = latest.get("active_ticket_job")
            if isinstance(selected, dict) and selected.get("ticket_number") == self.ticket_number:
                latest["active_ticket_job"] = job
                if isinstance(invocation, dict):
                    latest["active_agent_invocation"] = invocation
                latest["status"] = "blocked"
                latest["diagnostics"] = [{
                    "code": "ticket_execution_failed",
                    "message": job["execution_failure"],
                    "ticket_number": self.ticket_number,
                }]
            self.states._save_run_and_notify_unlocked(self.run_id, latest)

        with parallel_delivery_state_lock:
            self.states._commit_run(commit)


def _merge_invocation_history(
    latest: dict[str, Any], lane: dict[str, Any], ticket_number: int
) -> None:
    subject = f"ticket:{ticket_number}"
    history = latest["agent_invocation_history"]
    for entry in lane.get("agent_invocation_history", []):
        if not isinstance(entry, dict) or entry.get("work_subject") != subject:
            continue
        identity = (entry.get("work_subject"), entry.get("started_at"))
        for index, previous in enumerate(history):
            if isinstance(previous, dict) and (
                previous.get("work_subject"), previous.get("started_at")
            ) == identity:
                history[index] = deepcopy(entry)
                break
        else:
            history.append(deepcopy(entry))


def _merge_resume_successors(latest: dict[str, Any], lane: dict[str, Any]) -> None:
    from agent_run.resume_audit_contract import resume_history_digest

    current = latest["resume_audit"]["history"]
    by_id = {
        event.get("resume_id"): event for event in lane["resume_audit"]["history"]
        if isinstance(event, dict) and event.get("successor_invocation_started_at")
    }
    changed = False
    for event in current:
        if isinstance(event, dict) and event.get("resume_id") in by_id:
            source = by_id[event["resume_id"]]
            if event.get("successor_invocation_started_at") != source.get(
                "successor_invocation_started_at"
            ):
                event["successor_invocation_started_at"] = source[
                    "successor_invocation_started_at"
                ]
                changed = True
    if changed:
        latest["resume_audit"]["rolling_digest"] = resume_history_digest(current)
