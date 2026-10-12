from __future__ import annotations

from agent_run.delivery_facts import current_work_subject
from agent_run.work_facts import work_attempt, work_phase, work_role

import fcntl
import json
import logging
import os
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.error_safety import bounded_error, redact_credentials
from agent_run.history_check_facts import CHECKS_HISTORY_FIELDS, checks_timeline_facts
from agent_run.resume_audit import latest_resume_audit
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_commits import coordinator_for
from agent_run.state_errors import IncompatibleRunStateError


MAX_TIMELINE_EVENTS = 256
MAX_TIMELINE_CONTINUATION_EVENTS = 256
MAX_DIAGNOSTIC_ENTRIES = 32
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MAX_RUN_STATE_BYTES = 4 * 1024 * 1024
_T = TypeVar("_T")


class ConcurrentRunStateError(RuntimeError):
    """A Run changed after a Controller read its refresh baseline."""


class SimulatedProcessCrash(OSError):
    """Fault-injection signal whose cleanup matches an abrupt process exit."""


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self._lock_depth = 0
        self._lock_owner: int | None = None
        self._write_guard: Callable[[], None] | None = None
        self.run_saved_observer: Callable[[dict[str, Any]], None] | None = None
        self._write_transaction: Callable[[], Any] | None = None
        self._expected_run_state = threading.local()
        self._repository = SQLiteStateRepository(root, max_bytes=MAX_RUN_STATE_BYTES)
        self._coordinator = coordinator_for(root)
        self._commit_context = threading.local()
        self._validated_read: tuple[object, weakref.ReferenceType[object]] | None = None

    @contextmanager
    def require_unchanged_run(
        self, run_id: str, expected: dict[str, Any]
    ) -> Iterator[None]:
        """Reject stale Controller saves while leaving GitHub reads unlocked."""
        previous: dict[str, dict[str, Any]] = getattr(
            self._expected_run_state, "by_run", {}
        )
        current = dict(previous)
        current[run_id] = deepcopy(expected)
        self._expected_run_state.by_run = current
        try:
            yield
        finally:
            self._expected_run_state.by_run = previous

    def load_unchanged_run(self, run_id: str) -> dict[str, Any] | None:
        """Read the result of this Controller refresh only if no writer superseded it."""
        expected = getattr(self._expected_run_state, "by_run", {}).get(run_id)
        latest = self.load_run(run_id)
        if expected is None or latest != expected:
            raise ConcurrentRunStateError("Run changed before Controller returned its refresh")
        return latest

    def _set_write_guard(
        self,
        guard: Callable[[], None] | None,
        *,
        transaction: Callable[[], Any] | None = None,
    ) -> None:
        """Set the short-transaction guard used before a Run state commit."""

        self._write_guard = guard
        self._write_transaction = transaction

    @contextmanager
    def locked(self) -> Iterator[None]:
        owner = threading.get_ident()
        if self._lock_owner == owner:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._lock_depth = 1
            self._lock_owner = owner
            try:
                yield
            finally:
                self._lock_depth = 0
                self._lock_owner = None
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        self._commit_run(
            lambda: self._save_run_and_notify_unlocked(run_id, state),
        )

    def update_run(
        self, run_id: str, update: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Commit a logical object transition against the latest consistent Run.

        The callback verifies the relevant Job/Attempt/Invocation identities
        and changes only its owned objects. It runs once inside the authorized
        transaction; Agent, Git, network and observer work belong outside it.
        StateStore validates associated facts and returns only a durable view.
        """
        def commit() -> dict[str, Any]:
            state = self.load_current_run(run_id)
            if state is None:
                raise ValueError("Delivery Run disappeared during object commit")
            update(state)
            from agent_run.state_contract import require_current_run_state

            require_current_run_state(state)
            self._save_run_and_notify_unlocked(run_id, state)
            saved = self.load_current_run(run_id)
            assert saved is not None
            return saved

        return self._commit_run(commit)

    @contextmanager
    def _write_boundary(self) -> Iterator[None]:
        if self._write_transaction is not None:
            with self._write_transaction():
                yield
        else:
            if self._write_guard is not None:
                self._write_guard()
            yield

    def _commit_run(self, operation: Callable[[], _T]) -> _T:
        # Queue before acquiring Control/Run locks. Never replay a transaction.
        with self.commit_admission():
            notifications = self._commit_context.notifications
            checkpoint = len(notifications)
            expected = dict(getattr(self._expected_run_state, "by_run", {}))
            try:
                with self._write_boundary(), self.locked(), self._repository.atomic():
                    return operation()
            except BaseException:
                del notifications[checkpoint:]
                self._expected_run_state.by_run = expected
                raise

    @contextmanager
    def commit_admission(self) -> Iterator[None]:
        """Acquire a FIFO seat before callers enter a compound Control boundary.

        Nested saves on this StateStore reuse the seat. Notifications wait until its outer scope
        has released all Control and database locks, and never fail the save.
        """
        nested = getattr(self._commit_context, "notifications", None) is not None
        notifications: list[dict[str, Any] | None] = []
        try:
            with self._coordinator.admitted():
                if not nested:
                    self._commit_context.notifications = notifications
                try:
                    yield
                finally:
                    if not nested:
                        self._commit_context.notifications = None
        finally:
            if not nested:
                for saved in notifications:
                    self._after_run_saved()
                    if saved is not None and self.run_saved_observer is not None:
                        try:
                            self.run_saved_observer(saved)
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Run observer failed after durable commit", exc_info=True,
                            )

    def create_run_if_absent(
        self, run_id: str, state: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create one Run or return the existing Run."""

        custom_save = self._custom_save_run()
        def create() -> tuple[dict[str, Any], bool]:
            existing = self.load_run(run_id)
            if existing is not None:
                return existing, False
            if custom_save is not None:
                custom_save(run_id, state)
            else:
                self._save_run_and_notify_unlocked(run_id, state)
            return deepcopy(state), True

        return self._commit_run(create)

    def create_run_if_unfinished_absent(
        self,
        repository: str,
        parent_number: int,
        run_id: str,
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reuse an unfinished Parent Run or create one."""

        custom_save = self._custom_save_run()
        def create() -> tuple[dict[str, Any], bool]:
            unfinished = self.find_unfinished_runs(repository, parent_number)
            if len(unfinished) > 1:
                run_ids = ", ".join(str(item.get("run_id")) for item in unfinished)
                raise ValueError(
                    "multiple unfinished Delivery Runs exist for this Parent Issue: "
                    f"{run_ids}"
                )
            if unfinished:
                return unfinished[0], False
            existing = self.load_run(run_id)
            if existing is not None:
                return existing, False
            if custom_save is not None:
                custom_save(run_id, state)
            else:
                self._save_run_and_notify_unlocked(run_id, state)
            return deepcopy(state), True

        return self._commit_run(create)

    def _custom_save_run(self) -> Callable[[str, dict[str, Any]], None] | None:
        instance_method = self.__dict__.get("save_run")
        if callable(instance_method):
            return cast(Callable[[str, dict[str, Any]], None], instance_method)
        class_method = getattr(type(self), "save_run", None)
        if class_method is not StateStore.save_run:
            return self.save_run
        return None

    def _save_run_and_notify_unlocked(
        self, run_id: str, state: dict[str, Any]
    ) -> None:
        durable = self._save_run_unlocked(run_id, state)
        notifications = getattr(self._commit_context, "notifications", None)
        if notifications is None:
            raise RuntimeError("Run save requires commit admission")
        if self.run_saved_observer is None:
            notifications.append(None)
        elif durable is None:
            # Custom save hooks may retain the former None return contract.
            notifications.append(deepcopy(state))
        else:
            # Sanitization already made an isolated durable view. Only timeline
            # fields are shared back with the caller by the save bridge.
            for field in ("timeline", "timeline_continuation"):
                if field in durable:
                    durable[field] = deepcopy(durable[field])
            notifications.append(durable)

    def _after_run_saved(self) -> None:
        """Hook for durable-write fault injection."""

    def _save_run_unlocked(self, run_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
        previous = self.load_run(run_id)
        expected_by_run: dict[str, dict[str, Any]] = getattr(
            self._expected_run_state, "by_run", {}
        )
        expected = expected_by_run.get(run_id)
        if expected is not None and previous != expected:
            raise ConcurrentRunStateError("Run changed during Controller refresh")
        if (expected is None and isinstance(previous, dict)
                and state.get("resume_audit", {}).get("total", 0)
                <= previous.get("resume_audit", {}).get("total", 0)):
            # Foreground external supervision may hold an older whole-Run
            # projection while a targeted Resume commits. Do not erase its
            # accepted work, audit or queue; the driver reloads and continues.
            for number, job in previous.get("ticket_jobs", {}).items():
                proposed = state.get("ticket_jobs", {}).get(number, {})
                if (isinstance(job, dict) and job.get("last_ticket_resume")
                        and job.get("ticket_branch_generation") == proposed.get("ticket_branch_generation")
                        and job["last_ticket_resume"] != proposed.get("last_ticket_resume")):
                    raise ConcurrentRunStateError("Ticket Resume superseded the Run projection")
        if isinstance(previous, dict) and isinstance(previous.get("ticket_pause_requests"), dict):
            requests = state.get("ticket_pause_requests")
            jobs = state.get("ticket_jobs")
            if isinstance(requests, dict):
                for number, requested_at in previous["ticket_pause_requests"].items():
                    job = jobs.get(number) if isinstance(jobs, dict) else None
                    if isinstance(job, dict) and job.get("phase") in {"blocked", "completed", "publication_pending"}:
                        requests.pop(number, None)
                    else:
                        requests.setdefault(number, requested_at)
        invocations = state.get("ticket_agent_invocations")
        active_job = state.get("active_ticket_job")
        active_invocation = state.get("active_agent_invocation")
        if (
            isinstance(invocations, dict)
            and isinstance(active_job, dict)
            and type(active_job.get("ticket_number")) is int
            and isinstance(active_invocation, dict)
            and active_invocation.get("work_subject")
            == f"ticket:{active_job['ticket_number']}"
        ):
            invocations[str(active_job["ticket_number"])] = deepcopy(active_invocation)
        from agent_run.ticket_state import capture_ticket_wait

        capture_ticket_wait(state)
        if "ticket_work_queue" in state:
            from agent_run.ticket_work import reconcile

            reconcile(state)
        durable_state = _sanitize_durable_errors(state)
        from agent_run.state_retention import compact_invocation_details

        compact_invocation_details(durable_state)
        if "agent_invocation_history" in durable_state:
            state["agent_invocation_history"] = deepcopy(durable_state["agent_invocation_history"])
        if "run_id" in durable_state:
            _append_timeline_event(durable_state, previous)
            state["timeline"] = durable_state["timeline"]
            if durable_state.get("timeline_at_capacity") is True:
                state["timeline_at_capacity"] = True
            continuation = durable_state.get("timeline_continuation")
            if isinstance(continuation, list):
                state["timeline_continuation"] = continuation
        self._repository.save(run_id, durable_state)
        if expected is not None:
            expected_by_run[run_id] = deepcopy(durable_state)
        return durable_state

    def _reject_legacy_run(self, run_id: str | None = None) -> None:
        legacy = (self.runs_directory / f"{run_id}.json" if run_id is not None
                  else next(self.runs_directory.glob("*.json"), None))
        if legacy is not None and legacy.is_file():
            raise IncompatibleRunStateError(
                f"incompatible legacy JSON Run: {legacy}; no migration is performed"
            )

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        self._reject_legacy_run(run_id)
        return self._repository.load(run_id)

    def has_run(self, run_id: str) -> bool:
        """Locate persisted records, including unsupported legacy identities."""
        return (self.runs_directory / f"{run_id}.json").is_file() or self._repository.has(run_id)

    def list_run_ids(self, *, limit: int | None = None) -> list[str]:
        """Discover SQLite and legacy identities without reading old records."""
        if limit is not None and limit < 0:
            raise ValueError("Run ID listing limit must not be negative")
        run_ids = set(self._repository.list_ids(limit=limit))
        for path in self.runs_directory.glob("*.json"):
            run_ids.add(path.stem)
            if limit is not None and len(run_ids) > limit:
                raise ValueError("Run ID listing exceeds the requested limit")
        return sorted(run_ids)

    def load_current_run(self, run_id: str) -> dict[str, Any] | None:
        """Load only the one supported persisted Run contract."""

        state = self.load_run(run_id)
        if state is None:
            self._validated_read = None
            return None
        from agent_run.state_contract import require_current_run_state

        token = self._repository.read_token()
        ordinary = (
            getattr(self.load_run, "__func__", None) is _DEFAULT_LOAD_RUN
            and getattr(self._validate_current_run, "__func__", None) is _DEFAULT_VALIDATE_CURRENT_RUN
        )
        cached = self._validated_read
        if not (ordinary and token is not None and cached is not None
                and cached[0] is require_current_run_state and cached[1]() is token):
            self._validate_current_run(run_id, state)
        # Only SQLite's immutable, content-proven baseline identifies this
        # pure validation. Never retain/recognize the caller's mutable dict.
        self._validated_read = ((require_current_run_state, weakref.ref(token))
                                if ordinary and token is not None else None)
        return state

    @contextmanager
    def current_run_reader(self, run_id: str) -> Iterator[Callable[[], dict[str, Any] | None]]:
        """Retain one read-only scheduling view while SQLite reports no commit.

        The caller must not mutate this polling projection. Ordinary loads
        and all commit paths still read independent mutable views. The watcher
        holds no transaction between polls and closes on every scope exit.
        """
        from agent_run.sqlite_watcher import SQLiteWatcher

        previous: dict[str, Any] | None = None
        previous_version: tuple[int, int] | None = None
        retained = False
        previous_limit = self._repository.max_bytes
        with SQLiteWatcher(self._repository.path) as watcher:
            def read() -> dict[str, Any] | None:
                nonlocal previous, previous_version, retained, previous_limit
                watcher.require_active()
                self._reject_legacy_run(run_id)
                if self._repository.in_transaction:
                    return self.load_current_run(run_id)
                version = watcher.version()
                if (retained and version == previous_version
                        and previous_limit == self._repository.max_bytes):
                    return previous
                with self._repository.use_reader(watcher.connection):
                    current = self.load_current_run(run_id)
                # Sampled before reading: a concurrent commit during the read
                # can only cause an extra refresh, never label old facts new.
                previous_version = version
                previous_limit = self._repository.max_bytes
                retained = current is None or self._repository._check_size(current) <= 512 * 1024
                previous = current if retained else None
                return current

            yield read

    def _validate_current_run(self, run_id: str, state: dict[str, Any]) -> None:
        from agent_run.state_contract import require_current_run_state
        from agent_run.state_errors import MissingRunMaterialsError

        try:
            require_current_run_state(state)
        except MissingRunMaterialsError as error:
            raise MissingRunMaterialsError(
                f"{self._repository.path} Run {run_id}: {error}"
            ) from error

    def find_run(self, repository: str, parent_number: int) -> dict[str, Any] | None:
        matches = self._find_runs(repository, parent_number, unfinished_only=False)
        return max(matches, key=lambda state: str(state.get("created_at", ""))) if matches else None

    def find_unfinished_runs(
        self, repository: str, parent_number: int,
    ) -> list[dict[str, Any]]:
        return self._find_runs(repository, parent_number, unfinished_only=True)

    def _find_runs(
        self, repository: str, parent_number: int, *, unfinished_only: bool,
    ) -> list[dict[str, Any]]:
        # Unknown old JSON may belong to this Parent. Refuse before new work,
        # without reading, migrating or overwriting any legacy record.
        self._reject_legacy_run()
        result = []
        for run_id in self._repository.find_ids(
            repository, parent_number, unfinished_only=unfinished_only,
        ):
            state = self.load_run(run_id)
            if state is not None:
                result.append(state)
        return sorted(result, key=lambda state: str(state.get("created_at", "")))


# Preserve custom read/validation hooks, including class-level replacements.
_DEFAULT_LOAD_RUN = StateStore.load_run
_DEFAULT_VALIDATE_CURRENT_RUN = StateStore._validate_current_run


def _append_timeline_event(
    state: dict[str, Any], previous: dict[str, Any] | None
) -> None:
    marker = _timeline_marker(state)
    marker.update(checks_timeline_facts(state, marker))
    marker.update(_execution_timeline_projection(state, marker))
    marker["result"] = _timeline_result(state)
    previous_marker: dict[str, object] | None = None
    if previous is not None:
        previous_marker = _timeline_marker(previous)
        previous_marker.update(checks_timeline_facts(previous, previous_marker))
        previous_marker.update(_execution_timeline_projection(previous, previous_marker))
        previous_marker["result"] = _timeline_result(previous)
        if marker == previous_marker:
            return
    timeline = state.setdefault("timeline", [])
    if not isinstance(timeline, list):
        raise ValueError("timeline must be an array")
    if state.get("timeline_at_capacity") is True:
        _append_timeline_continuation(state, marker, previous_marker)
        return
    if len(timeline) >= MAX_TIMELINE_EVENTS - 1:
        timeline.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "kind": "timeline_capacity",
                "status": str(state.get("status", "unknown")),
                "result": "capacity_reached",
            }
        )
        state["timeline_at_capacity"] = True
        _append_timeline_continuation(state, marker, previous_marker)
        return
    event = _timeline_event(marker, previous_marker)
    if _event_matches_marker(timeline[-1] if timeline else None, marker):
        return
    timeline.append(event)


def _append_timeline_continuation(
    state: dict[str, Any],
    marker: dict[str, object],
    previous_marker: dict[str, object] | None,
) -> None:
    continuation = state.setdefault("timeline_continuation", [])
    if not isinstance(continuation, list):
        raise ValueError("timeline_continuation must be an array")
    continuation_marker = dict(marker)
    continuation_marker["kind"] = _continuation_kind(marker, previous_marker)
    if _event_matches_marker(
        continuation[-1] if continuation else None, continuation_marker
    ):
        return
    continuation.append(_timeline_event(continuation_marker, previous_marker))
    if len(continuation) > MAX_TIMELINE_CONTINUATION_EVENTS:
        del continuation[: len(continuation) - MAX_TIMELINE_CONTINUATION_EVENTS]


def _continuation_kind(
    marker: dict[str, object], previous_marker: dict[str, object] | None
) -> object:
    status = marker.get("status")
    phase = marker.get("phase")
    if status in {"completed", "abandoned"}:
        return "completion"
    if phase in {"merged", "completed"}:
        return "integration"
    if previous_marker is None or marker.get("approval_granted_at") != previous_marker.get(
        "approval_granted_at"
    ):
        if marker.get("approval_granted_at") is not None:
            return "publication"
    if previous_marker is None or marker.get(
        "required_checks_observed_at"
    ) != previous_marker.get("required_checks_observed_at"):
        if marker.get("required_checks_result") is not None:
            return "required_checks"
    if marker.get("kind") == "run_publication":
        return "publication"
    return marker["kind"]


def _timeline_event(
    marker: dict[str, object], previous_marker: dict[str, object] | None = None
) -> dict[str, object]:
    event: dict[str, object] = {
        "at": datetime.now(UTC).isoformat(),
        "kind": marker["kind"],
        "status": marker["status"],
    }
    for key in (
        "ticket",
        "worker",
        "attempt",
        "thread_id",
        "phase",
        "pr_number",
        "commit_sha",
        "approval_granted_at",
        "required_checks_result",
        "required_checks_observed_at",
        *CHECKS_HISTORY_FIELDS,
        "human_blockers",
        "accepted_graph_revision",
        "observed_graph_revision",
        "graph_change_summary",
        "next_action",
        "semantic_attempt_id",
        "semantic_attempt_role",
        "semantic_attempt_ordinal",
        "budget_window",
        "agent_invocation_started_at",
        "agent_invocation_status",
        "output_attempt",
        "publication_operation_retry_attempts",
        "publication_operation_retry_limit",
        "explicit_resume_sequence",
        "explicit_resume_kind",
        "explicit_resume_thread_id",
        "explicit_resume_attempt_id",
        "result",
    ):
        value = marker.get(key)
        if value is not None or (key == "budget_window" and key in marker):
            event[key] = deepcopy(value) if key == "graph_change_summary" else value
    if marker.get("required_checks_evidence") is not None and (
        previous_marker is None
        or marker.get("required_checks_signature") != previous_marker.get("required_checks_signature")
    ):
        event["required_checks_evidence"] = deepcopy(marker["required_checks_evidence"])
    return event


def _execution_timeline_projection(
    state: dict[str, Any], marker: dict[str, object],
) -> dict[str, object]:
    """Project counters for the same work owner as the timeline marker."""
    projection: dict[str, object] = {}
    subject_key = {
        "ticket_phase": "active_ticket_job", "parent_phase": "parent_job",
        "run_acceptance": "run_acceptance", "run_publication": "run_publication",
    }.get(str(marker.get("kind")))
    subject = state.get(subject_key) if subject_key is not None else None
    if (marker.get("kind") == "run_acceptance" and isinstance(subject, dict)
            and isinstance(subject.get("repair_job"), dict)):
        subject = subject["repair_job"]
    pending = subject.get("pending_semantic_attempt") if isinstance(subject, dict) else None
    attempt = pending if isinstance(pending, dict) else None
    invocation = state.get("active_agent_invocation")
    bound = invocation.get("semantic_attempt") if isinstance(invocation, dict) else None
    if marker.get("kind") == "run_status" and isinstance(bound, dict):
        # Transitional Run statuses have no stage owner in their marker.
        # Keep the actual Invocation continuation without mixing stage facts.
        subject = next((owner for owner in semantic_attempt_subjects(state)
                        if isinstance(owner.get("pending_semantic_attempt"), dict)
                        and owner["pending_semantic_attempt"].get("attempt_id") == bound.get("attempt_id")), None)
        attempt = subject["pending_semantic_attempt"] if subject is not None else bound
    invocation_matches = (
        attempt is not None and isinstance(bound, dict) and isinstance(invocation, dict)
        and isinstance(attempt.get("attempt_id"), str)
        and bound.get("attempt_id") == attempt["attempt_id"]
        and bound.get("work_subject") == attempt.get("work_subject")
        and invocation.get("work_subject", bound.get("work_subject")) == attempt.get("work_subject")
    )
    if attempt is not None:
        projection.update(
            {
                "semantic_attempt_id": attempt.get("attempt_id"),
                "semantic_attempt_role": attempt.get("role"),
                "semantic_attempt_ordinal": attempt.get("ordinal"),
                "budget_window": attempt.get("budget_window"),
            }
        )
    if invocation_matches and isinstance(invocation, dict):
        projection.update(
            {
                "agent_invocation_started_at": invocation.get("started_at"),
                "agent_invocation_status": invocation.get("status"),
                "output_attempt": invocation.get("attempt_count"),
            }
        )
    retry = subject.get("publication_operation_retry") if isinstance(subject, dict) else None
    if isinstance(retry, dict):
        projection.update(
            {
                "publication_operation_retry_attempts": retry.get("attempts"),
                "publication_operation_retry_limit": retry.get("limit"),
            }
        )
    resume = latest_resume_audit(state)
    if resume is not None:
        projection.update(
            {
                "explicit_resume_sequence": resume.get("sequence"),
                "explicit_resume_kind": resume.get("kind"),
                "explicit_resume_thread_id": resume.get("thread_id"),
                "explicit_resume_attempt_id": resume.get("semantic_attempt_id"),
            }
        )
    return projection


def _timeline_marker(state: dict[str, Any]) -> dict[str, object]:
    status = str(state.get("status", "unknown"))
    scope_change = state.get("unsupported_scope_change")
    if status == "unsupported_scope_change" and isinstance(
        scope_change, dict
    ):
        return {
            "kind": "unsupported_scope_change",
            "status": status,
            "accepted_graph_revision": scope_change.get(
                "accepted_graph_revision"
            ),
            "observed_graph_revision": scope_change.get(
                "observed_graph_revision"
            ),
            "graph_change_summary": scope_change.get("graph_change_summary"),
            "next_action": "restore_graph_or_abandon",
        }
    publication = state.get("run_publication")
    current_subject = current_work_subject(state)
    publication_is_current = (
        current_subject is not None and current_subject[1] is publication
    )
    if (
        status in {
        "run_publication_pending",
        "waiting_checks",
        "run_approval_pending",
        "parent_closeout_pending",
        "completed",
        "abandoned",
        }
        or (
            status in {"ready_for_human", "waiting_external", "supervision_timeout", "operator_stopped"}
            and isinstance(publication, dict)
            and publication.get("phase") in {"ready_for_human", "waiting_external", "waiting_checks", "ready_for_approval"}
        )
    ) and isinstance(publication, dict) and publication_is_current:
        phase = work_phase(publication) or "pending"
        role, attempt, thread_id = _history_work_context(state, publication, location="run_publication")
        approval_grant = publication.get("approval_grant")
        required_checks = publication.get("required_checks_evidence")
        return _with_human_blockers({
            "kind": "run_publication",
            "status": status,
            "worker": _worker_name(role, run=True),
            "attempt": attempt,
            "thread_id": thread_id,
            "phase": phase,
            "pr_number": publication.get("pr_number"),
            "commit_sha": publication.get("integrated_sha"),
            "approval_granted_at": (
                approval_grant.get("granted_at")
                if isinstance(approval_grant, dict)
                else None
            ),
            "required_checks_result": (
                required_checks.get("result")
                if isinstance(required_checks, dict)
                else None
            ),
            "required_checks_observed_at": publication.get(
                "required_checks_observed_at"
            ),
        }, publication)
    run_acceptance = state.get("run_acceptance")
    if isinstance(run_acceptance, dict) and (
        status in {"run_acceptance_pending", "ready_for_human"}
        or (
            status in {"waiting_checks", "waiting_external", "waiting_merge", "supervision_timeout", "operator_stopped"}
            and isinstance(run_acceptance.get("repair_job"), dict)
        )
    ):
        phase = str(run_acceptance.get("phase", "pending"))
        repair = run_acceptance.get("repair_job")
        subject = repair if isinstance(repair, dict) else run_acceptance
        subject_phase = work_phase(subject) or phase
        role, attempt, thread_id = _history_work_context(
            state, subject, location="run_repair" if isinstance(repair, dict) else "run_acceptance",
        )
        return _with_human_blockers({
            "kind": "run_acceptance",
            "status": status,
            "worker": _worker_name(role, run=not isinstance(repair, dict)),
            "attempt": attempt,
            "thread_id": thread_id,
            "phase": subject_phase,
            "pr_number": subject.get("pr_number"),
            "commit_sha": subject.get("integrated_sha") or subject.get("candidate_sha"),
        }, subject)
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        return _job_timeline_marker(state, active, status)
    parent_job = state.get("parent_job")
    if isinstance(parent_job, dict):
        return _job_timeline_marker(state, parent_job, status)
    return {"kind": "run_status", "status": status}


def _job_timeline_marker(state: dict[str, Any], job: dict[str, Any], status: str) -> dict[str, object]:
    phase = work_phase(job) or "pending"
    role, attempt, thread_id = _history_work_context(
        state, job, hide_pending_development=job.get("phase") in {"developing", "repairing"},
    )
    ticket = job.get("ticket_number")
    grant = job.get("approval_grant")
    return _with_human_blockers({
        "kind": "ticket_phase" if isinstance(ticket, int) else "parent_phase",
        "status": status,
        "ticket": ticket if isinstance(ticket, int) else None,
        "worker": _worker_name(role),
        "attempt": attempt,
        "thread_id": thread_id,
        "phase": phase,
        "pr_number": job.get("pr_number"),
        "approval_granted_at": grant.get("granted_at") if isinstance(grant, dict) else None,
        "commit_sha": job.get("integrated_sha")
        or job.get("publication_sha")
        or job.get("candidate_sha"),
    }, job)


def _history_work_context(
    state: dict[str, Any], subject: dict[str, Any], *,
    location: str | None = None, hide_pending_development: bool = False,
) -> tuple[str | None, int | None, object]:
    """History attributes a saved return to its Agent, even during program closeout."""
    pending = subject.get("pending_semantic_attempt")
    if isinstance(pending, dict) and pending.get("status") == "pending":
        role = pending.get("role")
        if role in {"development", "reviewer", "publication"}:
            ordinal = pending.get("ordinal")
            attempt = ordinal if isinstance(ordinal, int) else work_attempt(subject, role)
            returned = pending.get("returned_result")
            if isinstance(returned, dict):
                thread_id = returned.get("thread_id")
                artifact = returned.get("artifact")
                if thread_id is None and isinstance(artifact, dict):
                    thread_id = artifact.get("_thread_id")
                if isinstance(thread_id, str):
                    return role, attempt, thread_id
            invocation = state.get("active_agent_invocation")
            if isinstance(invocation, dict):
                bound = invocation.get("semantic_attempt")
                if (isinstance(bound, dict) and isinstance(pending.get("attempt_id"), str)
                        and bound.get("attempt_id") == pending["attempt_id"]
                        and bound.get("work_subject") == pending.get("work_subject")
                        and invocation.get("work_subject", bound.get("work_subject")) == pending.get("work_subject")):
                    thread_id = invocation.get("reported_thread_id") or invocation.get("requested_thread_id")
                    return role, attempt, thread_id if isinstance(thread_id, str) else None
            if role == "reviewer":
                prefix = "reviewer" if location == "run_acceptance" else "review"
                if subject.get(f"{prefix}_new_thread"):
                    return role, attempt, None
                resume_thread = subject.get(f"{prefix}_resume_thread_id")
                if isinstance(resume_thread, str):
                    return role, attempt, resume_thread
                human_resume = (subject.get("prior_human_blockers") if prefix == "reviewer"
                                else subject.get("review_human_blocker_resume"))
                if not human_resume:
                    return role, attempt, None
            return role, attempt, _thread_id_for_role(
                subject, role, hide_pending_development=hide_pending_development,
            )
    role = work_role(subject, location=location)
    return role, work_attempt(subject, role), (
        _thread_id_for_role(subject, role, hide_pending_development=hide_pending_development)
        if role is not None else None
    )


def _with_human_blockers(
    marker: dict[str, object], subject: dict[str, Any]
) -> dict[str, object]:
    blockers = subject.get("human_blockers")
    if isinstance(blockers, list) and all(isinstance(item, str) for item in blockers):
        marker["human_blockers"] = list(blockers)
    return marker


def _worker_name(role: str | None, *, run: bool = False) -> str | None:
    if role == "development":
        return "开发工作代理"
    if role == "reviewer":
        return "运行验收工作代理" if run else "独立验收工作代理"
    if role == "publication":
        return "运行发布工作代理" if run else "发布工作代理"
    return None


def _thread_id_for_role(
    subject: dict[str, Any],
    role: str | None,
    *,
    hide_pending_development: bool = False,
) -> object:
    reviewer_ids = subject.get("reviewer_thread_ids")
    if role == "reviewer":
        if isinstance(reviewer_ids, list) and reviewer_ids:
            latest = reviewer_ids[-1]
            return latest if isinstance(latest, str) else None
        return None
    if role == "development":
        if hide_pending_development and isinstance(subject.get("pending_attempt"), int):
            return None
        thread_id = subject.get("development_thread_id")
        return thread_id if isinstance(thread_id, str) else None
    if role == "publication":
        for key in ("publication_thread_id", "thread_id"):
            thread_id = subject.get(key)
            if isinstance(thread_id, str):
                return thread_id
        return None
    for key in ("publication_thread_id", "thread_id", "development_thread_id"):
        thread_id = subject.get(key)
        if isinstance(thread_id, str):
            return thread_id
    if isinstance(reviewer_ids, list) and reviewer_ids:
        latest = reviewer_ids[-1]
        return latest if isinstance(latest, str) else None
    return None


def _event_matches_marker(event: object, marker: dict[str, object]) -> bool:
    if not isinstance(event, dict):
        return False
    return all(event.get(key) == value for key, value in marker.items())


def _timeline_result(state: dict[str, Any]) -> object:
    if state.get("status") == "unsupported_scope_change":
        change = state.get("unsupported_scope_change")
        if isinstance(change, dict):
            summary = change.get("graph_change_summary")
            if isinstance(summary, dict) and isinstance(
                summary.get("summary"), str
            ):
                return summary["summary"]
    diagnostics = state.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        first = diagnostics[0]
        if isinstance(first, dict) and isinstance(first.get("code"), str):
            return first["code"]
    terminal_kind = state.get("terminal_kind")
    return (
        terminal_kind
        if isinstance(terminal_kind, str)
        and terminal_kind
        not in {"waiting_human", "waiting_checks", "all_tickets_completed"}
        else None
    )


def _sanitize_durable_errors(value: dict[str, Any]) -> dict[str, Any]:
    """Build an isolated durable copy while bounding and redacting payloads."""

    # Static instructions are intentional user content, not diagnostic payloads.
    # Preserve them verbatim across restart; dynamic output remains sanitized.
    sanitized = _sanitize_error_value(
        {key: item for key, item in value.items() if key != "prompt_resources"},
        redacted={}, key_kinds={},
    )
    if not isinstance(sanitized, dict):  # pragma: no cover - typed input is a mapping
        raise ValueError("durable Run state must be a mapping")
    if "prompt_resources" in value:
        sanitized["prompt_resources"] = deepcopy(value["prompt_resources"])
    diagnostics = sanitized.get("diagnostics")
    if isinstance(diagnostics, list):
        sanitized["diagnostics"] = [
            _bound_diagnostic(item)
            for item in diagnostics[:MAX_DIAGNOSTIC_ENTRIES]
            if isinstance(item, dict)
        ]
    return sanitized


_IDENTITY_SNAPSHOT_PATHS = frozenset({
    ("github_identity",), ("runner_github_identity",),
    ("creation_configuration", "github_identity"),
    ("creation_configuration", "runner_github_identity"),
})


def _sanitize_error_value(
    value: object, *, redacted: dict[str, str],
    key_kinds: dict[str, tuple[bool, bool, bool]],
    diagnostic: bool = False, path: tuple[str, ...] = ()
) -> object:
    if path in _IDENTITY_SNAPSHOT_PATHS:
        # Only validated source and references belong here, never credential values.
        from agent_run.github_identity import validate_identity_snapshot

        return validate_identity_snapshot(value)
    if isinstance(value, list):
        return [
            _sanitize_error_value(
                item, redacted=redacted, key_kinds=key_kinds, diagnostic=diagnostic, path=path,
            )
            for item in value
        ]
    if isinstance(value, str):
        if diagnostic:
            return bounded_error(value)
        cached = redacted.get(value)
        return cached if cached is not None else _redact_uncached_text(value, redacted)
    if not isinstance(value, dict):
        # JSON scalars are immutable. Extensions such as tuples may contain
        # mutable values, so retain their previous deepcopy isolation.
        return value if value is None or type(value) in (bool, int, float) else deepcopy(value)
    sanitized: dict[object, object] = {}
    for key, item in value.items():
        ephemeral = credential = error_field = False
        if isinstance(key, str):
            kinds = key_kinds.get(key)
            if kinds is None:
                normalized = key.casefold().replace("_", "").replace("-", "").replace(" ", "")
                kinds = (
                    _is_ephemeral_payload_key(normalized),
                    _is_credential_key(normalized),
                    key == "error" or key.endswith("_error"),
                )
                if len(key_kinds) < 256 and len(key) <= 128:
                    key_kinds[key] = kinds
            ephemeral, credential, error_field = kinds
        if ephemeral:
            continue
        if (
            credential and isinstance(key, str)
            and not _is_durable_protocol_identity((*path, key))
        ):
            sanitized[key] = "[REDACTED]"
            continue
        is_diagnostic = key == "diagnostics"
        # Leaf values need neither a recursive container dispatch nor a path.
        # Credential-key exceptions above still use the full current path.
        if isinstance(item, str):
            if diagnostic or is_diagnostic or error_field:
                sanitized[key] = bounded_error(item)
            else:
                cached = redacted.get(item)
                sanitized[key] = (
                    cached if cached is not None else _redact_uncached_text(item, redacted)
                )
        elif isinstance(item, (dict, list)):
            sanitized[key] = _sanitize_error_value(
                item,
                redacted=redacted,
                key_kinds=key_kinds,
                diagnostic=diagnostic or is_diagnostic,
                path=(*path, key) if isinstance(key, str) else path,
            )
        else:
            sanitized[key] = (
                item if item is None or type(item) in (bool, int, float) else deepcopy(item)
            )
    return sanitized


def _redact_uncached_text(value: str, redacted: dict[str, str]) -> str:
    result = redact_credentials(value)
    # Repeated history labels/identities need one scan per save. Keep raw
    # text only within this traversal, with bounded entries and key size.
    if len(redacted) < 256 and len(value) <= 1024:
        redacted[value] = result
    return result


def _is_ephemeral_payload_key(normalized: str) -> bool:
    return (
        normalized in {"env", "stdout", "stderr"}
        or "environment" in normalized
        or "transcript" in normalized
        or "rawoutput" in normalized
    )


def _is_credential_key(normalized: str) -> bool:
    return "credential" in normalized or normalized.endswith(
        (
            "authorization",
            "token",
            "secret",
            "password",
            "apikey",
            "privatekey",
            "cookie",
        )
    )


def _is_durable_protocol_identity(path: tuple[str, ...]) -> bool:
    return path in {
        ("action_application_receipt", "target_executor", "process_start_token"),
        (
            "action_application_receipt", "target_executor", "worker",
            "process_start_token",
        ),
    } or path[-4:] == (
        "last_ticket_resume", "payload", "resume_intent", "authorization"
    ) or path[-2:] == ("candidate_commit_intent", "token") or (
        bool(path)
        and path[-1]
        in {
            "credential_availability",
            "credential_failure_class",
            "credential_http_status",
            "previous_publication_authorization",
        }
    )


def _diagnostic_size(value: dict[object, object]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "...[truncated]"
    budget = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _bound_diagnostic(value: dict[object, object]) -> dict[object, object]:
    if _diagnostic_size(value) <= MAX_DIAGNOSTIC_BYTES:
        return value
    bounded: dict[object, object] = {}
    code = value.get("code")
    message = value.get("message")
    operator_gate = value.get("operator_gate")
    if isinstance(code, str):
        bounded["code"] = _truncate_utf8(code, 512)
    if isinstance(message, str):
        bounded["message"] = _truncate_utf8(message, 10 * 1024)
    if isinstance(operator_gate, dict):
        bounded["operator_gate"] = {
            key: _truncate_utf8(item, 512)
            for key, item in operator_gate.items()
            if key in {"work_subject", "action_kind", "phase", "reason"}
            and isinstance(item, str)
        }
    if _diagnostic_size(bounded) > MAX_DIAGNOSTIC_BYTES:
        bounded["message"] = _truncate_utf8(str(bounded.get("message", "")), 8 * 1024)
    return bounded


class FaultInjectingStateStore(StateStore):
    """Black-box test seam that crashes after one durable state write."""

    def __init__(self, root: Path, *, crash_after_save: int) -> None:
        if crash_after_save < 1:
            raise ValueError("crash_after_save must be positive")
        super().__init__(root)
        self.crash_after_save = crash_after_save
        self.save_count = 0
        self.injected = False

    def _after_run_saved(self) -> None:
        self.save_count += 1
        if not self.injected and self.save_count == self.crash_after_save:
            self.injected = True
            raise SimulatedProcessCrash(
                f"simulated crash after durable save #{self.save_count}"
            )
