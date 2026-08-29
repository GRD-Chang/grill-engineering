from __future__ import annotations

import hashlib
import secrets
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_run.agent_profiles import AgentProfileStore
from agent_run.agent_invocation import fail_interrupted_invocation
from agent_run.change_currentness import (
    candidate_or_acceptance_is_inconsistent,
    has_currentness_facts,
    stale_change_job_reason,
    unknown_pr_mutation,
)
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.delivery_policy import (
    DELIVERY_POLICY_PROTOCOL,
    DeliveryPolicy,
    default_delivery_policy,
    ticket_budget_policy_for_job,
)
from agent_run.error_safety import bounded_error
from agent_run.graph import state_from_graph
from agent_run.human_responses import append_human_response
from agent_run.git import GitError, GitRepository, Publisher
from agent_run.github import GitHubReadError
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    is_github_refresh_wait,
    restore_supervision_wait,
    supervision_window_matches,
    waiting_boundary,
    wait_for_github_convergence,
    wait_for_github_refresh,
)
from agent_run.models import DeliveryGraph, Repository
from agent_run.requeue import RequeueError, current_change_job, requeue_change_job
from agent_run.run_currentness import (
    invalidate_run_acceptance,
    invalidate_stale_run_repair,
    ticket_completion_records,
    ticket_completion_records_fingerprint,
)
from agent_run.run_locator import RunLocatorError, RunLocatorIndex
from agent_run.review_budget import (
    RUN_POLICY,
    budget_checkpoint_subjects,
    reset_budget,
)
from agent_run.requeue_supervision import (
    refresh_requeue_transition_facts,
    wait_for_recoverable_github_read,
)
from agent_run.resume_audit import append_explicit_resume_audit
from agent_run.scope_changes import reconcile_structure
from agent_run.semantic_attempt import invocation_attempt_is_pending
from agent_run.state import StateStore
from agent_run.state_contract import (
    IncompatibleRunStateError,
    human_blocker_subject_count,
    require_current_run_state,
)


class GitHubReader(Protocol):
    def repository(self) -> Repository: ...

    def delivery_graph(self, parent_number: int) -> DeliveryGraph: ...

    def live_pull_request(self, pr_number: int) -> dict[str, Any]: ...


class Controller:
    def __init__(
        self,
        github: GitHubReader,
        git: GitRepository,
        states: StateStore,
        locator: RunLocatorIndex | None = None,
        profiles: AgentProfileStore | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        delivery_policy_provider: Callable[[], DeliveryPolicy] | None = None,
    ) -> None:
        self.github = github
        self.states = states
        self.checkout = git.root
        self.publisher = Publisher(git)
        self.locator = locator
        self.profiles = profiles
        self.delivery_policy = delivery_policy or default_delivery_policy()
        self.delivery_policy_provider = delivery_policy_provider

    def start(
        self, parent_number: int, *, reuse_existing: bool = True
    ) -> tuple[dict[str, Any], bool]:
        try:
            repository = self.github.repository()
        except GitHubReadError as error:
            return self._wait_for_initial_repository(
                parent_number, error, unfinished_only=not reuse_existing
            )
        with self.states.locked():
            existing = (
                self.states.find_run(repository.name_with_owner, parent_number)
                if reuse_existing
                else None
            )
            return self._start_locked(repository, parent_number, existing)

    def start_or_resume_unfinished(
        self, parent_number: int
    ) -> tuple[dict[str, Any], bool]:
        """Atomically select the one live Run for the foreground `run` command."""
        try:
            repository = self.github.repository()
        except GitHubReadError as error:
            return self._wait_for_initial_repository(
                parent_number, error, unfinished_only=True
            )
        with self.states.locked():
            unfinished = self.states.find_unfinished_runs(
                repository.name_with_owner, parent_number
            )
            if len(unfinished) > 1:
                run_ids = ", ".join(str(state["run_id"]) for state in unfinished)
                raise ValueError(
                    "multiple unfinished Delivery Runs exist for this Parent Issue: "
                    f"{run_ids}"
                )
            existing = unfinished[0] if unfinished else None
            return self._start_locked(repository, parent_number, existing)

    def _wait_for_initial_repository(
        self,
        parent_number: int,
        error: GitHubReadError,
        *,
        unfinished_only: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Persist the first GitHub repository read as a resumable wait."""

        if not is_github_convergence_error(error.code):
            raise error
        hint_reader = getattr(self.github, "repository_hint", None)
        repository_hint = hint_reader() if callable(hint_reader) else None
        if not isinstance(repository_hint, str) or not repository_hint:
            raise error
        with self.states.locked():
            if unfinished_only:
                candidates = self.states.find_unfinished_runs(
                    repository_hint, parent_number
                )
                if len(candidates) > 1:
                    run_ids = ", ".join(str(state["run_id"]) for state in candidates)
                    raise ValueError(
                        "multiple unfinished Delivery Runs exist for this Parent Issue: "
                        f"{run_ids}"
                    )
                existing = candidates[0] if candidates else None
            else:
                existing = self.states.find_run(repository_hint, parent_number)
            if existing is not None:
                require_current_run_state(existing)
                self._require_current_checkout(existing)
            resumed = existing is not None
            if existing is None:
                provisional = Repository(
                    name_with_owner=repository_hint,
                    default_branch="HEAD",
                    default_head_sha=None,
                )
                run_id = self._available_run_id(
                    repository_hint, parent_number, "repository-pending"
                )
                existing = self._initial_state(
                    provisional,
                    parent_number,
                    run_id,
                    "repository-pending",
                    delivery_policy=self._policy_for_new_run(),
                )
                existing["base_resolution_pending"] = True
                existing["repository_binding_pending"] = True
            wait_for_github_convergence(
                existing,
                code=error.code,
                message=error.message,
                waiting_for="GitHub repository binding",
            )
            ensure_supervision_window(existing)
            existing["updated_at"] = _now()
            self.states.save_run(str(existing["run_id"]), existing)
            self._initialize_direct_profile(existing)
            self._register_pending_locator(existing)
            return existing, resumed

    def unfinished_runs(self, parent_number: int) -> list[dict[str, Any]]:
        repository = self.github.repository()
        return self.states.find_unfinished_runs(
            repository.name_with_owner, parent_number
        )

    def resume(
        self,
        run_id: str,
        *,
        resume_human_blocker: bool = False,
        new_thread: bool = False,
        human_response: str | None = None,
        message: str | None = None,
        explicit_resume: bool = False,
        resume_budget_checkpoint: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        with self.states.locked():
            existing = self._load_run(run_id)
            budget_policy = self.delivery_policy
            if resume_budget_checkpoint and budget_checkpoint_subjects(existing):
                budget_policy = self._policy_for_new_run()
            try:
                existing = self._load_bound_run(run_id, state=existing)
            except GitHubReadError as error:
                if not is_github_convergence_error(error.code):
                    raise
                if explicit_resume:
                    append_explicit_resume_audit(
                        existing,
                        new_thread=new_thread,
                        human_response_supplied=(
                            human_response is not None or message is not None
                        ),
                    )
                wait_for_github_refresh(
                    existing,
                    code=error.code,
                    message=error.message,
                    waiting_for="GitHub repository binding",
                )
                existing["updated_at"] = _now()
                self.states.save_run(run_id, existing)
                return existing, True
            if existing.get("status") in {
                "abandoned",
                "abandonment_pending",
                "completed",
                "deterministic_contradiction",
            }:
                return existing, True
            if explicit_resume:
                append_explicit_resume_audit(
                    existing,
                    new_thread=new_thread,
                    human_response_supplied=(
                        human_response is not None or message is not None
                    ),
                )
            resuming_supervision_timeout = existing.get("status") == "supervision_timeout"
            existing_invocation = existing.get("active_agent_invocation")
            resume_completed_invocation = (
                existing.get("status") == "execution_failed"
                and isinstance(existing_invocation, dict)
                and existing_invocation.get("status") == "completed"
                and invocation_attempt_is_pending(existing, existing_invocation)
            )
            if resuming_supervision_timeout:
                if new_thread or human_response is not None or message is not None:
                    raise ValueError(
                        "supervision timeout resume does not accept Agent or Human Blocker options"
                    )
                restore_supervision_wait(existing)
            parent = _state_mapping(existing, "parent")
            parent_number = int(parent["number"])
            if existing.get("base_resolution_pending") is True:
                try:
                    repository = self.github.repository()
                except GitHubReadError as error:
                    if not is_github_convergence_error(error.code):
                        raise
                    wait_for_github_refresh(
                        existing,
                        code=error.code,
                        message=error.message,
                        waiting_for="GitHub repository binding",
                    )
                    existing["updated_at"] = _now()
                    self.states.save_run(run_id, existing)
                    return existing, True
                return self._start_locked(repository, parent_number, existing)
            base = _state_mapping(existing, "base")
            base_sha = str(base["sha"])
            state = self._refresh(existing, parent_number)
            if is_github_refresh_wait(state):
                self.states.save_run(run_id, state)
                return state, True
            if state.get("status") in {
                "unsupported_scope_change",
                "deterministic_contradiction",
            } or (
                state.get("status") == "execution_failed"
                and (
                    existing.get("status") != "execution_failed"
                    or state.get("diagnostics") != existing.get("diagnostics")
                )
            ):
                self.states.save_run(run_id, state)
                return state, True
            if state.get("status") == "requeue_required":
                self.states.save_run(run_id, state)
                return state, True
            if state.get("terminal_kind") == "run_acceptance_stale":
                # Refresh retired the exact blocked/failed Run Repair Attempt.
                # The supplied Human response or Thread choice belongs to that
                # stale identity and must not be applied to fresh Acceptance.
                self.states.save_run(run_id, state)
                return state, True
            if _is_currentness_human_blocker(state):
                # A live Change PR no longer matches the persisted
                # Generation. Do not repair invocations or touch a managed
                # branch while the maintainer decides how to resolve it.
                self.states.save_run(run_id, state)
                return state, True
            invocation = state.get("active_agent_invocation")
            if (
                isinstance(invocation, dict)
                and invocation.get("role") in {"reviewer", "final_publication"}
                and not self._run_invocation_boundary_is_current(state, invocation)
            ):
                _invalidate_stale_run_invocation(state, invocation)
                self._ensure_delivery_branch(state, base_sha)
                self.states.save_run(run_id, state)
                return state, True
            if message is not None:
                if human_response is not None:
                    raise ValueError("pass only one Human Blocker response")
                human_response = message
            if human_response is not None:
                human_response = _validated_human_response(human_response)
            resuming_run_acceptance = False
            budget_resumed = (
                _resume_review_budget_window(
                    state,
                    budget_policy,
                )
                if resume_budget_checkpoint
                else False
            )
            if budget_resumed and human_response is not None:
                raise ValueError("Review Budget resume does not accept a Human Blocker response")
            if budget_resumed:
                resumed_subject = True
            elif resume_human_blocker:
                resuming_run_acceptance = _run_acceptance_human_blocker(state)
                resumed_subject = _resume_agent_human_blocker(state, human_response)
                if human_response is not None and not resumed_subject:
                    raise ValueError("--message requires a current Human Blocker")
            elif human_response is not None:
                raise ValueError("--message requires Human Blocker resume")
            if new_thread and not budget_resumed:
                if resuming_run_acceptance:
                    _state_mapping(state, "run_acceptance")["reviewer_new_thread"] = True
                else:
                    _clear_current_invocation_thread(state)
            else:
                _restore_current_invocation_thread(
                    state, allow_completed=resume_completed_invocation
                )
            _mark_failed_invocation_resuming(state)
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            self._initialize_direct_profile(state)
            return state, True

    def requeue(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create a blank Change Job Generation from facts read *now*.

        Requeue is intentionally separate from ``resume``: it never tries to
        attach an old Thread or retain candidate/acceptance state.
        """
        with self.states.locked():
            try:
                existing = self._load_bound_run(run_id)
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    waiting_for="GitHub repository binding",
                ), {}
            parent_number = int(_state_mapping(existing, "parent")["number"])
            transition = existing.get("requeue_transition")
            if isinstance(transition, dict):
                retired = transition.get("retired")
                if isinstance(retired, dict):
                    refreshed = refresh_requeue_transition_facts(
                        existing, parent_number, self.github, now=_now
                    )
                    if is_github_refresh_wait(refreshed):
                        self.states.save_run(run_id, refreshed)
                        return refreshed, retired
                    if refreshed.get("status") != "requeue_required":
                        self.states.save_run(run_id, refreshed)
                        raise RequeueError(
                            "requeue transition no longer has a current Change Job"
                        )
                    self.states.save_run(run_id, refreshed)
                    return refreshed, retired
            state = self._refresh(
                existing, parent_number, check_requeue_currentness=True
            )
            if is_github_refresh_wait(state):
                self.states.save_run(run_id, state)
                return state, {}
            if state.get("status") != "requeue_required":
                self.states.save_run(run_id, state)
                raise RequeueError("requeue is only allowed in requeue_required state")
            try:
                repository = self.github.repository()
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    state=state,
                    waiting_for="GitHub requeue base refresh",
                ), {}
            base = _state_mapping(state, "base")
            base["sha"] = self.publisher.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            transition_state = deepcopy(state)
            retired = requeue_change_job(transition_state)
            state["requeue_transition"] = {
                "retired": retired,
                "base_sha": base["sha"],
                "close_nonce": secrets.token_hex(16),
            }
            self.states.save_run(run_id, state)
            return state, retired

    def finalize_requeue(self, run_id: str) -> dict[str, Any]:
        """Commit a prepared replacement only after old assets are retired."""
        with self.states.locked():
            try:
                state = self._load_bound_run(run_id)
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    waiting_for="GitHub repository binding",
                )
            transition = _state_mapping(state, "requeue_transition")
            retired = _state_mapping(transition, "retired")
            prepared_base_sha = transition.get("base_sha")
            if not isinstance(prepared_base_sha, str):
                raise RequeueError("prepared requeue base is invalid")
            # The initial intent is durable so an old-PR close can be retried,
            # but the new Generation binds its base only after that retirement
            # completes. This prevents a response-loss retry from reviving an
            # already superseded default-branch snapshot.
            try:
                repository = self.github.repository()
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    state=state,
                    waiting_for="GitHub requeue finalization refresh",
                )
            base_sha = self.publisher.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            _state_mapping(state, "base")["sha"] = base_sha
            applied = requeue_change_job(state)
            if applied != retired:
                raise RequeueError("prepared requeue no longer matches current Job")
            if retired["work_subject"].startswith("parent-only:"):
                generation = int(retired["generation"]) + 1
                state["parent_branch"] = (
                    f"agent-run/{run_id}/parent-generation-{generation}"
                )
            parent_number = int(_state_mapping(state, "parent")["number"])
            state.pop("requeue_transition", None)
            state = self._refresh(state, parent_number)
            self._ensure_delivery_branch(state, base_sha)
            self.states.save_run(run_id, state)
            return state

    def reject_requeue_after_pr_race(self, run_id: str) -> dict[str, Any]:
        """Persist a Human Blocker when old-PR retirement lost its race."""
        with self.states.locked():
            try:
                state = self._load_bound_run(run_id)
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    waiting_for="GitHub repository binding",
                )
            subject, job, _ = current_change_job(state)
            try:
                reason = (
                    unknown_pr_mutation(
                        state, subject, job, self.github, self.publisher.git
                    )
                    if job is not None
                    else None
                )
            except GitHubReadError as error:
                return self._wait_for_github_read(
                    run_id,
                    error,
                    state=state,
                    waiting_for="GitHub Change PR currentness refresh",
                )
            state.pop("requeue_transition", None)
            state.update(
                {
                    "status": "blocked",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {
                            "code": reason or "change_pr_supersession_unknown",
                            "message": "Change PR changed while Requeue retired its Generation",
                        }
                    ],
                    "updated_at": _now(),
                }
            )
            self.states.save_run(run_id, state)
            return state

    def _run_invocation_boundary_is_current(
        self, state: dict[str, Any], invocation: dict[str, Any]
    ) -> bool:
        boundary = invocation.get("currentness_boundary")
        if not isinstance(boundary, dict) or not boundary:
            # Invocations recorded before the Run boundary was introduced retain
            # their pre-existing resume behavior. Every #45 Run invocation writes
            # a boundary through invocation_event_recorder.
            return True
        parent = _state_mapping(state, "parent")
        graph = _state_mapping(state, "ticket_graph")
        run_branch = state.get("run_branch")
        repository = self.github.repository()
        if not isinstance(run_branch, str):
            return False
        return (
            boundary.get("reviewed_head_sha") == self.publisher.git.resolve(run_branch)
            and boundary.get("reviewed_default_base_sha")
            == self.publisher.git.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            and boundary.get("parent_revision") == parent.get("revision")
            and boundary.get("ticket_graph_revision") == graph.get("revision")
            and boundary.get("ticket_completion_records_fingerprint")
            == ticket_completion_records_fingerprint(state)
            and boundary.get("expected_merge_tree")
            == self.publisher.git.expected_merge_tree(
                default_head_sha=str(boundary["reviewed_default_base_sha"]),
                run_head_sha=str(boundary["reviewed_head_sha"]),
            )
        )

    def record_execution_failure(self, run_id: str, message: str) -> bool:
        with self.states.locked():
            state = self.states.load_run(run_id)
            if state is None:
                return False
            try:
                require_current_run_state(state)
            except IncompatibleRunStateError:
                return False
            if state.get("status") in {
                "abandoned",
                "abandonment_pending",
                "completed",
                "parent_closeout_pending",
            }:
                return False
            # Requeue persists its replacement intent before it performs any
            # Publisher mutation. A lost response while retiring the old PR
            # must preserve the pending state for a fresh currentness decision,
            # never turn it into an ordinary failed invocation.
            if state.get("status") == "requeue_required" and isinstance(
                state.get("requeue_transition"), dict
            ):
                return False
            if (
                state.get("status") == "blocked"
                and state.get("terminal_kind") == "waiting_human"
            ):
                return False
            hint_reader = getattr(self.github, "repository_hint", None)
            repository_hint = hint_reader() if callable(hint_reader) else None
            if (
                isinstance(repository_hint, str)
                and repository_hint
                and state.get("repository") != repository_hint
            ):
                return False
            try:
                repository = self.github.repository()
            except (GitHubReadError, OSError, ValueError):
                repository = None
            if (
                repository is not None
                and state.get("repository") != repository.name_with_owner
            ):
                return False
            active = state.get("active_agent_invocation")
            if isinstance(active, dict) and isinstance(active.get("role"), str):
                fail_interrupted_invocation(
                    state,
                    role=str(active["role"]),
                    save=lambda _state: None,
                )
            state.update(
                {
                    "status": "execution_failed",
                    "terminal_kind": "execution_failed",
                    "diagnostics": [
                        {
                            "code": (
                                "worker_credential_renewal_failed"
                                if "worker_credential_renewal_failed:" in message
                                else "command_failed"
                            ),
                            "message": bounded_error(message),
                        }
                    ],
                    "updated_at": _now(),
                }
            )
            self.states.save_run(run_id, state)
            return True

    def record_deterministic_contradiction(
        self, run_id: str, code: str, message: str
    ) -> bool:
        """Persist a proven GitHub fact without treating it as retryable."""

        with self.states.locked():
            state = self.states.load_run(run_id)
            if state is None:
                return False
            try:
                require_current_run_state(state)
            except IncompatibleRunStateError:
                return False
            if state.get("status") in {
                "abandoned",
                "abandonment_pending",
                "completed",
                "parent_closeout_pending",
                "deterministic_contradiction",
            }:
                return False
            hint_reader = getattr(self.github, "repository_hint", None)
            repository_hint = hint_reader() if callable(hint_reader) else None
            if (
                isinstance(repository_hint, str)
                and repository_hint
                and state.get("repository") != repository_hint
            ):
                return False
            state.pop("supervision_window", None)
            state.pop("supervision_wait", None)
            state.update(
                {
                    "status": "deterministic_contradiction",
                    "terminal_kind": "deterministic_contradiction",
                    "diagnostics": [
                        {"code": code, "message": bounded_error(message)}
                    ],
                    "updated_at": _now(),
                }
            )
            self.states.save_run(run_id, state)
            return True

    def _load_bound_run(
        self, run_id: str, *, state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        state = self._load_run(run_id) if state is None else state
        self._require_current_checkout(state)
        repository = self.github.repository()
        if state.get("repository") != repository.name_with_owner:
            raise ValueError(
                "configured GitHub repository does not match the Delivery Run"
            )
        return state

    def _require_current_checkout(self, state: dict[str, Any]) -> None:
        expected_identity = state.get("checkout_identity")
        current_identity = self.publisher.git.checkout_identity()
        if (
            not isinstance(expected_identity, str)
            or current_identity is None
            or current_identity != expected_identity
        ):
            raise RunLocatorError(
                "run_locator_stale",
                "Delivery Run 与当前 checkout identity 不一致或不可用；不会执行 mutation。",
            )

    def _wait_for_github_read(
        self,
        run_id: str,
        error: GitHubReadError,
        *,
        waiting_for: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        waiting = self._load_run(run_id) if state is None else state
        wait_for_recoverable_github_read(
            waiting, error, waiting_for=waiting_for, now=_now
        )
        self.states.save_run(run_id, waiting)
        return waiting

    def _load_run(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        require_current_run_state(state)
        return state

    def _refresh(
        self,
        state: dict[str, Any],
        parent_number: int,
        *,
        check_requeue_currentness: bool = False,
    ) -> dict[str, Any]:
        try:
            repository = self.github.repository()
            default_head = self.publisher.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            graph = self.github.delivery_graph(parent_number)
            previous_boundary = waiting_boundary(state)
            projected = state_from_graph(state, graph)
            refreshed = reconcile_structure(state, projected)
            supervision_window = state.get("supervision_window")
            credential_availability = state.get("credential_availability")
            if isinstance(credential_availability, dict):
                # The first-mint retry record is also Controller-owned local
                # state, not a GitHub graph fact.  Retain it before checking
                # whether the supervision window still has the same identity.
                refreshed["credential_availability"] = deepcopy(
                    credential_availability
                )
            if (
                isinstance(supervision_window, dict)
                and supervision_window_matches(
                    refreshed, supervision_window, boundary=previous_boundary
                )
            ):
                # Graph reconciliation is deliberately about GitHub-owned
                # delivery facts.  A foreground wait deadline is local Run
                # ownership and must survive a refresh that temporarily
                # projects the same lifecycle back to ``active``.  A window
                # for a completed or replaced Change Job must not leak into
                # the next frontier item.
                refreshed["supervision_window"] = deepcopy(supervision_window)
            else:
                refreshed.pop("supervision_window", None)
            refreshed.pop("currentness_resolution_pending", None)
            if state.get("status") == "requeue_required":
                if check_requeue_currentness:
                    # The graph projection may materialize the blocked Job as
                    # ``blocked``/``active``.  Give the currentness checker a
                    # neutral projection so it can distinguish a real PR
                    # mutation from the already-authorized local requeue.
                    refreshed.update(
                        {
                            "status": "active",
                            "terminal_kind": None,
                            "diagnostics": [],
                        }
                    )
                    self._mark_stale_change_job(
                        refreshed, check_requeue_currentness=True
                    )
                if refreshed.get("status") not in {
                    "active",
                    "requeue_required",
                }:
                    return refreshed
                # Graph reconciliation projects normal lifecycle phases from
                # GitHub facts and can otherwise erase a local stale-boundary
                # decision, especially when an ABA revision returns to its
                # original fingerprint.  Requeue is an explicit operator
                # decision, so preserve it until the requeue command consumes
                # the transition.
                refreshed.update(
                    {
                        "status": "requeue_required",
                        "terminal_kind": "requeue_required",
                        "diagnostics": deepcopy(state.get("diagnostics", [])),
                        "requeue_required": deepcopy(
                            state.get("requeue_required")
                        ),
                    }
                )
            else:
                self._mark_stale_change_job(
                    refreshed, check_requeue_currentness=check_requeue_currentness
                )
            self._invalidate_stale_final_run(refreshed, default_head)
            refreshed.pop("github_refresh_pending", None)
            return refreshed
        except GitHubReadError as error:
            failed = dict(state)
            if is_github_convergence_error(error.code):
                wait_for_github_refresh(
                    failed,
                    code=error.code,
                    message=error.message,
                    waiting_for="GitHub authority refresh",
                )
            else:
                failed.pop("supervision_window", None)
                failed.pop("supervision_wait", None)
                failed.update(
                    {
                        "status": "deterministic_contradiction",
                        "terminal_kind": "deterministic_contradiction",
                        "diagnostics": [
                            {
                                "code": error.code,
                                "message": bounded_error(error.message),
                            }
                        ],
                    }
                )
            failed["updated_at"] = _now()
            return failed

    def _mark_stale_change_job(
        self, state: dict[str, Any], *, check_requeue_currentness: bool = False
    ) -> None:
        """Route only mechanically provable Change Job drift to Requeue."""
        if state.get("status") in {
            "unsupported_scope_change",
            "abandoned",
            "abandonment_pending",
            "completed",
        }:
            return
        if state.get("status") == "requeue_required" and not check_requeue_currentness:
            return
        subject, job, _ = current_change_job(state)
        if job is None or job.get("phase") in {
            "completed",
            "merged",
            "merging",
        }:
            return
        if not any(
            type(job.get(key)) is int
            for key in (
                "ticket_branch_generation",
                "parent_generation",
                "repair_generation",
            )
        ):
            # A graph-selected Ticket is only a frontier projection.  It
            # becomes a Change Job once its normal constructor has bound the
            # first generation and currentness facts.
            return
        if not has_currentness_facts(subject, job):
            # Historical interrupted invocations can predate the persisted
            # currentness boundary. They remain eligible for their normal
            # invocation-recovery checks, but cannot be mechanically labelled
            # stale solely because a required fact is absent.
            return
        external = (
            None
            if job.get("blocked_reason") == "merged_revision_mismatch"
            else unknown_pr_mutation(
                state, subject, job, self.github, self.publisher.git
            )
        )
        if external is not None:
            job.pop("approval_grant", None)
            state.update(
                {
                    "status": "blocked",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {
                            "code": external,
                            "message": "Change PR changed outside the current Generation",
                        }
                    ],
                }
            )
            return
        if candidate_or_acceptance_is_inconsistent(job):
            job.pop("approval_grant", None)
            state.update(
                {
                    "status": "blocked",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {
                            "code": "candidate_or_acceptance_inconsistent",
                            "message": "Candidate or Acceptance cannot be safely requeued",
                        }
                    ],
                }
            )
            return
        reason = stale_change_job_reason(state, subject, job, self.publisher.git)
        if reason is None:
            return
        job.pop("approval_grant", None)
        if subject.startswith("run-repair:"):
            checkout = (
                self.states.root
                / "worktrees"
                / str(state["run_id"])
                / "run-repair"
            )
            repair_branch = str(job["repair_branch"])
            dirty_reason = self.publisher.git.managed_checkout_dirty_reason(checkout)
            if dirty_reason is not None:
                invalidate_stale_run_repair(state)
                DeliveryCleanupEngine(
                    git=self.publisher.git,
                    states=self.states,
                ).preserve_dirty_checkout(
                    state,
                    kind="run_repair",
                    branch=repair_branch,
                    checkout=checkout,
                    reason=dirty_reason,
                )
                return
            self.publisher.git.remove_worktree(checkout)
            for directory in (checkout.parent, checkout.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            invalidate_stale_run_repair(state)
            return
        state.update(
            {
                "status": "requeue_required",
                "terminal_kind": "requeue_required",
                "diagnostics": [
                    {
                        "code": reason,
                        "message": "Change Job Generation is stale; run requeue",
                    }
                ],
                "requeue_required": {
                    "work_subject": subject,
                    "generation": _subject_generation(job),
                    "reason": reason,
                },
            }
        )

    def _invalidate_stale_final_run(
        self, state: dict[str, Any], default_head: str
    ) -> None:
        """Route completed-Run boundary drift back to fresh Run Acceptance."""
        if state.get("status") in {
            "unsupported_scope_change",
            "deterministic_contradiction",
        }:
            return
        acceptance = state.get("run_acceptance")
        if not isinstance(acceptance, dict) or acceptance.get("phase") != "accepted":
            return
        publication = state.get("run_publication")
        if (
            isinstance(publication, dict)
            and publication.get("phase") == "waiting_external"
            and isinstance(publication.get("merge_intent"), dict)
        ):
            # A merge may have succeeded before GitHub's response or readback
            # converged. The approval path owns exact merge-intent recovery.
            return
        record = acceptance.get("acceptance_record")
        if not isinstance(record, dict):
            return
        run_branch = state.get("run_branch")
        if not isinstance(run_branch, str):
            return
        parent = _state_mapping(state, "parent")
        graph = _state_mapping(state, "ticket_graph")
        if (
            record.get("reviewed_head_sha") == self.publisher.git.resolve(run_branch)
            and record.get("reviewed_default_base_sha") == default_head
            and record.get("parent_revision") == parent.get("revision")
            and record.get("ticket_graph_revision") == graph.get("revision")
            and record.get("ticket_completion_records")
            == ticket_completion_records(state)
        ):
            return
        invalidate_run_acceptance(state)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_stale",
                "diagnostics": [],
            }
        )

    def _initial_state(
        self,
        repository: Repository,
        parent_number: int,
        run_id: str,
        base_sha: str,
        *,
        delivery_policy: DeliveryPolicy | None = None,
    ) -> dict[str, Any]:
        now = _now()
        policy = delivery_policy or self.delivery_policy
        state: dict[str, Any] = {
            "run_id": run_id,
            "branch_authority_protocol": 2,
            "review_budget_protocol": 1,
            "delivery_policy_protocol": DELIVERY_POLICY_PROTOCOL,
            "semantic_attempt_protocol": 1,
            "checkout_identity": self.publisher.git.ensure_checkout_identity(),
            "policy_snapshot": policy.snapshot(),
            "repository": repository.name_with_owner,
            "parent": {"number": parent_number, "title": None, "revision": None},
            "base": {"branch": repository.default_branch, "sha": base_sha},
            "ticket_graph": {
                "revision": None,
                "ordered_ticket_numbers": [],
                "tickets": {},
            },
            "frontier": [],
            "active_ticket_job": None,
            "ticket_jobs": {},
            "retired_ticket_generations": {},
            "pending_ticket_retirements": {},
            "accepted_ticket_graph_revision": None,
            "accepted_parent_spec_revision": None,
            "currentness_resolution_pending": True,
            "active_agent_invocation": None,
            "agent_invocation_history": [],
            "resume_audit": {
                "total": 0,
                "compacted": 0,
                "rolling_digest": None,
                "history": [],
            },
            "status": "starting",
            "diagnostics": [],
            "created_at": now,
            "updated_at": now,
        }
        if self.locator is not None:
            state["locator_registration_pending"] = True
        return state

    def _ensure_delivery_branch(self, state: dict[str, Any], base_sha: str) -> None:
        if state.get("status") in {
            "execution_failed",
            "unsupported_scope_change",
            "deterministic_contradiction",
            "abandoned",
            "completed",
            "requeue_required",
        }:
            return
        if _is_currentness_human_blocker(state):
            return
        graph = _state_mapping(state, "ticket_graph")
        ordered = _integer_list(graph, "ordered_ticket_numbers")
        if not ordered:
            state["delivery_type"] = "parent_only"
            branch = state.setdefault(
                "parent_branch", f"agent-run/{state['run_id']}/parent"
            )
        else:
            state["delivery_type"] = "ticket_run"
            branch = state.setdefault("run_branch", f"agent-run/{state['run_id']}/run")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Delivery Run branch is invalid")
        self.publisher.ensure_run_branch(branch, base_sha)

    def _start_locked(
        self,
        repository: Repository,
        parent_number: int,
        existing: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        resumed = existing is not None
        if existing is None:
            identity_sha = repository.default_head_sha or (
                f"unresolved-{repository.default_branch}"
            )
            run_id = self._available_run_id(
                repository.name_with_owner, parent_number, identity_sha
            )
            state = self._initial_state(
                repository,
                parent_number,
                run_id,
                identity_sha,
                delivery_policy=self._policy_for_new_run(),
            )
            state["base_resolution_pending"] = True
            # Persist identity before a remote fetch.  A timeout can then be
            # resumed against the same durable Run rather than creating a new
            # branch or Worker identity on the next foreground invocation.
            self.states.save_run(run_id, state)
            self._initialize_direct_profile(state)
        else:
            state = existing
            require_current_run_state(state)
            self._require_current_checkout(state)
            run_id = str(state["run_id"])
        self._register_pending_locator(state)
        if resumed:
            if state.get("status") in {
                "abandoned",
                "abandonment_pending",
                "deterministic_contradiction",
            }:
                return state, True
            if state.get("status") == "supervision_timeout":
                restore_supervision_wait(state)
        base = _state_mapping(state, "base")
        base_sha = str(base["sha"])
        if state.get("base_resolution_pending") is True:
            if state.pop("repository_binding_pending", None) is True:
                base["branch"] = repository.default_branch
                base["sha"] = repository.default_head_sha or (
                    f"unresolved-{repository.default_branch}"
                )
            try:
                base_sha = self.publisher.resolve_base(
                    repository.default_branch, repository.default_head_sha
                )
            except GitError as error:
                state.update(
                    {
                        "status": "execution_failed",
                        "terminal_kind": "execution_failed",
                        "diagnostics": [
                            {
                                "code": "base_resolution_failed",
                                "message": str(error),
                            }
                        ],
                        "updated_at": _now(),
                    }
                )
                self.states.save_run(run_id, state)
                return state, resumed
            base["sha"] = base_sha
            state.pop("base_resolution_pending", None)
            state["status"] = "starting"
            state["terminal_kind"] = None
            state["diagnostics"] = []
        state = self._refresh(state, parent_number)
        if state.get("status") in {
            "requeue_required",
            "deterministic_contradiction",
        } or _is_currentness_human_blocker(state):
            self.states.save_run(run_id, state)
            return state, resumed
        self._ensure_delivery_branch(state, base_sha)
        self.states.save_run(run_id, state)
        return state, resumed

    def _policy_for_new_run(self) -> DeliveryPolicy:
        if self.delivery_policy_provider is not None:
            return self.delivery_policy_provider()
        return self.delivery_policy

    def _register_pending_locator(self, state: dict[str, Any]) -> None:
        if self.locator is None or state.get("locator_registration_pending") is not True:
            return
        run_id = str(state["run_id"])
        self.locator.register(
            run_id=run_id,
            repository_root=self.checkout,
            state_dir=self.states.root,
        )
        state.pop("locator_registration_pending")
        self.states.save_run(run_id, state)

    def _initialize_direct_profile(self, state: dict[str, Any]) -> None:
        if self.profiles is None:
            run_id = state.get("run_id")
            if isinstance(run_id, str):
                AgentProfileStore(self.states.root).initialize(run_id)

    def _available_run_id(
        self, repository: str, parent_number: int, base_sha: str
    ) -> str:
        original = _run_id(repository, parent_number, base_sha, self.checkout)
        if self.states.load_run(original) is None:
            return original
        sequence = 2
        while self.states.load_run(f"{original}-{sequence}") is not None:
            sequence += 1
        return f"{original}-{sequence}"


def _run_id(
    repository: str, parent_number: int, base_sha: str, checkout: Path
) -> str:
    identity = f"{repository}\0{parent_number}\0{base_sha}\0{checkout.resolve()}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return f"run-{parent_number}-{suffix}"


def _is_currentness_human_blocker(state: dict[str, Any]) -> bool:
    """Whether fresh external Change-PR facts require a maintainer decision."""
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )


def _state_mapping(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run state field {key!r} is invalid")
    return value


def _integer_list(state: dict[str, Any], key: str) -> list[int]:
    value = state.get(key)
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ValueError(f"run state field {key!r} must contain integers")
    return list(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resume_agent_human_blocker(
    state: dict[str, Any], human_response: str | None = None
) -> bool:
    """Re-enter exactly one top-level Agent phase after an explicit resume.

    This is deliberately mechanical: Codex supplied the raw blocker text and
    the maintainer chose to resume.  The controller neither interprets the
    condition nor declares it fixed.
    """
    if human_blocker_subject_count(state) > 1:
        raise ValueError(
            "multiple current Human Blockers require an unambiguous resume target"
        )
    ticket_jobs = state.get("ticket_jobs")
    if isinstance(ticket_jobs, dict):
        for job in ticket_jobs.values():
            if _resume_change_job(
                state, job, ticket=True, human_response=human_response
            ):
                return True
    parent = state.get("parent_job")
    if _resume_change_job(state, parent, ticket=False, human_response=human_response):
        return True
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict):
        return False
    repair = acceptance.get("repair_job")
    if _resume_run_repair_human_blocker(
        state, acceptance, repair, human_response=human_response
    ):
        return True
    if acceptance.get("phase") == "ready_for_human" and acceptance.get(
        "blocked_reason"
    ) in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        blockers = _human_blockers(acceptance)
        append_human_response(
            acceptance,
            blockers,
            human_response,
            generation=int(acceptance.get("acceptance_generation", 1)),
        )
        acceptance.update(
            {
                "phase": str(acceptance.get("human_blocker_phase", "pending")),
                "prior_human_blockers": blockers,
            }
        )
        acceptance.pop("blocked_reason", None)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return True
    publication = state.get("run_publication")
    if (
        isinstance(publication, dict)
        and publication.get("phase") == "ready_for_human"
        and publication.get("human_blockers") is not None
    ):
        publication.update(
            {
                "phase": str(publication.get("human_blocker_phase", "pending")),
                "prior_human_blockers": _human_blockers(publication),
            }
        )
        append_human_response(
            publication,
            _human_blockers(publication),
            human_response,
            generation=_publication_generation(state),
        )
        state.update(
            {
                "status": "run_publication_pending",
                "terminal_kind": "run_publication_pending",
                "diagnostics": [],
            }
        )
        return True
    return False


def _resume_review_budget_window(
    state: dict[str, Any], delivery_policy: DeliveryPolicy | None = None
) -> bool:
    """Open exactly one new bounded window after an explicit budget pause."""

    policy = delivery_policy or default_delivery_policy()
    subjects = budget_checkpoint_subjects(state)
    if not subjects:
        return False
    if len(subjects) > 1:
        raise ValueError("multiple Review Budget checkpoints require an unambiguous resume target")
    subject_kind, subject = subjects[0]
    if subject_kind == "ticket":
        reset_budget(
            subject,
            ticket_budget_policy_for_job(
                subject, state_snapshot=state.get("policy_snapshot")
            ),
        )
        subject["policy_snapshot"] = policy.snapshot()
        state["policy_snapshot"] = policy.snapshot()
        subject.pop("blocked_reason", None)
        subject.pop("escalation_code", None)
        _resume_change_subject_after_budget(subject)
        state["active_ticket_job"] = subject
        state.update({"status": "active", "terminal_kind": None, "diagnostics": []})
        return True
    if subject_kind == "parent":
        reset_budget(subject, RUN_POLICY)
        state["policy_snapshot"] = policy.snapshot()
        subject.pop("blocked_reason", None)
        subject.pop("escalation_code", None)
        _resume_change_subject_after_budget(subject)
        state.update(
            {"status": "parent_delivery_pending", "terminal_kind": None, "diagnostics": []}
        )
        return True
    if subject_kind == "run":
        reset_budget(subject, RUN_POLICY)
        state["policy_snapshot"] = policy.snapshot()
        subject.pop("blocked_reason", None)
        subject["phase"] = "repairing"
        subject["repair_request"] = _budget_repair_request(subject)
        state.update(
            {"status": "run_acceptance_pending", "terminal_kind": "run_repair_pending", "diagnostics": []}
        )
        return True
    run_state = _state_mapping(state, "run_acceptance")
    reset_budget(run_state, RUN_POLICY)
    state["policy_snapshot"] = policy.snapshot()
    run_state["repair_request"] = _budget_repair_request(subject)
    run_state["phase"] = "repairing"
    run_state.pop("blocked_reason", None)
    run_state.pop("repair_job", None)
    state.update(
        {"status": "run_acceptance_pending", "terminal_kind": "run_repair_pending", "diagnostics": []}
    )
    return True


def _resume_change_subject_after_budget(job: dict[str, Any]) -> None:
    source = str(job.get("repair_source", ""))
    if source == "required_checks" and isinstance(job.get("ci_evidence"), dict):
        job["phase"] = "repairing"
        job["repair_source"] = "required_checks"
    elif source == "git_integrity" and isinstance(
        job.get("git_integrity_evidence"), dict
    ):
        job["phase"] = "repairing"
        job["repair_source"] = "git_integrity"
    elif source == "acceptance" and isinstance(
        job.get("acceptance_artifact"), dict
    ):
        job["phase"] = "repairing"
        job["repair_source"] = "acceptance"
    elif source == "human_revision" and isinstance(
        job.get("human_feedback"), str
    ):
        job["phase"] = "repairing"
        job["repair_source"] = "human_revision"
    elif source == "merge_conflict" and isinstance(
        job.get("merge_conflict_evidence"), str
    ):
        job["phase"] = "repairing"
        job["repair_source"] = "merge_conflict"
    else:
        job["phase"] = "developing"
    job.pop("next_attempt_kind", None)


def _budget_repair_request(job: dict[str, Any]) -> dict[str, Any]:
    source = str(job.get("repair_source", ""))
    if source == "required_checks" and isinstance(job.get("ci_evidence"), dict):
        request = {
            "repair_source": "required_checks",
            "ci_evidence": deepcopy(job["ci_evidence"]),
        }
    elif source == "git_integrity" and isinstance(
        job.get("git_integrity_evidence"), dict
    ):
        request = {
            "repair_source": "git_integrity",
            "git_integrity_evidence": deepcopy(job["git_integrity_evidence"]),
        }
    elif source == "human_revision" and isinstance(job.get("human_feedback"), str):
        request = {
            "repair_source": "human_revision",
            "human_feedback": str(job["human_feedback"]),
        }
    elif source == "merge_conflict" and isinstance(
        job.get("merge_conflict_evidence"), str
    ):
        request = {
            "repair_source": "merge_conflict",
            "merge_conflict_evidence": str(job["merge_conflict_evidence"]),
        }
    else:
        artifact = job.get("acceptance_artifact")
        if not isinstance(artifact, dict):
            artifact = job.get("unresolved_acceptance_artifact")
        if not isinstance(artifact, dict):
            raise ValueError("budget checkpoint has no repair evidence")
        request = {
            "repair_source": "acceptance",
            "acceptance_artifact": deepcopy(artifact),
        }
    candidate = job.get("candidate_sha")
    if candidate is not None:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("budget checkpoint has an invalid current Candidate")
        request["repair_candidate_sha"] = candidate
    repair_mode = job.get("repair_mode")
    if repair_mode is not None:
        if repair_mode not in {"squash", "merge_resolution"}:
            raise ValueError("budget checkpoint has an invalid Run Repair mode")
        request["repair_mode"] = repair_mode
    return _carry_development_thread_context(request, job)


def _carry_development_thread_context(
    request: dict[str, Any], job: dict[str, Any]
) -> dict[str, Any]:
    """Carry the persistent Development Thread across an R5 Job rotation."""

    thread_id = job.get("development_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not thread_id.strip()
    ):
        raise ValueError("budget checkpoint has an invalid Development Thread ID")
    history = job.get("development_thread_history", [])
    if not isinstance(history, list) or not all(
        isinstance(item, str) and item.strip() for item in history
    ):
        raise ValueError("budget checkpoint has invalid Development Thread history")
    if thread_id is not None:
        request["development_thread_id"] = thread_id
    request["development_thread_history"] = deepcopy(history)
    return request


def _clear_current_invocation_thread(state: dict[str, Any]) -> None:
    invocation = state.get("active_agent_invocation")
    if not isinstance(invocation, dict):
        raise ValueError("--new-thread requires a current Agent Invocation")
    role = invocation.get("role")
    if role == "reviewer":
        run = _run_acceptance_for_invocation(state, invocation)
        run.pop("reviewer_resume_thread_id", None)
        run["reviewer_new_thread"] = True
        run["phase"] = "pending"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return
    if role in {"development", "fresh_acceptance"}:
        job = _change_job_for_invocation(state, invocation)
        if role == "development":
            job.pop("development_thread_id", None)
            job["development_new_thread"] = True
        else:
            job.pop("review_resume_thread_id", None)
            job["review_new_thread"] = True
        return
    if role not in {"publication", "final_publication"}:
        raise ValueError("--new-thread requires a current Agent Invocation")
    job, mirror = _publication_job_for_invocation(state, invocation)
    if invocation.get("role") == "final_publication":
        job.pop("thread_id", None)
        job.pop("publication_failure_resume", None)
    else:
        job.pop("publication_thread_id", None)
    job["publication_new_thread"] = True
    job.pop("publication_failure_resume", None)
    if mirror is not None:
        mirror.pop("publication_thread_id", None)
        mirror["publication_new_thread"] = True
        mirror.pop("publication_failure_resume", None)


def _restore_current_invocation_thread(
    state: dict[str, Any], *, allow_completed: bool = False
) -> None:
    invocation = state.get("active_agent_invocation")
    if (
        not isinstance(invocation, dict)
        or invocation.get("status") not in {"failed", "completed", "resuming"}
        or (
            invocation.get("status") == "completed"
            and not allow_completed
        )
        or not isinstance(invocation.get("semantic_attempt"), dict)
        or invocation["semantic_attempt"].get("status") != "pending"
        or not invocation_attempt_is_pending(state, invocation)
        or invocation.get("role")
        not in {
            "development",
            "fresh_acceptance",
            "publication",
            "final_publication",
            "reviewer",
        }
    ):
        return
    role = invocation.get("role")
    if role == "reviewer":
        run = _run_acceptance_for_invocation(state, invocation)
        thread_id = invocation.get("reported_thread_id") or invocation.get(
            "requested_thread_id"
        )
        if isinstance(thread_id, str) and thread_id.strip():
            run["reviewer_resume_thread_id"] = thread_id
        run.pop("reviewer_new_thread", None)
        run["phase"] = "pending"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_pending",
                "diagnostics": [],
            }
        )
        return
    if role in {"development", "fresh_acceptance"}:
        job = _change_job_for_invocation(state, invocation)
        thread_id = invocation.get("reported_thread_id") or invocation.get(
            "requested_thread_id"
        )
        if not isinstance(thread_id, str) or not thread_id.strip():
            return
        if role == "development":
            job["development_thread_id"] = thread_id
            job.pop("development_new_thread", None)
            job["development_failure_resume"] = True
        else:
            job["review_resume_thread_id"] = thread_id
            job["review_new_thread"] = False
            job["review_failure_resume"] = True
        return
    job, mirror = _publication_job_for_invocation(state, invocation)
    thread_id = invocation.get("reported_thread_id") or invocation.get(
        "requested_thread_id"
    )
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    if invocation.get("role") == "final_publication":
        job["thread_id"] = thread_id
        job["publication_failure_resume"] = True
    else:
        job["publication_thread_id"] = thread_id
        job.pop("publication_new_thread", None)
        job["publication_failure_resume"] = True
        if mirror is not None:
            mirror["publication_thread_id"] = thread_id
            mirror.pop("publication_new_thread", None)
            mirror["publication_failure_resume"] = True


def _mark_failed_invocation_resuming(state: dict[str, Any]) -> None:
    """Make an explicit resume distinguishable from a still-unacknowledged failure."""

    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, dict) and invocation.get("status") == "failed":
        invocation["status"] = "resuming"


def _run_acceptance_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    run_id = state.get("run_id")
    if invocation.get("work_subject") != f"run-acceptance:{run_id}":
        raise ValueError("Run Acceptance Invocation work_subject is invalid")
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        raise ValueError("current Run Acceptance job is missing")
    generation = invocation.get("generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("current Agent Invocation generation is invalid")
    _require_invocation_generation(generation, run.get("acceptance_generation"))
    return run


def _invalidate_stale_run_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> None:
    """Discard a stale final-stage Invocation and restart Run Acceptance.

    Run Acceptance and Final Publication do not own generation-local branches
    or PRs.  Their changed authority boundary therefore calls for a new whole
    Run review, not Change Job Requeue (which has no final-stage asset to
    replace).
    """
    del invocation
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        raise ValueError("current Run Acceptance job is missing")
    invalidate_run_acceptance(state)
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        publication.pop("thread_id", None)
        publication.pop("publication_new_thread", None)
    state.update(
        {
            "status": "run_acceptance_pending",
            "terminal_kind": "run_acceptance_stale",
            "diagnostics": [],
        }
    )


def _publication_job_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve a Publication job from its persisted, unambiguous subject."""

    subject = invocation.get("work_subject")
    generation = invocation.get("generation")
    run_id = state.get("run_id")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("current Publication Invocation work_subject is missing")
    if type(generation) is not int or generation < 1:
        raise ValueError("current Publication Invocation generation is invalid")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Delivery Run ID is missing")

    role = invocation.get("role")
    if role == "final_publication":
        if subject != f"run-publication:{run_id}":
            raise ValueError("Final Publication Invocation work_subject is invalid")
        publication = state.get("run_publication")
        if not isinstance(publication, dict):
            raise ValueError("current Final Publication job is missing")
        acceptance = state.get("run_acceptance")
        current_generation = (
            acceptance.get("acceptance_generation", 1)
            if isinstance(acceptance, dict)
            else None
        )
        _require_invocation_generation(generation, current_generation)
        return publication, None
    if role != "publication":
        raise ValueError("current Publication Invocation role is invalid")

    if subject.startswith("ticket:"):
        ticket_text = subject.removeprefix("ticket:")
        if not ticket_text.isdigit() or str(int(ticket_text)) != ticket_text:
            raise ValueError("Ticket Publication Invocation work_subject is invalid")
        ticket_number = int(ticket_text)
        jobs = state.get("ticket_jobs")
        job = jobs.get(ticket_text) if isinstance(jobs, dict) else None
        if not isinstance(job, dict) or job.get("ticket_number") != ticket_number:
            raise ValueError("current Ticket Publication job is missing")
        _require_invocation_generation(generation, job.get("ticket_branch_generation"))
        active = state.get("active_ticket_job")
        mirror = (
            active
            if isinstance(active, dict)
            and active.get("ticket_number") == ticket_number
            and active is not job
            else None
        )
        return job, mirror

    if subject == f"parent-only:{run_id}":
        parent = state.get("parent_job")
        if not isinstance(parent, dict):
            raise ValueError("current Parent-only Publication job is missing")
        _require_invocation_generation(generation, parent.get("parent_generation", 1))
        return parent, None

    if subject == f"run-repair:{run_id}":
        acceptance = state.get("run_acceptance")
        repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if not isinstance(repair, dict):
            raise ValueError("current Run Repair Publication job is missing")
        _require_invocation_generation(generation, repair.get("repair_generation"))
        return repair, None

    raise ValueError("Publication Invocation work_subject is invalid")


def _change_job_for_invocation(
    state: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a Change Job for Development or Fresh Acceptance resume."""

    subject = invocation.get("work_subject")
    generation = invocation.get("generation")
    run_id = state.get("run_id")
    if not isinstance(subject, str) or type(generation) is not int or generation < 1:
        raise ValueError("current Change Job Invocation identity is invalid")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Delivery Run ID is missing")
    if subject.startswith("ticket:"):
        ticket_text = subject.removeprefix("ticket:")
        if not ticket_text.isdigit() or str(int(ticket_text)) != ticket_text:
            raise ValueError(
                "current Ticket Change Job Invocation work_subject is invalid"
            )
        jobs = state.get("ticket_jobs")
        job = jobs.get(ticket_text) if isinstance(jobs, dict) else None
        if not isinstance(job, dict) or job.get("ticket_number") != int(ticket_text):
            raise ValueError("current Ticket Change Job is missing")
        _require_invocation_generation(generation, job.get("ticket_branch_generation"))
        return job
    if subject == f"parent-only:{run_id}":
        job = state.get("parent_job")
        if not isinstance(job, dict):
            raise ValueError("current Parent-only Change Job is missing")
        _require_invocation_generation(generation, job.get("parent_generation", 1))
        return job
    if subject == f"run-repair:{run_id}":
        acceptance = state.get("run_acceptance")
        job = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if not isinstance(job, dict):
            raise ValueError("current Run Repair Change Job is missing")
        _require_invocation_generation(generation, job.get("repair_generation"))
        return job
    raise ValueError("Change Job Invocation work_subject is invalid")


def _require_invocation_generation(invocation: int, current: object) -> None:
    if type(current) is not int or invocation != current:
        raise ValueError("Publication Invocation generation is stale")


def _resume_change_job(
    state: dict[str, Any], value: object, *, ticket: bool, human_response: str | None
) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("phase") != "blocked" or value.get("blocked_reason") not in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        return False
    _resume_change_job_owner(value, human_response=human_response)
    if ticket:
        state["active_ticket_job"] = value
        status = "active"
    elif "ticket_number" in value:
        status = "active"
    else:
        status = "parent_delivery_pending"
    state.update(
        {"status": status, "terminal_kind": "waiting_human", "diagnostics": []}
    )
    return True


def _resume_change_job_owner(
    value: dict[str, Any], *, human_response: str | None
) -> None:
    blockers = _human_blockers(value)
    reviewer_resume = (
        value.get("blocked_reason") == "reviewer_requires_human"
        and value.get("human_blocker_phase") == "candidate"
    )
    append_human_response(
        value,
        blockers,
        human_response,
        generation=_subject_generation(value),
    )
    value.update(
        {
            "phase": str(value.get("human_blocker_phase", "developing")),
            "prior_human_blockers": blockers,
        }
    )
    if reviewer_resume:
        value["review_human_blocker_resume"] = True
    else:
        value.pop("review_human_blocker_resume", None)
    value.pop("blocked_reason", None)


def _resume_run_repair_human_blocker(
    state: dict[str, Any],
    run: dict[str, Any],
    value: object,
    *,
    human_response: str | None,
) -> bool:
    """Resume the blocked role inside the existing Run Repair Attempt."""

    if not isinstance(value, dict):
        return False
    if value.get("phase") != "blocked" or value.get("blocked_reason") not in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        return False
    _resume_change_job_owner(value, human_response=human_response)
    cycle = run.get("repair_cycle")
    if isinstance(cycle, dict):
        cycle["status"] = "active"
        cycle.pop("ended_reason", None)
    run["phase"] = "repairing"
    run.pop("blocked_reason", None)
    state.pop("requeue_required", None)
    state.update(
        {
            "status": "run_acceptance_pending",
            "terminal_kind": "run_repair_pending",
            "diagnostics": [],
        }
    )
    return True


def _human_blockers(subject: dict[str, Any]) -> list[str]:
    value = subject.get("human_blockers")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("Agent Human Blocker is missing raw blocker strings")
    return list(value)


def _run_acceptance_human_blocker(state: dict[str, Any]) -> bool:
    acceptance = state.get("run_acceptance")
    return isinstance(acceptance, dict) and (
        acceptance.get("phase") == "ready_for_human"
        and acceptance.get("blocked_reason")
        in {"agent_requires_human", "reviewer_requires_human"}
    )


def _validated_human_response(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("--message must be non-empty after trimming")
    if len(normalized.encode("utf-8")) > 8192:
        raise ValueError("--message must be at most 8 KiB")
    return normalized


def _subject_generation(subject: dict[str, Any]) -> int:
    for key in (
        "ticket_branch_generation",
        "repair_generation",
        "parent_generation",
    ):
        value = subject.get(key)
        if isinstance(value, int):
            return value
    return 1


def _publication_generation(state: dict[str, Any]) -> int:
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        current = publication.get("human_response_generation")
        if isinstance(current, int):
            return current
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        generation = acceptance.get("acceptance_generation")
        if isinstance(generation, int):
            return generation
    return 1
