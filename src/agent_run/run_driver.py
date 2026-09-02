"""Direct, typed orchestration for the public ``agent-run run`` lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Protocol

from agent_run.agent_invocation import record_operator_stop
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
from agent_run.executor_host import ExecutorHost
from agent_run.github import GitHubReadError
from agent_run.operator_gate import has_run_operator_gate
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.requeue import close_superseded_pull_request, remove_superseded_worktree
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from agent_run.task_control import TaskControlBusyError


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
class ControlRunOperation:
    """One admitted Stop or Abandon consumed by the shared Run Driver."""

    kind: Literal["stop", "abandon"]
    target_executor: Mapping[str, Any] | None
    discard_worktree: bool = False


@dataclass(frozen=True)
class RunOutcome:
    """The only result shape consumed by :class:`RunDriver`."""

    kind: RunOutcomeKind
    state: dict[str, Any]
    next_step: RunStep | None


class RunController(Protocol):
    def resume(self, run_id: str) -> tuple[dict[str, Any], bool]: ...

    def requeue(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def reject_requeue_after_pr_race(self, run_id: str) -> dict[str, Any]: ...

    def finalize_requeue(self, run_id: str) -> dict[str, Any]: ...

    def record_execution_failure(self, run_id: str, message: str) -> bool: ...

    def record_deterministic_contradiction(
        self, run_id: str, code: str, message: str
    ) -> bool: ...


class _FencedExternal:
    """Check Executor ownership before every call into an external seam."""

    def __init__(self, target: Any, fence: Callable[[], None]) -> None:
        self._target = target
        self._fence = fence

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value):
            return value

        def fenced_call(*args: Any, **kwargs: Any) -> Any:
            self._fence()
            return value(*args, **kwargs)

        return fenced_call


def _fenced_publisher(
    publisher: Any,
    *,
    fenced_git: Any,
    fence: Callable[[], None],
) -> Any:
    """Keep publisher implementations from retaining an unfenced Git seam."""

    if hasattr(publisher, "git"):
        publisher.git = fenced_git
    return _FencedExternal(publisher, fence)


def _fence_controller_dependencies(
    controller: Any,
    *,
    git: Any,
    github: Any,
    fence: Callable[[], None],
) -> None:
    """Apply the same fences to Controller calls made from a Driver step."""

    controller_states = getattr(controller, "states", None)
    set_write_guard = getattr(controller_states, "_set_write_guard", None)
    if callable(set_write_guard) and getattr(
        controller_states, "_write_transaction", None
    ) is None:
        set_write_guard(fence)
    if hasattr(controller, "github"):
        controller.github = github
    publisher = getattr(controller, "publisher", None)
    if publisher is not None and hasattr(publisher, "git"):
        publisher.git = git


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
        before_external_step: Callable[[], None] | None = None,
        resume_pending_refresh: Callable[
            [str], tuple[dict[str, Any], bool]
        ]
        | None = None,
        use_current_state_once: bool = False,
        executor_host: ExecutorHost | None = None,
    ) -> None:
        self.controller: Any = controller
        self.states = states
        self.before_external_step = before_external_step
        self.resume_pending_refresh = resume_pending_refresh
        self.use_current_state_once = use_current_state_once
        self.executor_host = executor_host
        self.git: Any
        self.github_reader: Any
        self._publisher_factory: Callable[[], Any]
        if before_external_step is not None:
            if getattr(states, "_write_transaction", None) is None:
                states._set_write_guard(before_external_step)
            fenced_git = _FencedExternal(git, before_external_step)
            fenced_github_reader = _FencedExternal(
                github_reader, before_external_step
            )
            self.git = fenced_git
            self.github_reader = fenced_github_reader
            self._publisher_factory = lambda: _fenced_publisher(
                publisher_factory(),
                fenced_git=fenced_git,
                fence=before_external_step,
            )
            _fence_controller_dependencies(
                controller,
                git=fenced_git,
                github=fenced_github_reader,
                fence=before_external_step,
            )
            self.controller = _FencedExternal(controller, before_external_step)
        else:
            self.git = git
            self.github_reader = github_reader
            self._publisher_factory = publisher_factory
        self._publisher: Any | None = None
        self.agents = agents
        self.profiles = profiles
        if before_external_step is not None:
            self.agents = _FencedExternal(agents, before_external_step)

    @property
    def publisher(self) -> Any:
        if self._publisher is None:
            self._publisher = self._publisher_factory()
        return self._publisher

    def apply_control(
        self, run_id: str, operation: ControlRunOperation
    ) -> RunOutcome:
        """Apply one fenced control operation without entering automatic progress."""

        if operation.kind not in {"stop", "abandon"}:
            raise ValueError("unsupported control operation")
        if self.before_external_step is not None:
            self.before_external_step()
        if operation.target_executor is not None:
            if self.executor_host is None:
                raise ValueError("Control operation 缺少 Executor Host")
            self.executor_host.terminate_control_target(operation.target_executor)
        state = self.states.load_current_run(run_id)
        if state is None:
            raise ValueError("Control Executor 找不到 Delivery Run")
        if operation.kind == "stop":
            if state.get("status") not in {
                "completed",
                "abandoned",
                "operator_stopped",
            }:
                record_operator_stop(
                    state,
                    save=lambda value: self.states.save_run(run_id, value),
                )
            return self.classify(state)
        if state.get("status") in {"abandoned", "completed"}:
            return self.classify(state)
        repository = self.github_reader.repository()
        default_head = self.git.resolve_base(
            repository.default_branch, repository.default_head_sha
        )
        if state.get("delivery_type") == "parent_only":
            result = ParentDeliveryEngine(
                git=self.git,
                states=self.states,
                github=self.publisher,
                agents=self.agents,
            ).abandon(
                run_id,
                discard_worktree=operation.discard_worktree,
            )
        else:
            result = RunPublicationEngine(
                git=self.git,
                states=self.states,
                agents=self.agents,
                github=self.publisher,
                default_branch=repository.default_branch,
                default_head_sha=default_head,
                currentness_reader=self.github_reader,
            ).abandon(
                run_id,
                discard_worktree=operation.discard_worktree,
            )
        return self.classify(result)

    def deliver(self, run_id: str) -> RunOutcome:
        refreshed, _ = self._refresh(run_id)
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
        refreshed, _ = self._refresh(run_id)
        if _has_pending_stale_dirty_checkout(refreshed):
            refreshed = DeliveryCleanupEngine(
                git=self.git, states=self.states, github=self.publisher
            ).resume(run_id)
            if _has_pending_stale_dirty_checkout(refreshed):
                return RunOutcome(
                    kind=RunOutcomeKind.HUMAN_GATE,
                    state=refreshed,
                    next_step=None,
                )
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
        refreshed, _ = self._refresh(run_id)
        if self._cannot_advance(refreshed):
            return self.classify(refreshed)
        publication = refreshed.get("run_publication")
        if (
            isinstance(publication, dict)
            and publication.get("phase") == "publication_pending"
        ):
            return self.classify(refreshed)
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

    def _refresh(self, run_id: str) -> tuple[dict[str, Any], bool]:
        current = self.states.load_current_run(run_id)
        if (
            self.resume_pending_refresh is not None
            and isinstance(current, dict)
            and is_github_refresh_wait(current)
        ):
            self.use_current_state_once = False
            retry_result = self.resume_pending_refresh(run_id)
            if not is_github_refresh_wait(retry_result[0]):
                self.resume_pending_refresh = None
            return retry_result
        if self.use_current_state_once and isinstance(current, dict):
            self.use_current_state_once = False
            self.resume_pending_refresh = None
            return current, True
        normal_refresh: tuple[dict[str, Any], bool] = self.controller.resume(run_id)
        return normal_refresh

    def approve(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunOutcome:
        refreshed, _ = self._refresh(run_id)
        if self._cannot_advance(refreshed) and refreshed.get("status") not in {
            "run_approval_pending",
            "parent_approval_pending",
            "parent_closeout_pending",
        }:
            return self.classify(refreshed)
        if refreshed.get("delivery_type") == "parent_only":
            parent = ParentDeliveryEngine(
                git=self.git,
                states=self.states,
                github=self.publisher,
                agents=self.agents,
            )
            state = (
                parent.recover_closeout(run_id, prepare_state=prepare_state)
                if refreshed.get("status") == "parent_closeout_pending"
                else parent.approve(run_id, prepare_state=prepare_state)
            )
            return self.classify(state)
        repository = self.github_reader.repository()
        publication = RunPublicationEngine(
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
        state = (
            publication.recover_closeout(run_id, prepare_state=prepare_state)
            if refreshed.get("status") == "parent_closeout_pending"
            else publication.approve(run_id, prepare_state=prepare_state)
        )
        return self.classify(state)

    def revise(
        self,
        run_id: str,
        feedback: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunOutcome:
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
        ).revise(run_id, feedback, prepare_state=prepare_state)
        return self.classify(state)

    def requeue(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunOutcome:
        state, retired = self.controller.requeue(
            run_id, prepare_state=prepare_state
        )
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

        if self.before_external_step is not None:
            self.before_external_step()
        # The direct engine seam is also used by legacy in-process callers
        # that predate the public profile-aware CLI.  Seed only that seam's
        # independent control plane; the CLI passes an existing store and
        # therefore remains fail-closed for old Runs.
        if self.profiles is None:
            AgentProfileStore(self.states.root).initialize(run_id)
        set_run_id = getattr(self.agents, "set_run_id", None)
        if callable(set_run_id):
            set_run_id(run_id)

        if step is RunStep.DELIVER:
            return self.deliver(run_id)
        if step is RunStep.ACCEPT:
            return self.accept(run_id)
        if step is RunStep.PUBLISH:
            return self.publish(run_id)
        return self.requeue(run_id)

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
        } or (
            status == "progress_exhausted"
            and state.get("terminal_kind") == "waiting_human"
        ):
            kind = RunOutcomeKind.HUMAN_GATE
        else:
            kind = RunOutcomeKind.PROGRESS
        return RunOutcome(kind=kind, state=state, next_step=_next_step(state))

    @staticmethod
    def _cannot_advance(state: dict[str, Any]) -> bool:
        return (
            state.get("status") in {"completed", "abandoned"}
            or has_run_operator_gate(state)
            or is_github_refresh_wait(state)
            or _is_currentness_human_blocker(state)
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

    def advance(
        self,
        state: dict[str, Any],
        *,
        control_operation: ControlRunOperation | None = None,
    ) -> dict[str, Any]:
        run_id = state.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("Delivery Run is missing its Run ID")
        if control_operation is not None:
            return self.operations.apply_control(run_id, control_operation).state
        outcome = self.operations.classify(state)
        previous_marker: tuple[object, ...] | None = None
        try:
            while True:
                step = outcome.next_step
                if step is None:
                    return state
                progress_marker_before_dispatch = _progress_marker(state)
                outcome = self.operations.dispatch(step, run_id)
                state = outcome.state
                if (
                    outcome.kind is RunOutcomeKind.PROGRESS
                    and _progress_marker(state) == progress_marker_before_dispatch
                ):
                    recorded = self.operations.controller.record_execution_failure(
                        run_id,
                        "controller_no_progress: Run Controller returned Progress "
                        "without changing its durable progress identity",
                    )
                    if not recorded:
                        return state
                    failed = self.states.load_current_run(run_id)
                    if failed is None:  # pragma: no cover - Controller just wrote it
                        return state
                    return failed
                if outcome.kind is not RunOutcomeKind.EXTERNAL_WAIT:
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
                self._before_external_step()
                self.supervisor.observe(state)
                if state.get("supervision_window") != previous_window:
                    self.states.save_run(run_id, state)
                marker = _external_wait_marker(state)
                credential_wait = isinstance(state.get("credential_availability"), dict)
                if credential_wait or marker == previous_marker:
                    self._before_external_step()
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
                self._before_external_step()
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

        except SimulatedProcessCrash:
            # Fault injection models an abrupt Executor exit.  Do not convert
            # it into a normal Run execution failure: the Host must retain the
            # unresolved ownership record for the next command to reconcile.
            raise
        except TaskControlBusyError:
            # Short Task Control contention is not an execution failure.  The
            # state store retries its own commit boundary; any remaining
            # contention must reach the caller without changing Run status.
            raise
        except (CodexProcessError, OSError, ValueError) as error:
            if not self.operations.controller.record_execution_failure(run_id, str(error)):
                raise
            failed = self.states.load_current_run(run_id)
            if failed is None:  # pragma: no cover - Controller just wrote it
                raise
            return failed

    def _before_external_step(self) -> None:
        fence = getattr(self.operations, "before_external_step", None)
        if callable(fence):
            fence()


def _next_step(state: dict[str, Any]) -> RunStep | None:
    """Select the following step while translating persistent state to a result."""

    if _has_pending_stale_dirty_checkout(state):
        return RunStep.ACCEPT
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
    if status == "publication_pending":
        return None
    if status == "run_publication_pending" or (
        status in {"waiting_checks", "waiting_external"}
        and isinstance(publication, dict)
        and publication.get("phase")
        in {"publication_pending", "waiting_checks", "waiting_external", "ready_for_approval"}
    ):
        return RunStep.PUBLISH
    if status == "waiting_checks":
        return RunStep.DELIVER
    if status == "waiting_external":
        return RunStep.REQUEUE if isinstance(state.get("requeue_transition"), dict) else RunStep.DELIVER
    return None


def _has_pending_stale_dirty_checkout(state: dict[str, Any]) -> bool:
    cleanup = state.get("delivery_cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "cleanup_pending":
        return False
    items = cleanup.get("items")
    return isinstance(items, dict) and any(
        isinstance(item, dict)
        and item.get("status") != "completed"
        and item.get("recovery_kind") == "stale_dirty_checkout"
        for item in items.values()
    )


_PROGRESS_VOLATILE_KEYS = frozenset(
    {
        "diagnostics",
        "last_retry_delay_seconds",
        "latest_observation",
        "last_publication_error",
        "retry_count",
        "timeline",
        "timeline_continuation",
    }
)
_PROGRESS_VOLATILE_FIELDS = {
    "publication_operation_retry": frozenset({"attempts"}),
}


def _progress_marker(state: dict[str, Any]) -> tuple[object, ...]:
    """Return the durable identity used to detect a controller no-op.

    The projection deliberately keeps the state machine's durable business
    fields, including subject/generation/attempt/invocation/candidate/PR/head,
    and cleanup/waiting identities. Timestamps, supervision retry metadata,
    diagnostics, and timeline entries are observations rather than progress.
    """

    next_step = _next_step(state)
    return (
        next_step.value if next_step is not None else None,
        _stable_progress_value(state),
    )


def _external_wait_marker(state: dict[str, Any]) -> tuple[object, ...]:
    """Return the lifecycle identity used to gate repeated external polls.

    External observations and retry bookkeeping are intentionally excluded.
    A phase, attempt, candidate, or pull-request transition still represents
    progress and allows the next poll to run without consuming another retry.
    """

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


def _stable_progress_value(value: object, *, key: str | None = None) -> object:
    if key is not None and (
        key in _PROGRESS_VOLATILE_KEYS or key.endswith("_at")
    ):
        return None
    if isinstance(value, dict):
        volatile_fields = (
            _PROGRESS_VOLATILE_FIELDS.get(key, frozenset())
            if key is not None
            else frozenset()
        )
        return tuple(
            (name, _stable_progress_value(child, key=name))
            for name, child in sorted(value.items())
            if isinstance(name, str)
            and name not in _PROGRESS_VOLATILE_KEYS
            and name not in volatile_fields
            and not name.endswith("_at")
        )
    if isinstance(value, list):
        return tuple(_stable_progress_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _is_currentness_human_blocker(state: dict[str, Any]) -> bool:
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )
