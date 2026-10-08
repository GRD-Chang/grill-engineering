"""Ticket-local projection with atomic commits to the one Run StateStore."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agent_run.agent_invocation import _sync_invocation_history
from agent_run.state import StateStore
from agent_run.error_safety import bounded_error
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_eligibility import DISQUALIFYING_LABELS
from agent_run.ticket_queue import enqueue, reconcile
from agent_run.ticket_pause import TicketStopRequested, pause_ticket


RUN_STOP_STATUSES = frozenset({
    "operator_stopped", "abandoned", "abandonment_pending", "completed",
    "execution_failed", "supervision_timeout", "requeue_required",
    "unsupported_scope_change", "deterministic_contradiction",
})
_WAIT_FIELDS = ("supervision_window", "supervision_wait", "credential_availability")
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


class TicketState:
    """Persist one Ticket through development, integration and supervision.

    Graph/selection/global control remain Controller-owned. Every commit checks
    the original Job and demand, then patches only this Ticket into latest state.
    """

    def __init__(self, states: StateStore, run_id: str, ticket_number: int) -> None:
        self.states = states
        self.run_id = run_id
        self.ticket_number = ticket_number
        self.root = states.root
        self._last_job: dict[str, Any] | None = None
        self._last_invocation: dict[str, Any] | None = None
        self._last_demand: tuple[Any, Any, Any, Any] | None = None
        self._loaded = False
        self._last_holder: int | None = None

    def load_current_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id != self.run_id:
            raise ValueError("Ticket state belongs to another Run")
        state = self.states.load_current_run(run_id)
        if state is None:
            return None
        number = str(self.ticket_number)
        selected = state.get("active_ticket_job")
        selected_number = selected.get("ticket_number") if isinstance(selected, dict) else None
        original_job = state["ticket_jobs"].get(number)
        job = original_job
        if not isinstance(job, dict):
            job = {"ticket_number": self.ticket_number}
            state["ticket_jobs"][number] = job
        state["active_ticket_job"] = job
        invocation = state["ticket_agent_invocations"].get(number)
        if not self._loaded:
            self._last_demand = deepcopy(_demand_identity(state, number))
            self._last_job = deepcopy(original_job) if isinstance(original_job, dict) else None
            self._last_invocation = deepcopy(invocation) if isinstance(invocation, dict) else None
            self._last_holder = state["ticket_integration_holder"]
            self._loaded = True
        state["active_agent_invocation"] = deepcopy(invocation)
        if selected_number != self.ticket_number and state["status"] not in RUN_STOP_STATUSES:
            state["status"] = "active"
            state["diagnostics"] = []
            state["terminal_kind"] = None
            for key in _WAIT_FIELDS:
                state.pop(key, None)
        if state["status"] in {"active", "ticket_delivery_pending", "waiting_checks", "waiting_external", "waiting_merge"}:
            wait = job.get("external_wait")
            if isinstance(wait, dict):
                state.update(deepcopy(wait))
        return state

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        if run_id != self.run_id:
            raise ValueError("Ticket state belongs to another Run")
        job = state.get("active_ticket_job")
        if not isinstance(job, dict) or job.get("ticket_number") != self.ticket_number:
            raise ValueError("Ticket attempted to save another Job")
        number = str(self.ticket_number)

        def commit() -> None:
            latest = self.states.load_current_run(run_id)
            if latest is None:
                raise ValueError("Delivery Run disappeared during Ticket execution")
            if latest.get("status") in RUN_STOP_STATUSES:
                raise TaskControlBusyError("Ticket lost Run authorization")
            current = latest["ticket_jobs"].get(number)
            if _execution_job(current) != _execution_job(self._last_job):
                raise TaskControlBusyError("Ticket Job changed while its lane ran")
            if latest["ticket_agent_invocations"].get(number) != self._last_invocation:
                raise TaskControlBusyError("Ticket Invocation changed while its lane ran")
            if _demand_identity(latest, number) != _demand_identity(state, number):
                raise TaskControlBusyError("Ticket demand changed while its lane ran")
            if (
                isinstance(current, dict)
                and current.get("ticket_branch_generation") is not None
                and current.get("ticket_branch_generation") != job.get("ticket_branch_generation")
            ):
                raise TaskControlBusyError("Ticket Job generation changed")
            if _demand_identity(latest, number) != self._last_demand:
                raise TaskControlBusyError("Ticket demand changed after execution began")
            invocation = state.get("active_agent_invocation")
            diagnostics = state.get("diagnostics", [])
            if (
                state.get("status") == "execution_failed"
                and isinstance(invocation, dict)
                and invocation.get("work_subject") == f"ticket:{self.ticket_number}"
                and invocation.get("status") == "failed"
                and diagnostics and diagnostics[0].get("code") == "agent_invocation_failed"
            ):
                # The shared Invocation recorder reports execution failure.
                # Commit its Ticket owner as paused in the same transaction,
                # never expose that local event as a Run authorization loss.
                if number in latest["ticket_pause_requests"]:
                    pause_ticket(state, job)
                else:
                    job["execution_resume_phase"] = job["phase"]
                    job["phase"] = "blocked"
                    job["blocked_reason"] = "ticket_execution_failed"
                    job["execution_failure"] = bounded_error(str(
                        invocation.get("error") or diagnostics[0]["message"]
                    ))
                    if "prior_human_blockers" in job:
                        job.pop("human_blockers", None)
                    state["status"] = "blocked"
                    state["terminal_kind"] = None
            holder = state["ticket_integration_holder"]
            if holder != self._last_holder and (holder == self.ticket_number or self._last_holder == self.ticket_number):
                if latest["ticket_integration_holder"] != self._last_holder:
                    raise TaskControlBusyError("Ticket integration seat changed")
                if holder == self.ticket_number and latest["ticket_integration_queue"]:
                    if latest["ticket_integration_queue"][0]["ticket_number"] != self.ticket_number:
                        raise TaskControlBusyError("Ticket integration queue changed")
                latest["ticket_integration_holder"] = holder
            _clear_progressed_wait(state, self._last_job)
            capture_ticket_wait(state)
            if state.get("status") not in {"waiting_checks", "waiting_external", "waiting_merge"}:
                job.pop("external_wait", None)
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
            _merge_resume_successors(latest, state, self.ticket_number)
            if job.get("phase") == "ready_for_integration":
                enqueue(latest, latest["ticket_jobs"][number])
            else:
                reconcile(latest, latest["ticket_jobs"][number])
            if job.get("phase") in {"blocked", "completed", "publication_pending"}:
                latest["ticket_pause_requests"].pop(number, None)
            selected = latest.get("active_ticket_job")
            if isinstance(selected, dict) and selected.get("ticket_number") == self.ticket_number:
                latest["active_ticket_job"] = latest["ticket_jobs"][number]
                latest["active_agent_invocation"] = deepcopy(invocation)
                latest["status"] = state["status"]
                latest["diagnostics"] = deepcopy(state.get("diagnostics", []))
                latest["terminal_kind"] = state.get("terminal_kind")
                for key in _WAIT_FIELDS:
                    if key in state:
                        latest[key] = deepcopy(state[key])
                    else:
                        latest.pop(key, None)
            if state.get("status") == "requeue_required":
                for key in ("status", "terminal_kind", "diagnostics", "requeue_required"):
                    latest[key] = deepcopy(state.get(key))
            _merge_ticket_cleanup(latest, state, job)
            self.states._save_run_and_notify_unlocked(run_id, latest)
            state["ticket_work_queue"] = deepcopy(latest["ticket_work_queue"])
            # The lane also validates its in-memory projection between saves.
            # Keep its canonical Invocation synchronized with the active one,
            # just as StateStore does for a direct Controller save.
            if isinstance(invocation, dict):
                state["ticket_agent_invocations"][number] = deepcopy(invocation)
            else:
                state["ticket_agent_invocations"].pop(number, None)
            self._last_holder = latest["ticket_integration_holder"]
            # Persistence normalizes diagnostic payloads (including Git control
            # characters). Compare subsequent writes with the durable baseline,
            # rather than the unsanitized in-memory payload we just saved.
            from agent_run.state import _sanitize_durable_errors
            baseline = _sanitize_durable_errors({
                "job": latest["ticket_jobs"][number], "invocation": invocation,
            })
            self._last_job = baseline["job"]
            self._last_invocation = baseline["invocation"] if isinstance(invocation, dict) else None

        self.states._commit_run(commit)

    def block_execution_failure(self, error: Exception) -> None:
        """Pause only this Ticket after its lane fails, preserving its work."""
        from agent_run.external_supervision import (
            ensure_supervision_window,
            is_proven_github_state_contradiction,
            wait_for_github_convergence,
        )
        from agent_run.github import GitHubReadError

        if isinstance(error, GitHubReadError) and is_proven_github_state_contradiction(error.code):
            raise error
        # A merge/close response can be lost after the external mutation.
        # Preserve its recovery boundary and seat until the result is known.
        if self._last_job and self._last_job.get("phase") == "completed":
            raise error
        if self._last_job and self._last_job.get("phase") in {"merging", "merged"}:
            # The write may have succeeded. Keep its intent and integration
            # seat while the normal Published-Head path observes the result.
            error = GitHubReadError("merge_reconciliation_pending", bounded_error(str(error)))
        if isinstance(error, GitHubReadError):
            # Branch preparation can fail before entering the shared Change
            # Delivery loop. Keep that unknown observation bounded as well.
            state = self.load_current_run(self.run_id)
            if state is None:
                raise error
            wait_for_github_convergence(
                state, code=error.code, message=str(error),
                waiting_for=f"ticket-{self.ticket_number}",
            )
            ensure_supervision_window(state)
            self.save_run(self.run_id, state)
            from agent_run.ticket_work import TicketWorkAdmission, generation

            TicketWorkAdmission(self.states, self.run_id).cancel_waiter(
                self.ticket_number, generation(state, self.ticket_number),
            )
            return

        def commit() -> None:
            latest = self.states.load_current_run(self.run_id)
            if latest is None:
                raise ValueError("Delivery Run disappeared during Ticket failure")
            if latest.get("status") in RUN_STOP_STATUSES:
                raise TaskControlBusyError("Ticket lost Run authorization")
            number = str(self.ticket_number)
            job = latest["ticket_jobs"].get(number)
            if not isinstance(job, dict) or _execution_job(job) != _execution_job(self._last_job):
                raise TaskControlBusyError("Ticket Job changed while its lane ran")
            if latest["ticket_agent_invocations"].get(number) != self._last_invocation:
                raise TaskControlBusyError("Ticket Invocation changed while its lane ran")
            if _demand_identity(latest, number) != self._last_demand:
                raise TaskControlBusyError("Ticket demand changed while its lane ran")
            if job.get("phase") == "blocked":
                return
            stopping = isinstance(error, TicketStopRequested) or number in latest["ticket_pause_requests"]
            if stopping:
                pause_ticket(latest, job)
            else:
                job["execution_resume_phase"] = job.get("phase") or "developing"
                job["phase"] = "blocked"
                job["blocked_reason"] = "ticket_execution_failed"
                job["execution_failure"] = bounded_error(str(error))
                if "prior_human_blockers" in job:
                    job.pop("human_blockers", None)
            invocation = latest["ticket_agent_invocations"].get(number)
            if isinstance(invocation, dict) and invocation.get("status") in {
                "running", "resuming",
            }:
                invocation = dict(invocation)
                invocation.update({
                    "status": "failed",
                    "interruption_observed_at": datetime.now(UTC).isoformat(),
                    "error": "ticket_paused" if stopping else "ticket_execution_failed",
                    "execution_interrupted": True,
                })
                latest["ticket_agent_invocations"][number] = invocation
                _sync_invocation_history(latest, invocation)
            reconcile(latest, job)
            selected = latest.get("active_ticket_job")
            if isinstance(selected, dict) and selected.get("ticket_number") == self.ticket_number:
                latest["active_ticket_job"] = job
                if isinstance(invocation, dict):
                    latest["active_agent_invocation"] = invocation
                latest["status"] = "blocked"
                latest["diagnostics"] = [{
                    "code": job["blocked_reason"],
                    "message": job.get("execution_failure", "Ticket paused; explicit resume is required"),
                    "ticket_number": self.ticket_number,
                }]
            self.states._save_run_and_notify_unlocked(self.run_id, latest)

        self.states._commit_run(commit)

    def block_shared_failure(self, error: Exception) -> None:
        """Fence the Run before other lanes can commit after proven auth loss."""
        from agent_run.agent_invocation import fail_interrupted_invocation
        from agent_run.controller import _operator_gate_diagnostic

        def commit() -> None:
            latest = self.states.load_current_run(self.run_id)
            if latest is None:
                raise ValueError("Delivery Run disappeared during credential failure")
            if latest.get("status") in RUN_STOP_STATUSES or latest.get("terminal_kind") == "permanent_blocked":
                return
            number = str(self.ticket_number)
            job = latest["ticket_jobs"].get(number)
            if _execution_job(job) != _execution_job(self._last_job):
                raise TaskControlBusyError("Ticket changed before credential failure")
            latest["active_ticket_job"] = job
            latest["active_agent_invocation"] = latest["ticket_agent_invocations"].get(number)
            invocation = latest["active_agent_invocation"]
            if isinstance(invocation, dict):
                fail_interrupted_invocation(
                    latest, role=str(invocation["role"]), save=lambda _: None,
                )
            latest.update({
                "status": "execution_failed", "terminal_kind": "execution_failed",
                "diagnostics": [_operator_gate_diagnostic(
                    latest, code="runner_credential_failed", message=str(error),
                    action_kind="execution_failure", reason="runner_credential_failed",
                    fallback_phase="active",
                )],
            })
            self.states._save_run_and_notify_unlocked(self.run_id, latest)

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


def _merge_resume_successors(latest: dict[str, Any], lane: dict[str, Any], ticket_number: int) -> None:
    from agent_run.resume_audit_contract import resume_event_digest, resume_history_digest

    current = latest["resume_audit"]["history"]
    by_id = {
        event.get("resume_id"): event for event in lane["resume_audit"]["history"]
        if isinstance(event, dict) and event.get("successor_invocation_started_at")
        and event.get("work_subject") == f"ticket:{ticket_number}"
    }
    changed = False
    for event in current:
        if isinstance(event, dict) and event.get("resume_id") in by_id:
            source = by_id[event["resume_id"]]
            if event.get("successor_invocation_started_at") != source.get(
                "successor_invocation_started_at"
            ):
                if event.get("successor_invocation_started_at") is not None:
                    raise TaskControlBusyError("Resume already belongs to another Invocation")
                for key in ("work_subject", "generation", "semantic_attempt_id", "successor_invocation_started_at"):
                    event[key] = source[key]
                event["event_digest"] = resume_event_digest(event)
                changed = True
    if changed:
        latest["resume_audit"]["rolling_digest"] = resume_history_digest(current)


def _merge_ticket_cleanup(latest: dict[str, Any], lane: dict[str, Any], job: dict[str, Any]) -> None:
    branch = job.get("ticket_branch")
    cleanup = lane.get("delivery_cleanup")
    if not isinstance(cleanup, dict) or branch not in cleanup.get("items", {}):
        return
    target = latest.setdefault("delivery_cleanup", {"items": {}})
    target["items"][branch] = deepcopy(cleanup["items"][branch])
    pending = [item for item in target["items"].values() if item.get("status") != "completed"]
    target["status"] = "cleanup_pending" if pending else "completed"
    if pending:
        target["last_error"] = pending[0].get("last_error", "cleanup is pending")
    else:
        target.pop("last_error", None)


def _clear_progressed_wait(state: dict[str, Any], previous: dict[str, Any] | None) -> None:
    """A successful retry can change phase/head before the old status clears."""
    from agent_run.external_supervision import supervision_window_matches

    window = state.get("supervision_window")
    old_wait = previous.get("external_wait") if isinstance(previous, dict) else None
    if (
        isinstance(window, dict) and isinstance(old_wait, dict)
        and window == old_wait.get("supervision_window")
        and state.get("status") in {"waiting_checks", "waiting_external", "waiting_merge"}
        and not supervision_window_matches(state, window)
    ):
        state["status"] = "active"
        state["terminal_kind"] = None
        state["diagnostics"] = []
        for key in ("supervision_window", "supervision_wait"):
            state.pop(key, None)


def capture_ticket_wait(state: dict[str, Any]) -> None:
    """Keep a Ticket's wait when the public foreground supervisor updates it."""
    job = state.get("active_ticket_job")
    if (
        not isinstance(job, dict)
        or job.get("phase") in {"blocked", "completed"}
        or state.get("github_refresh_pending")
        or state.get("status") not in {"waiting_checks", "waiting_external", "waiting_merge"}
    ):
        return
    if not isinstance(state.get("supervision_window"), dict):
        job.pop("external_wait", None)
        return
    job["external_wait"] = {
        key: deepcopy(state[key]) for key in (*_WAIT_FIELDS, "status", "terminal_kind", "diagnostics")
        if key in state
    }
    state["ticket_jobs"][str(job["ticket_number"])] = job
