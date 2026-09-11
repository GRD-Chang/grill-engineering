from __future__ import annotations

from datetime import UTC, datetime, timedelta
from copy import deepcopy
from typing import Any, Callable

from agent_run.requeue import RequeueError, current_change_job
from agent_run.semantic_attempt import canonical_fingerprint as canonical_fingerprint
from agent_run.semantic_attempt import invocation_attempt_is_pending
from agent_run.resume_audit import bind_resume_to_successor


def _sync_invocation_history(
    state: dict[str, Any], invocation: dict[str, Any]
) -> None:
    """Keep one live history snapshot for each started Invocation."""

    history = state.setdefault("agent_invocation_history", [])
    if not isinstance(history, list):
        raise ValueError("agent_invocation_history must be an array")
    started_at = invocation.get("started_at")
    for index in range(len(history) - 1, -1, -1):
        previous = history[index]
        if (
            isinstance(previous, dict)
            and previous.get("started_at") == started_at
            and previous.get("work_subject") == invocation.get("work_subject")
        ):
            history[index] = dict(invocation)
            return
    history.append(dict(invocation))


def select_publication_thread(
    job: dict[str, Any], *, max_context_attempts: int
) -> str | None:
    """Select Publication context using the shared, bounded fallback order."""

    if job.get("publication_new_thread") is True:
        return None
    publication_thread = job.get("publication_thread_id")
    if isinstance(publication_thread, str) and publication_thread:
        return publication_thread
    development_thread = job.get("development_thread_id")
    if (
        isinstance(development_thread, str)
        and development_thread
        and int(job.get("publication_attempts", 0)) < max_context_attempts
    ):
        return development_thread
    return None


def invocation_event_recorder(
    state: dict[str, Any],
    *,
    role: str,
    phase: str,
    work_subject: str,
    generation: int,
    invocation_input: dict[str, Any],
    currentness_boundary: dict[str, Any],
    semantic_attempt: dict[str, Any],
    save: Callable[[dict[str, Any]], object],
    invocation_deadline_seconds: float | None = None,
) -> Callable[..., None]:
    """Persist the small, durable facts for one active Agent Invocation."""

    recovery_state: dict[str, Any] = {
        "output_attempt": 1,
        "validation_error": "",
        "ordinary_recovery_used": False,
        "capacity_recovery_count": 0,
        "recovery_wait_intervals": [],
    }
    previous = state.get("active_agent_invocation")
    if (
        isinstance(previous, dict)
        and isinstance(previous.get("semantic_attempt"), dict)
        and previous["semantic_attempt"].get("attempt_id")
        == semantic_attempt.get("attempt_id")
    ):
        for key in (
            "ordinary_recovery_used",
            "capacity_recovery_count",
        ):
            value = previous.get(key, recovery_state[key])
            recovery_state[key] = value
        if (
            invocation_input.get("thread_id")
            and invocation_input.get("thread_id")
            == (previous.get("reported_thread_id") or previous.get("requested_thread_id"))
            and previous.get("status") in {"failed", "resuming", "running"}
            and previous.get("execution_interrupted") is True
        ):
            for key in ("output_attempt", "validation_error"):
                recovery_state[key] = previous.get(key, recovery_state[key])
            if isinstance(previous.get("deadline_seconds"), (int, float)):
                invocation_deadline_seconds = previous["deadline_seconds"]

    def recovery_allowed() -> bool:
        active = state.get("active_agent_invocation")
        if not (
            isinstance(active, dict)
            and active.get("status") in {"running", "resuming"}
            and isinstance(active.get("semantic_attempt"), dict)
            and active["semantic_attempt"].get("attempt_id")
            == semantic_attempt.get("attempt_id")
            and active.get("currentness_boundary") == currentness_boundary
            and invocation_attempt_is_pending(state, active)
            and (active.get("reported_thread_id") or active.get("requested_thread_id"))
            and state.get("status") not in {
                "operator_stopped", "abandoned", "execution_failed", "requeue_required"
            }
        ):
            return False
        # ProfiledAgentBackend separately invokes the Driver's exact ownership
        # fence. This check only establishes the pending work identity.
        return True

    def record(kind: str, **facts: object) -> None:
        now = datetime.now(UTC).isoformat()
        if kind == "started":
            resume_binding = bind_resume_to_successor(
                state,
                semantic_attempt=semantic_attempt,
                successor_started_at=now,
            )
            resume_id, resume_sequence = (
                resume_binding if resume_binding is not None else (None, None)
            )
            invocation: dict[str, Any] = {
                "work_subject": work_subject,
                "generation": generation,
                "role": role,
                "phase": phase,
                "mode": facts.get("invocation_mode")
                or ("resume" if facts.get("requested_thread_id") else "fresh"),
                "input_fingerprint": canonical_fingerprint(invocation_input),
                "currentness_boundary": dict(currentness_boundary),
                "semantic_attempt": deepcopy(semantic_attempt),
                "status": "running",
                "requested_thread_id": facts.get("requested_thread_id"),
                "reported_thread_id": None,
                "binding_id": facts.get("binding_id"),
                "binding_role": facts.get("binding_role"),
                "profile_role": facts.get("profile_role"),
                "invocation_role": facts.get("invocation_role"),
                "model": facts.get("model"),
                "reasoning_effort": facts.get("reasoning_effort"),
                "profile_revision": facts.get("profile_revision"),
                "thread_execution_binding": facts.get("thread_execution_binding"),
                "attempt_count": 0,
                "started_at": now,
                "ended_at": None,
                "error": None,
                "return_code": None,
                "signal": None,
                "resume_id": resume_id,
                "resume_sequence": resume_sequence,
                **recovery_state,
            }
            deadline_seconds = invocation_deadline_seconds
            if deadline_seconds is None:
                deadline_seconds = invocation_input.get("_invocation_deadline_seconds")
            if isinstance(deadline_seconds, (int, float)) and not isinstance(
                deadline_seconds, bool
            ):
                invocation["deadline_seconds"] = deadline_seconds
                try:
                    invocation["deadline_at"] = (
                        datetime.fromisoformat(now)
                        + timedelta(seconds=float(deadline_seconds))
                    ).isoformat()
                except OverflowError:
                    # Monotonic enforcement still applies; an unrepresentable
                    # far-future wall-clock timestamp is not durable evidence.
                    pass
            state["active_agent_invocation"] = invocation
            _sync_invocation_history(state, invocation)
        else:
            active = state.get("active_agent_invocation")
            if not isinstance(active, dict):
                raise ValueError("active Agent Invocation is missing")
            invocation = active
            invocation.update(facts)
            for key in recovery_state:
                if key in facts:
                    recovery_state[key] = facts[key]
            if kind == "output_step":
                invocation["execution_interrupted"] = True
            elif kind == "recovery_waiting":
                invocation["status"] = "resuming"
                invocation["recovery_waiting"] = True
                invocation["last_failure_at"] = now
                invocation["execution_interrupted"] = True
                intervals = invocation.setdefault("recovery_wait_intervals", [])
                if not isinstance(intervals, list):
                    intervals = []
                    invocation["recovery_wait_intervals"] = intervals
                if not any(
                    isinstance(interval, dict) and interval.get("ended_at") is None
                    for interval in intervals
                ):
                    intervals.append(
                        {
                            "kind": facts.get("recovery_kind"),
                            "started_at": now,
                            "ended_at": None,
                        }
                    )
            elif kind == "recovery_started":
                invocation["status"] = "running"
                invocation["recovery_waiting"] = False
                invocation["last_recovery_started_at"] = now
                invocation["ended_at"] = None
                intervals = invocation.get("recovery_wait_intervals")
                if isinstance(intervals, list):
                    for interval in reversed(intervals):
                        if (
                            isinstance(interval, dict)
                            and interval.get("ended_at") is None
                        ):
                            interval["ended_at"] = now
                            break
                seconds = invocation.get("deadline_seconds")
                if isinstance(seconds, (int, float)):
                    try:
                        invocation["deadline_at"] = (
                            datetime.fromisoformat(now) + timedelta(seconds=seconds)
                        ).isoformat()
                    except OverflowError:
                        invocation.pop("deadline_at", None)
            if kind == "thread_started":
                requested = invocation.get("requested_thread_id")
                if requested is not None and requested != invocation.get(
                    "reported_thread_id"
                ):
                    error = "reported Thread ID does not match requested Thread ID"
                    invocation.update(
                        {"status": "failed", "ended_at": now, "error": error}
                    )
                    _sync_invocation_history(state, invocation)
                    state.update(
                        {
                            "status": "execution_failed",
                            "terminal_kind": "execution_failed",
                            "diagnostics": [
                                {"code": "agent_invocation_failed", "message": error}
                            ],
                        }
                    )
                    save(state)
                    raise ValueError(error)
            elif kind in {"completed", "failed"}:
                invocation["status"] = kind
                invocation["ended_at"] = now
                invocation["recovery_waiting"] = False
                if kind == "completed":
                    invocation["execution_interrupted"] = False
                if kind == "failed":
                    state.update(
                        {
                            "status": "execution_failed",
                            "terminal_kind": "execution_failed",
                            "diagnostics": [
                                {
                                    "code": "agent_invocation_failed",
                                    "message": str(
                                        invocation.get("error")
                                        or "Codex invocation failed"
                                    ),
                                }
                            ],
                        }
                    )
            _sync_invocation_history(state, invocation)
        save(state)

    setattr(record, "deadline_seconds", invocation_deadline_seconds)
    setattr(record, "recovery_state", recovery_state)
    setattr(record, "recovery_allowed", recovery_allowed)
    return record


def fail_interrupted_invocation(
    state: dict[str, Any], *, role: str, save: Callable[[dict[str, Any]], object]
) -> bool:
    """Close a durable running Invocation after its controller disappeared."""

    active = state.get("active_agent_invocation")
    if (
        not isinstance(active, dict)
        or active.get("role") != role
        or active.get("status") != "running"
    ):
        return False
    failed = dict(active)
    failed.update(
        {
            "status": "failed",
            "interruption_observed_at": datetime.now(UTC).isoformat(),
            "error": "controller_interrupted",
        }
    )
    state["active_agent_invocation"] = failed
    _sync_invocation_history(state, failed)
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "diagnostics": [
                {
                    "code": "agent_invocation_failed",
                    "message": "controller_interrupted",
                }
            ],
        }
    )
    save(state)
    return True


def record_session_interruption(
    state: dict[str, Any], *, save: Callable[[dict[str, Any]], object]
) -> None:
    """Persist a proven Executor Session loss without replaying its work."""

    if session_interruption_is_persisted(state):
        # The Run commit and Task Control close are deliberately separated by
        # one local transaction boundary.  A crash in that window must only
        # finish the original Control record on retry; deriving the gate again
        # from the now-terminal Run would change its durable identity.
        return

    active = state.get("active_agent_invocation")
    if isinstance(active, dict) and active.get("status") in {"running", "resuming"}:
        failed = dict(active)
        failed.update(
            {
                "status": "failed",
                "interruption_observed_at": datetime.now(UTC).isoformat(),
                "error": "session_interrupted",
            }
        )
        state["active_agent_invocation"] = failed
        _sync_invocation_history(state, failed)
    diagnostic: dict[str, Any] = {
        "code": "session_interrupted",
        "message": "Executor Session 已退出；保留现场并等待显式 Resume",
    }
    receipt = state.get("action_application_receipt")
    if (
        isinstance(receipt, dict)
        and isinstance(receipt.get("action_id"), str)
        and type(receipt.get("executor_generation")) is int
    ):
        diagnostic["action_identity"] = {
            "action_id": receipt["action_id"],
            "executor_generation": receipt["executor_generation"],
        }
    try:
        work_subject, subject, _container = current_change_job(state)
    except RequeueError:
        subject = None
    if subject is not None:
        phase = subject.get("phase")
        diagnostic["operator_gate"] = {
            "work_subject": work_subject,
            "action_kind": "execution_failure",
            "phase": (
                phase
                if isinstance(phase, str) and phase
                else str(state.get("status") or "execution_failed")
            ),
            "reason": "session_interrupted",
        }
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "diagnostics": [diagnostic],
        }
    )
    save(state)


def record_operator_stop(
    state: dict[str, Any], *, save: Callable[[dict[str, Any]], object]
) -> None:
    """Persist a reversible operator boundary without discarding run evidence."""

    if state.get("status") == "operator_stopped":
        return
    state["operator_stop"] = {
        "resume_status": state.get("status"),
        "resume_terminal_kind": state.get("terminal_kind"),
        "resume_diagnostics": deepcopy(state.get("diagnostics", [])),
        "stopped_at": datetime.now(UTC).isoformat(),
    }
    active = state.get("active_agent_invocation")
    if isinstance(active, dict) and active.get("status") in {"running", "resuming"}:
        failed = dict(active)
        failed.update(
            {
                "status": "failed",
                "ended_at": datetime.now(UTC).isoformat(),
                "error": "operator_stopped",
            }
        )
        state["active_agent_invocation"] = failed
        _sync_invocation_history(state, failed)
    state.update(
        {
            "status": "operator_stopped",
            "terminal_kind": "operator_stopped",
            "diagnostics": [
                {
                    "code": "operator_stopped",
                    "message": "操作者已停止 Executor；现场已保留，仅显式 Resume 可继续",
                }
            ],
        }
    )
    save(state)


def restore_operator_stop(state: dict[str, Any]) -> None:
    """Consume the reversible Stop boundary for an explicit Resume."""

    stop = state.get("operator_stop")
    if state.get("status") != "operator_stopped" or not isinstance(stop, dict):
        return
    resume_status = stop.get("resume_status")
    if not isinstance(resume_status, str) or resume_status == "operator_stopped":
        raise ValueError("operator_stopped Run 缺少可恢复状态")
    state["status"] = resume_status
    terminal_kind = stop.get("resume_terminal_kind")
    if terminal_kind is None:
        state.pop("terminal_kind", None)
    else:
        state["terminal_kind"] = terminal_kind
    diagnostics = stop.get("resume_diagnostics")
    state["diagnostics"] = deepcopy(diagnostics) if isinstance(diagnostics, list) else []
    state.pop("operator_stop", None)


def session_interruption_is_persisted(state: dict[str, Any]) -> bool:
    diagnostics = state.get("diagnostics")
    receipt = state.get("action_application_receipt")
    if not (
        isinstance(receipt, dict)
        and isinstance(receipt.get("action_id"), str)
        and type(receipt.get("executor_generation")) is int
    ):
        return False
    expected_identity = {
        "action_id": receipt["action_id"],
        "executor_generation": receipt["executor_generation"],
    }
    return (
        state.get("status") == "execution_failed"
        and state.get("terminal_kind") == "execution_failed"
        and isinstance(diagnostics, list)
        and any(
            isinstance(diagnostic, dict)
            and diagnostic.get("code") == "session_interrupted"
            and diagnostic.get("action_identity") == expected_identity
            for diagnostic in diagnostics
        )
    )
