"""Actual outcomes of one explicit Resume, kept outside business state."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_run.execution_timing import invocation_started, parse_optional_timestamp
from agent_run.notifications import Notifications
from agent_run.operator_action_facts import operator_action_facts

_BASELINE_FIELDS = ("status", "diagnostics", "active_agent_invocation", "human_blockers",
                    "parent_job", "ticket_jobs", "ticket_agent_invocations", "run_acceptance", "run_publication")


class ResumeFeedback:
    """A finite observer owned by the CLI or its actual Executor.

    Request receipt and Invocation preparation are deliberately not progress.
    The notification projection reuses actionable boundary identities, so a later
    ordinary observation cannot duplicate the same failure or human task.
    """

    def __init__(self, root: Path, state: dict[str, Any],
                 load: Callable[[], dict[str, Any] | None], *,
                 ticket_number: int | None = None) -> None:
        self.ticket_number = ticket_number
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
                       "evidence": evidence, "reason": reason,
                       "ticket_number": self.ticket_number, "work_subject": self.work_subject}

    def failure(self, reason: str, *, evidence: str = "preflight") -> None:
        self._result("failed", evidence, reason)

    def project(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**state, "_manual_resume_result": self.result} if self.result else state

    def observe(self, state: dict[str, Any], *, outcome_observed: bool = False) -> None:
        self.state = state
        fresh = outcome_observed or any(state.get(key) != self.baseline.get(key) for key in _BASELINE_FIELDS)
        gates = operator_action_facts(state)
        relevant = [item for item in gates if self.work_subject is None
                    or item["work_subject"] == self.work_subject
                    or (item["scope"] == "global" and not item["work_subject"].startswith("ticket:"))]
        gate = next((item for item in relevant if item["scope"] == "global"), None)
        if gate is None:
            gate = next((item for item in relevant if item["scope"] == "local"
                         and item["subject"] != (self.baseline.get("ticket_jobs") or {}).get(
                             item["work_subject"].removeprefix("ticket:"))), None)
        if fresh and gate is not None and gate["action_kind"] != "operator_stopped":
            failed = gate["action_kind"] in {"execution_failure", "supervision_timeout", "deterministic_contradiction"}
            self._result("failed" if failed else "human_action", "execution" if failed else "controller_boundary")
            assert self.result is not None
            self.result.update({key: gate[key] for key in ("work_subject", "scope", "action_kind", "generation")})
            self.result["ticket_number"] = (
                int(gate["work_subject"].removeprefix("ticket:"))
                if gate["work_subject"].startswith("ticket:") else None
            )
        if self.result is None and self.ticket_number is not None:
            target = (state.get("ticket_jobs") or {}).get(str(self.ticket_number), {})
            previous_target = (self.baseline.get("ticket_jobs") or {}).get(str(self.ticket_number), {})
            observed = target.get("publication_observed_at")
            if (parse_optional_timestamp(observed) is not None
                    and observed != previous_target.get("publication_observed_at")):
                self._result("started", "controller_check")
        invocations = list((state.get("ticket_agent_invocations") or {}).values())
        invocations.append(state.get("active_agent_invocation"))
        previous = list((self.baseline.get("ticket_agent_invocations") or {}).values())
        previous.append(self.baseline.get("active_agent_invocation"))
        old_starts = {(item.get("work_subject"), item.get("started_at"))
                      for item in previous if isinstance(item, dict) and invocation_started(item)}
        if self.result is None and any(
            isinstance(item, dict) and invocation_started(item)
            and item.get("status") == "running"
            and (self.ticket_number is None or item.get("work_subject") == f"ticket:{self.ticket_number}")
            and (item.get("work_subject"), item.get("started_at")) not in old_starts
            for item in invocations
        ):
            self._result("started", "worker_started")
        if self.sender is not None:
            self.sender.observe(self.project(state))

    def progressed(self, state: dict[str, Any], kind: str) -> None:
        target = (state.get("ticket_jobs") or {}).get(str(self.ticket_number), {})
        if (self.result is None and kind in {"progress", "external_wait", "terminal_completion"}
                and (self.ticket_number is None or target.get("phase") == "completed")):
            self._result("started", "controller_check" if kind == "external_wait" else "controller_progress")
        self.observe(state, outcome_observed=True)

    def close(self) -> None:
        if self.suppressed or self.executor_owned or self.result is None:
            return
        # Front-end failures can occur before a business Executor exists. Only
        # a successfully selected valid Run supplies a recipient and language.
        try:
            state = self.load() or self.state
            sender = Notifications(self.root, self.project(state),
                                   load_state=lambda: self.project(current) if (current := self.load()) else None)
            sender.close()
        except Exception:
            # Notification failures must not replace the CLI's actual outcome.
            pass
