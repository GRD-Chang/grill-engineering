"""Actual outcomes of one explicit Resume, kept outside business state."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_run.notifications import Notifications

_BASELINE_FIELDS = ("status", "diagnostics", "active_agent_invocation", "human_blockers",
                    "parent_job", "ticket_jobs", "run_acceptance")

_FAILURES = {"execution_failed", "supervision_timeout", "deterministic_contradiction"}
_HUMAN = {"ready_for_human", "blocked", "requeue_required", "unsupported_scope_change",
          "run_approval_pending", "parent_approval_pending", "publication_pending",
          "progress_exhausted", "abandonment_pending"}


class ResumeFeedback:
    """A finite observer owned by the CLI or its actual Executor.

    Request receipt and Invocation preparation are deliberately not progress.
    The notification projection reuses actionable boundary identities, so a later
    ordinary observation cannot duplicate the same failure or human task.
    """

    def __init__(self, root: Path, state: dict[str, Any],
                 load: Callable[[], dict[str, Any] | None]) -> None:
        self.root = root
        self.state = state
        self.baseline = deepcopy({key: state.get(key) for key in _BASELINE_FIELDS})
        self.load = load
        self.identity = uuid4().hex
        self.result: dict[str, Any] | None = None
        self.sender: Notifications | None = None
        self.executor_owned = False
        self.suppressed = False

    def attach(self, sender: Notifications, action_id: str | None) -> None:
        self.sender = sender
        self.executor_owned = True
        if action_id:
            self.identity = action_id

    def _result(self, outcome: str, evidence: str, reason: str = "") -> None:
        if not reason and self.result is not None and self.result.get("outcome") == outcome:
            reason = str(self.result.get("reason") or "")
        self.result = {"id": self.identity, "outcome": outcome,
                       "evidence": evidence, "reason": reason}

    def failure(self, reason: str, *, evidence: str = "preflight") -> None:
        self._result("failed", evidence, reason)

    def project(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**state, "_manual_resume_result": self.result} if self.result else state

    def observe(self, state: dict[str, Any], *, outcome_observed: bool = False) -> None:
        self.state = state
        status = state.get("status")
        fresh = outcome_observed or any(state.get(key) != self.baseline.get(key) for key in _BASELINE_FIELDS)
        if fresh and status in _FAILURES:
            self._result("failed", "execution")
        elif fresh and status in _HUMAN:
            self._result("human_action", "controller_boundary")
        invocation = state.get("active_agent_invocation")
        previous = self.baseline.get("active_agent_invocation") or {}
        if (self.result is None and isinstance(invocation, dict)
                and invocation.get("reported_thread_id")
                and invocation.get("status") == "running"
                and invocation.get("started_at") != previous.get("started_at")):
            self._result("started", "worker_started")
        if self.sender is not None:
            self.sender.observe(self.project(state))

    def progressed(self, state: dict[str, Any], kind: str) -> None:
        if self.result is None and kind in {"progress", "external_wait", "terminal_completion"}:
            self._result("started", "controller_check" if kind == "external_wait" else "controller_progress")
        self.observe(state, outcome_observed=True)

    def close(self) -> None:
        if self.suppressed or self.executor_owned or self.result is None:
            return
        # Front-end failures can occur before a business Executor exists. Only
        # a successfully selected valid Run supplies a recipient and language.
        try:
            state = self.load() or self.state
            sender = Notifications(self.root, self.project(state))
            sender.close()
        except Exception:
            # Notification failures must not replace the CLI's actual outcome.
            pass
