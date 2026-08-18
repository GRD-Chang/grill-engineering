"""Direct, typed orchestration for the public ``agent-run run`` lifecycle."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from agent_run.codex import CodexProcessError
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.external_supervision import (
    ExternalSupervisor,
    clear_supervision_window,
    is_github_refresh_wait,
    is_supervised_wait,
)
from agent_run.github import GitHubReadError
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.requeue import close_superseded_pull_request, remove_superseded_worktree
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore


class RunOutcomeKind(str, Enum):
    PROGRESS = "progress"
    EXTERNAL_WAIT = "external_wait"
    HUMAN_GATE = "human_gate"
    TERMINAL = "terminal"


class RunStep(str, Enum):
    """One typed lifecycle operation selected by the operations boundary."""

    DELIVER = "deliver"
    ACCEPT = "accept-run"
    PUBLISH = "publish-run"
    REQUEUE = "requeue"


@dataclass(frozen=True)
class RunOutcome:
    """The only result shape consumed by :class:`RunDriver`."""

    kind: RunOutcomeKind
    state: dict[str, Any]
    next_step: RunStep | None


class RunController(Protocol):
    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]: ...

    def requeue(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def reject_requeue_after_pr_race(self, run_id: str) -> dict[str, Any]: ...

    def finalize_requeue(self, run_id: str) -> dict[str, Any]: ...

    def record_execution_failure(self, run_id: str, message: str) -> bool: ...


class DirectRunOperations:
    """Internal lifecycle operations; never dispatch a public CLI command."""

    def __init__(
        self,
        *,
        controller: RunController,
        states: StateStore,
        git: Any,
        github_reader: Any,
        publisher_factory: Callable[[], Any],
        agents: Any,
    ) -> None:
        self.controller = controller
        self.states = states
        self.git = git
        self.github_reader = github_reader
        self._publisher_factory = publisher_factory
        self._publisher: Any | None = None
        self.agents = agents

    @property
    def publisher(self) -> Any:
        if self._publisher is None:
            self._publisher = self._publisher_factory()
        return self._publisher

    def deliver(self, run_id: str) -> RunOutcome:
        refreshed, _ = self.controller.resume(run_id)
        if self._cannot_advance(refreshed):
            return self.classify(refreshed)
        refreshed = DeliveryCleanupEngine(
            git=self.git, states=self.states, github=self.publisher
        ).resume(run_id)
        if refreshed.get("delivery_type") == "ticket_run" and isinstance(
            refreshed.get("parent_job"), dict
        ):
            refreshed = ParentDeliveryEngine(
                git=self.git,
                states=self.states,
                github=self.publisher,
                agents=self.agents,
            ).retire_for_child_flow(run_id)
        if refreshed.get("delivery_type") == "parent_only":
            parent_engine = ParentDeliveryEngine(
                git=self.git,
                states=self.states,
                github=self.publisher,
                agents=self.agents,
            )
            state = parent_engine.deliver(run_id)
            if state.get("status") == "parent_closeout_pending":
                state = parent_engine.recover_closeout(run_id)
            if (
                state.get("status") == "parent_approval_pending"
                and parent_engine.has_current_approval_grant(run_id)
            ):
                state = parent_engine.approve(run_id)
        elif (
            refreshed.get("status") == "parent_closeout_pending"
            and isinstance(refreshed.get("run_publication"), dict)
        ):
            repository = self.github_reader.repository()
            state = RunPublicationEngine(
                git=self.git,
                states=self.states,
                agents=self.agents,
                github=self.publisher,
                default_branch=repository.default_branch,
                default_head_sha=self.git.resolve_base(
                    repository.default_branch, repository.default_head_sha
                ),
                currentness_reader=self.github_reader,
            ).recover_closeout(run_id)
        else:
            state = DeliveryRunEngine(
                controller=self.controller,
                tickets=TicketDeliveryEngine(
                    git=self.git,
                    states=self.states,
                    github=self.publisher,
                    agents=self.agents,
                ),
            ).deliver_from_state(run_id, refreshed)
        return self.classify(state)

    def accept(self, run_id: str) -> RunOutcome:
        refreshed, _ = self.controller.resume(run_id)
        if self._cannot_advance(refreshed) or refreshed.get("status") not in {
            "run_acceptance_pending",
            "run_publication_pending",
        }:
            return self.classify(refreshed)
        repository = self.github_reader.repository()
        default_head = self.git.resolve_base(
            repository.default_branch, repository.default_head_sha
        )
        state = RunAcceptanceEngine(
            git=self.git,
            states=self.states,
            agents=self.agents,
            default_head_sha=default_head,
            github=self.publisher,
            currentness_reader=self.github_reader,
        ).accept(run_id)
        return self.classify(state)

    def publish(self, run_id: str) -> RunOutcome:
        refreshed, _ = self.controller.resume(run_id)
        if self._cannot_advance(refreshed):
            return self.classify(refreshed)
        publication = refreshed.get("run_publication")
        eligible = refreshed.get("status") == "run_publication_pending" or (
            isinstance(publication, dict)
            and refreshed.get("status")
            in {
                "publication_pending",
                "waiting_checks",
                "waiting_external",
                "run_approval_pending",
            }
        )
        if not eligible:
            return self.classify(refreshed)
        repository = self.github_reader.repository()
        publication_engine = RunPublicationEngine(
            git=self.git,
            states=self.states,
            agents=self.agents,
            github=self.publisher,
            default_branch=repository.default_branch,
            default_head_sha=self.git.resolve_base(
                repository.default_branch, repository.default_head_sha
            ),
            currentness_reader=self.github_reader,
        )
        if (
            isinstance(publication, dict)
            and publication.get("phase") == "waiting_external"
            and publication_engine.has_current_approval_grant(run_id)
        ):
            return self.classify(publication_engine.approve(run_id))
        state = publication_engine.publish(run_id)
        if (
            state.get("status") == "run_approval_pending"
            and publication_engine.has_current_approval_grant(run_id)
        ):
            state = publication_engine.approve(run_id)
        return self.classify(state)

    def requeue(self, run_id: str) -> RunOutcome:
        state, retired = self.controller.requeue(run_id)
        if is_github_refresh_wait(state):
            return self.classify(state)
        transition = state.get("requeue_transition")
        close_nonce = transition.get("close_nonce") if isinstance(transition, dict) else None
        if not close_superseded_pull_request(self.publisher, retired, close_nonce):
            return self.classify(self.controller.reject_requeue_after_pr_race(run_id))
        remove_superseded_worktree(self.git, self.states.root, run_id, retired)
        state = self.controller.finalize_requeue(run_id)
        subject = str(retired["work_subject"])
        if subject.startswith("ticket:") and state.get("status") == "active":
            state = DeliveryRunEngine(
                controller=self.controller,
                tickets=TicketDeliveryEngine(
                    git=self.git,
                    states=self.states,
                    github=self.publisher,
                    agents=self.agents,
                ),
            ).deliver_from_state(run_id, state)
        elif subject.startswith("parent-only:") and state.get("status") == "parent_delivery_pending":
            state = ParentDeliveryEngine(
                git=self.git,
                states=self.states,
                github=self.publisher,
                agents=self.agents,
            ).deliver(run_id)
        elif subject.startswith("run-repair:") and state.get("status") == "run_acceptance_pending":
            return self.accept(run_id)
        return self.classify(state)

    def dispatch(self, step: RunStep, run_id: str) -> RunOutcome:
        """Execute a typed step; the Driver never dispatches on raw state."""

        return {
            RunStep.DELIVER: self.deliver,
            RunStep.ACCEPT: self.accept,
            RunStep.PUBLISH: self.publish,
            RunStep.REQUEUE: self.requeue,
        }[step](run_id)

    @staticmethod
    def classify(state: dict[str, Any]) -> RunOutcome:
        """Translate mutable stored state at the operations boundary only."""

        status = str(state.get("status"))
        if is_github_refresh_wait(state) or is_supervised_wait(state):
            kind = RunOutcomeKind.EXTERNAL_WAIT
        elif status in {"completed", "abandoned", "execution_failed", "progress_exhausted"}:
            kind = RunOutcomeKind.TERMINAL
        elif _is_currentness_human_blocker(state) or status in {
            "ready_for_human",
            "run_approval_pending",
            "parent_approval_pending",
            "unsupported_scope_change",
            "requeue_required",
        }:
            kind = RunOutcomeKind.HUMAN_GATE
        else:
            kind = RunOutcomeKind.PROGRESS
        return RunOutcome(kind=kind, state=state, next_step=_next_step(state))

    @staticmethod
    def _cannot_advance(state: dict[str, Any]) -> bool:
        return (
            state.get("status") in {"completed", "abandoned", "requeue_required"}
            or is_github_refresh_wait(state)
            or _is_currentness_human_blocker(state)
            or state.get("status") == "unsupported_scope_change"
        )


class RunDriver:
    """Advance a Delivery Run through direct internal operations until a boundary."""

    def __init__(
        self,
        *,
        operations: DirectRunOperations,
        states: StateStore,
        supervisor: ExternalSupervisor,
    ) -> None:
        self.operations = operations
        self.states = states
        self.supervisor = supervisor

    def advance(self, state: dict[str, Any]) -> dict[str, Any]:
        run_id = state.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("Delivery Run is missing its Run ID")
        outcome = self.operations.classify(state)
        previous_marker: tuple[object, ...] | None = None
        try:
            while True:
                step = outcome.next_step
                if step is None:
                    return state
                outcome = self.operations.dispatch(step, run_id)
                state = outcome.state
                if outcome.kind is not RunOutcomeKind.EXTERNAL_WAIT:
                    previous_marker = None
                    if clear_supervision_window(state):
                        self.states.save_run(run_id, state)
                    if outcome.kind in {
                        RunOutcomeKind.HUMAN_GATE,
                        RunOutcomeKind.TERMINAL,
                    }:
                        return state
                    continue
                previous_window = state.get("supervision_window")
                self.supervisor.observe(state)
                if state.get("supervision_window") != previous_window:
                    self.states.save_run(run_id, state)
                marker = _progress_marker(state)
                credential_wait = isinstance(state.get("credential_availability"), dict)
                if credential_wait or marker == previous_marker:
                    if not self.supervisor.before_retry(state):
                        self.states.save_run(run_id, state)
                        return state
                    if credential_wait:
                        # Credential availability publishes its retry count
                        # alongside the durable deadline.  Saving every
                        # bounded retry also lets a restarted CLI report the
                        # same sanitized state it would have observed in this
                        # foreground invocation.
                        self.states.save_run(run_id, state)
                previous_marker = marker
                print(f"推进: {state['status']} → {step.value}", file=sys.stderr)
        except (CodexProcessError, GitHubReadError, OSError, ValueError) as error:
            if not self.operations.controller.record_execution_failure(run_id, str(error)):
                raise
            failed = self.states.load_current_run(run_id)
            if failed is None:  # pragma: no cover - Controller just wrote it
                raise
            return failed


def _next_step(state: dict[str, Any]) -> RunStep | None:
    """Select the following step while translating persistent state to a result."""

    status = str(state.get("status"))
    if status in {
        "active",
        "ticket_completed",
        "parent_delivery_pending",
        "parent_closeout_pending",
        "waiting_merge",
    }:
        return RunStep.DELIVER
    if status == "run_acceptance_pending":
        return RunStep.ACCEPT
    publication = state.get("run_publication")
    if status == "run_publication_pending" or (
        status in {"publication_pending", "waiting_checks", "waiting_external"}
        and isinstance(publication, dict)
        and publication.get("phase")
        in {"publication_pending", "waiting_checks", "waiting_external", "ready_for_approval"}
    ):
        return RunStep.PUBLISH
    if status in {"publication_pending", "waiting_checks"}:
        return RunStep.DELIVER
    if status == "waiting_external":
        return RunStep.REQUEUE if isinstance(state.get("requeue_transition"), dict) else RunStep.DELIVER
    return None


def _progress_marker(state: dict[str, Any]) -> tuple[object, ...]:
    active = state.get("active_ticket_job")
    parent = state.get("parent_job")
    acceptance = state.get("run_acceptance")
    publication = state.get("run_publication")
    return (
        state.get("status"),
        active.get("phase") if isinstance(active, dict) else None,
        active.get("modification_attempts") if isinstance(active, dict) else None,
        active.get("validation_attempts") if isinstance(active, dict) else None,
        active.get("publication_attempts") if isinstance(active, dict) else None,
        active.get("pull_number") if isinstance(active, dict) else None,
        parent.get("phase") if isinstance(parent, dict) else None,
        parent.get("modification_attempts") if isinstance(parent, dict) else None,
        parent.get("validation_attempts") if isinstance(parent, dict) else None,
        parent.get("publication_attempts") if isinstance(parent, dict) else None,
        parent.get("pr_number") if isinstance(parent, dict) else None,
        acceptance.get("phase") if isinstance(acceptance, dict) else None,
        acceptance.get("validation_attempts") if isinstance(acceptance, dict) else None,
        publication.get("phase") if isinstance(publication, dict) else None,
        publication.get("publication_attempts") if isinstance(publication, dict) else None,
        publication.get("pr_number") if isinstance(publication, dict) else None,
    )


def _is_currentness_human_blocker(state: dict[str, Any]) -> bool:
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )
