"""Direct, typed orchestration for the public ``agent-run run`` lifecycle."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from agent_run.agent_profiles import AgentProfileStore
from agent_run.codex import CodexProcessError
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.external_supervision import (
    ExternalSupervisor,
    clear_supervision_window,
    is_github_refresh_wait,
    is_supervised_wait,
    is_proven_github_state_contradiction,
    public_supervision_snapshot,
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
    EXECUTION_FAILURE = "execution_failure"
    DETERMINISTIC_CONTRADICTION = "deterministic_contradiction"
    REQUEUE_REQUIRED = "requeue_required"
    TERMINAL_COMPLETION = "terminal_completion"
    TERMINAL_ABANDONMENT = "terminal_abandonment"


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

    def record_deterministic_contradiction(
        self, run_id: str, code: str, message: str
    ) -> bool: ...


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
        profiles: AgentProfileStore | None = None,
    ) -> None:
        self.controller = controller
        self.states = states
        self.git = git
        self.github_reader = github_reader
        self._publisher_factory = publisher_factory
        self._publisher: Any | None = None
        self.agents = agents
        self.profiles = profiles

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
        acceptance = refreshed.get("run_acceptance")
        repair_wait = (
            isinstance(acceptance, dict)
            and acceptance.get("phase") == "repairing"
            and isinstance(acceptance.get("repair_job"), dict)
            and refreshed.get("status")
            in {"waiting_checks", "waiting_external", "waiting_merge"}
        )
        if self._cannot_advance(refreshed) or (
            refreshed.get("status")
            not in {"run_acceptance_pending", "run_publication_pending"}
            and not repair_wait
        ):
            return self.classify(refreshed)
        return self._accept_current_run(run_id)

    def _accept_current_run(self, run_id: str) -> RunOutcome:
        """Run Fresh Acceptance for an already eligible current Run."""

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
            and publication.get("phase") in {"waiting_checks", "waiting_external"}
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
            return self._accept_current_run(run_id)
        return self.classify(state)

    def dispatch(self, step: RunStep, run_id: str) -> RunOutcome:
        """Execute a typed step; the Driver never dispatches on raw state."""

        # The direct engine seam is also used by legacy in-process callers
        # that predate the public profile-aware CLI.  Seed only that seam's
        # independent control plane; the CLI passes an existing store and
        # therefore remains fail-closed for old Runs.
        if self.profiles is None:
            AgentProfileStore(self.states.root).initialize(run_id)
        set_run_id = getattr(self.agents, "set_run_id", None)
        if callable(set_run_id):
            set_run_id(run_id)

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
        elif status == "execution_failed":
            kind = RunOutcomeKind.EXECUTION_FAILURE
        elif status in {"unsupported_scope_change", "deterministic_contradiction"}:
            kind = RunOutcomeKind.DETERMINISTIC_CONTRADICTION
        elif status == "requeue_required":
            kind = RunOutcomeKind.REQUEUE_REQUIRED
        elif status == "completed":
            kind = RunOutcomeKind.TERMINAL_COMPLETION
        elif status == "abandoned":
            kind = RunOutcomeKind.TERMINAL_ABANDONMENT
        elif _is_currentness_human_blocker(state) or status in {
            "ready_for_human",
            "run_approval_pending",
            "parent_approval_pending",
            "progress_exhausted",
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
            or state.get("status")
            in {"unsupported_scope_change", "deterministic_contradiction"}
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
                        RunOutcomeKind.EXECUTION_FAILURE,
                        RunOutcomeKind.DETERMINISTIC_CONTRADICTION,
                        RunOutcomeKind.REQUEUE_REQUIRED,
                        RunOutcomeKind.TERMINAL_COMPLETION,
                        RunOutcomeKind.TERMINAL_ABANDONMENT,
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
                    if not self.supervisor.before_retry(
                        state,
                        persist_before_sleep=lambda: self.states.save_run(run_id, state),
                    ):
                        self.states.save_run(run_id, state)
                        return state
                    # Persist each throttled retry so concurrent status/history
                    # reads observe the same durable wait contract.
                    self.states.save_run(run_id, state)
                previous_marker = marker
                wait = public_supervision_snapshot(state, now=self.supervisor.now())
                if wait is None:  # pragma: no cover - external waits always create one
                    print(f"推进: {state['status']} → {step.value}", file=sys.stderr)
                else:
                    print(
                        "等待进度: "
                        f"kind={wait['kind']} subject={wait['subject']} "
                        f"started_at={wait['started_at']} deadline={wait['deadline']} "
                        f"remaining_seconds={wait['remaining_seconds']} "
                        f"retries={wait['retry_count']} "
                        f"observation={wait['latest_observation']} "
                        f"next_action={wait['next_action']}",
                        file=sys.stderr,
                    )
        except GitHubReadError as error:
            if is_proven_github_state_contradiction(error.code):
                recorded = self.operations.controller.record_deterministic_contradiction(
                    run_id, error.code, error.message
                )
            else:
                recorded = self.operations.controller.record_execution_failure(
                    run_id, str(error)
                )
            if not recorded:
                raise
            failed = self.states.load_current_run(run_id)
            if failed is None:  # pragma: no cover - Controller just wrote it
                raise
            return failed
        except (CodexProcessError, OSError, ValueError) as error:
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
    }:
        return RunStep.DELIVER
    if status == "run_acceptance_pending":
        return RunStep.ACCEPT
    acceptance = state.get("run_acceptance")
    if (
        status in {"waiting_checks", "waiting_external", "waiting_merge"}
        and isinstance(acceptance, dict)
        and acceptance.get("phase") == "repairing"
        and isinstance(acceptance.get("repair_job"), dict)
    ):
        return RunStep.ACCEPT
    if status == "waiting_merge":
        return RunStep.DELIVER
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
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
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
        repair.get("phase") if isinstance(repair, dict) else None,
        repair.get("modification_attempts") if isinstance(repair, dict) else None,
        repair.get("validation_attempts") if isinstance(repair, dict) else None,
        repair.get("publication_attempts") if isinstance(repair, dict) else None,
        repair.get("pr_number") if isinstance(repair, dict) else None,
        publication.get("phase") if isinstance(publication, dict) else None,
        publication.get("publication_attempts") if isinstance(publication, dict) else None,
        publication.get("pr_number") if isinstance(publication, dict) else None,
    )


def _is_currentness_human_blocker(state: dict[str, Any]) -> bool:
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )
