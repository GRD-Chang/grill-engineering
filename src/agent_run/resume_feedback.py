"""Actual outcomes of one explicit Resume, kept outside business state."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_run.notifications import Notifications
from agent_run.operator_action_facts import operator_action_facts

_BASELINE_FIELDS = ("status", "diagnostics", "active_agent_invocation", "human_blockers",
                    "parent_job", "ticket_jobs", "run_acceptance", "run_publication")


class ResumeFeedback:
    """A finite observer owned by the CLI or its actual Executor.

    Request receipt and Invocation preparation are deliberately not progress.
    The notification projection reuses actionable boundary identities, so a later
    ordinary observation cannot duplicate the same failure or human task.
    """

    def __init__(self, root: Path, state: dict[str, Any],
                 load: Callable[[], dict[str, Any] | None], *, ticket_number: int | None = None) -> None:
        self.root = root
        self.state = state
        self.baseline = deepcopy({key: state.get(key) for key in _BASELINE_FIELDS})
        self.load = load
        self.identity = uuid4().hex
        self.result: dict[str, Any] | None = None
        self.sender: Notifications | None = None
        self.executor_owned = False
        self.suppressed = False
        self.work_subject = f"ticket:{ticket_number}" if ticket_number is not None else None

    def attach(self, sender: Notifications, action_id: str | None) -> None:
        self.sender = sender
        self.executor_owned = True
        if action_id:
            self.identity = action_id

    def _result(self, outcome: str, evidence: str, reason: str = "") -> None:
        if not reason and self.result is not None and self.result.get("outcome") == outcome:
            reason = str(self.result.get("reason") or "")
        self.result = {"id": self.identity, "outcome": outcome,
                       "evidence": evidence, "reason": reason, "work_subject": self.work_subject}

    def failure(self, reason: str, *, evidence: str = "preflight") -> None:
        self._result("failed", evidence, reason)

    def project(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**state, "_manual_resume_result": self.result} if self.result else state

    def observe(self, state: dict[str, Any], *, outcome_observed: bool = False) -> None:
        self.state = state
        fresh = outcome_observed or any(state.get(key) != self.baseline.get(key) for key in _BASELINE_FIELDS)
        gates = operator_action_facts(state)
        gate = next((item for item in gates if item["work_subject"] == self.work_subject), None) if self.work_subject else (
            next((item for item in gates if item["scope"] == "global"), None)
        )
        if gate is None and self.work_subject is None and len(gates) == 1 and state.get("status") not in {
            "active", "ticket_delivery_pending", "waiting_checks", "waiting_external",
        }:
            gate = gates[0]
        if fresh and gate is not None and gate["action_kind"] != "operator_stopped":
            failed = gate["action_kind"] in {"execution_failure", "supervision_timeout", "deterministic_contradiction"}
            self._result("failed" if failed else "human_action", "execution" if failed else "controller_boundary")
            assert self.result is not None
            self.result["work_subject"] = gate["work_subject"]
        invocation = state.get("active_agent_invocation")
        previous = self.baseline.get("active_agent_invocation") or {}
        if (self.result is None and isinstance(invocation, dict)
                and invocation.get("reported_thread_id")
                and (self.work_subject is None or invocation.get("work_subject") == self.work_subject)
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
