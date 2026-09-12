from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from agent_run.error_safety import bounded_error, redact_credentials
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.history_check_facts import CHECKS_HISTORY_FIELDS, checks_timeline_facts
from agent_run.resume_audit import latest_resume_audit
from agent_run.presentation_helpers import current_work_subject
from agent_run.task_control import TaskControlBusyError


MAX_TIMELINE_EVENTS = 256
MAX_TIMELINE_CONTINUATION_EVENTS = 256
MAX_DIAGNOSTIC_ENTRIES = 32
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MAX_RUN_STATE_BYTES = 4 * 1024 * 1024
# Bound transient Task Control contention without relying on scheduler yield
# counts; the interval stays short while the total wait remains finite.
_STATE_COMMIT_RETRY_WINDOW_SECONDS = 0.25
_STATE_COMMIT_RETRY_INTERVAL_SECONDS = 0.001
_T = TypeVar("_T")


def _load_bounded_json(path: Path) -> object:
    with path.open("rb") as source:
        encoded = source.read(MAX_RUN_STATE_BYTES + 1)
    if len(encoded) > MAX_RUN_STATE_BYTES:
        raise ValueError(
            f"Run state exceeds {MAX_RUN_STATE_BYTES} byte persistence limit: {path}"
        )
    return json.loads(encoded.decode("utf-8"))


class SimulatedProcessCrash(OSError):
    """Fault-injection signal whose cleanup matches an abrupt process exit."""


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self._lock_depth = 0
        self._lock_owner: int | None = None
        self._write_guard: Callable[[], None] | None = None
        self._write_transaction: Callable[[], Any] | None = None

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
            lambda: self._save_run_and_notify_unlocked(run_id, state)
        )

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
        retry_deadline = (
            time.monotonic() + _STATE_COMMIT_RETRY_WINDOW_SECONDS
        )
        while True:
            try:
                # Acquire Task Control before Run state.  Every retry leaves
                # both short-transaction contexts before trying again.
                with self._write_boundary():
                    with self.locked():
                        return operation()
            except TaskControlBusyError:
                remaining = retry_deadline - time.monotonic()
                if remaining <= 0:
                    raise
                # Release the StateStore lock before retrying the short
                # Task Control transaction.  Use a bounded interval rather
                # than a fixed number of scheduler yields so a legal,
                # millisecond-scale transaction can finish.
                time.sleep(
                    min(_STATE_COMMIT_RETRY_INTERVAL_SECONDS, remaining)
                )

    def create_run_if_absent(
        self, run_id: str, state: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create one Run or return the existing Run."""

        custom_save = self._custom_save_run()
        if custom_save is not None:
            with self.locked():
                existing = self.load_run(run_id)
                if existing is not None:
                    return existing, False
                custom_save(run_id, state)
                return deepcopy(state), True

        def create() -> tuple[dict[str, Any], bool]:
            existing = self.load_run(run_id)
            if existing is not None:
                return existing, False
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
        if custom_save is not None:
            with self.locked():
                unfinished = self.find_unfinished_runs(repository, parent_number)
                if len(unfinished) > 1:
                    run_ids = ", ".join(str(item.get("run_id")) for item in unfinished)
                    raise ValueError(
                        "multiple unfinished Delivery Runs exist for this Parent Issue: "
                        f"{run_ids}"
                    )
                if unfinished:
                    return unfinished[0], False
                return self.create_run_if_absent(run_id, state)

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
        self._save_run_unlocked(run_id, state)
        self._after_run_saved()

    def _after_run_saved(self) -> None:
        """Hook for durable-write fault injection."""

    def _save_run_unlocked(self, run_id: str, state: dict[str, Any]) -> None:
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        destination = self.runs_directory / f"{run_id}.json"
        previous = self.load_run(run_id)
        durable_state = _sanitize_durable_errors(state)
        if "run_id" in durable_state:
            _append_timeline_event(durable_state, previous)
            state["timeline"] = durable_state["timeline"]
            if durable_state.get("timeline_at_capacity") is True:
                state["timeline_at_capacity"] = True
            continuation = durable_state.get("timeline_continuation")
            if isinstance(continuation, list):
                state["timeline_continuation"] = continuation
        serialized = (
            json.dumps(
                durable_state,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        if len(serialized.encode("utf-8")) > MAX_RUN_STATE_BYTES:
            raise ValueError(
                f"Run state exceeds {MAX_RUN_STATE_BYTES} byte persistence limit"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.runs_directory,
            prefix=f".{run_id}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(serialized)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
            self._sync_directory(self.runs_directory)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs_directory / f"{run_id}.json"
        if not path.exists():
            return None
        loaded = _load_bounded_json(path)
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid run state: {path}")
        return loaded

    def load_current_run(self, run_id: str) -> dict[str, Any] | None:
        """Load only the one supported persisted Run contract."""

        state = self.load_run(run_id)
        if state is not None:
            from agent_run.state_contract import require_current_run_state

            require_current_run_state(state)
        return state

    def find_run(self, repository: str, parent_number: int) -> dict[str, Any] | None:
        if not self.runs_directory.exists():
            return None
        matches: list[dict[str, Any]] = []
        for path in self.runs_directory.glob("*.json"):
            loaded = _load_bounded_json(path)
            if not isinstance(loaded, dict):
                continue
            parent = loaded.get("parent")
            if (
                loaded.get("repository") == repository
                and isinstance(parent, dict)
                and parent.get("number") == parent_number
            ):
                matches.append(loaded)
        if not matches:
            return None
        return max(matches, key=lambda state: str(state.get("created_at", "")))

    def find_unfinished_runs(
        self, repository: str, parent_number: int
    ) -> list[dict[str, Any]]:
        if not self.runs_directory.exists():
            return []
        runs: list[dict[str, Any]] = []
        for path in self.runs_directory.glob("*.json"):
            loaded = _load_bounded_json(path)
            if not isinstance(loaded, dict):
                continue
            parent = loaded.get("parent")
            if (
                loaded.get("repository") == repository
                and isinstance(parent, dict)
                and parent.get("number") == parent_number
                and loaded.get("status") not in {"completed", "abandoned"}
            ):
                runs.append(loaded)
        return sorted(runs, key=lambda state: str(state.get("created_at", "")))

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _append_timeline_event(
    state: dict[str, Any], previous: dict[str, Any] | None
) -> None:
    marker = _timeline_marker(state)
    marker.update(checks_timeline_facts(state, marker))
    marker.update(_execution_timeline_projection(state))
    marker["result"] = _timeline_result(state)
    previous_marker: dict[str, object] | None = None
    if previous is not None:
        previous_marker = _timeline_marker(previous)
        previous_marker.update(checks_timeline_facts(previous, previous_marker))
        previous_marker.update(_execution_timeline_projection(previous))
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


def _execution_timeline_projection(state: dict[str, Any]) -> dict[str, object]:
    """Project independent Agent counters without storing unbounded payloads."""

    projection: dict[str, object] = {}
    invocation = state.get("active_agent_invocation")
    invocation_attempt = (
        invocation.get("semantic_attempt") if isinstance(invocation, dict) else None
    )
    attempt = invocation_attempt if isinstance(invocation_attempt, dict) else None
    subjects = semantic_attempt_subjects(state)
    invocation_attempt_id = attempt.get("attempt_id") if attempt is not None else None
    selected_subject: dict[str, Any] | None = None
    for subject in subjects:
        pending = subject.get("pending_semantic_attempt")
        if not isinstance(pending, dict):
            continue
        if (
            invocation_attempt_id is None
            or pending.get("attempt_id") == invocation_attempt_id
        ):
            attempt = pending
            selected_subject = subject
            break
    if attempt is not None:
        projection.update(
            {
                "semantic_attempt_id": attempt.get("attempt_id"),
                "semantic_attempt_role": attempt.get("role"),
                "semantic_attempt_ordinal": attempt.get("ordinal"),
                "budget_window": attempt.get("budget_window"),
            }
        )
    if isinstance(invocation, dict):
        projection.update(
            {
                "agent_invocation_started_at": invocation.get("started_at"),
                "agent_invocation_status": invocation.get("status"),
                "output_attempt": invocation.get("attempt_count"),
            }
        )
    retry_subjects = [selected_subject] if selected_subject is not None else subjects
    for subject in retry_subjects:
        retry = subject.get("publication_operation_retry")
        if isinstance(retry, dict):
            projection.update(
                {
                    "publication_operation_retry_attempts": retry.get("attempts"),
                    "publication_operation_retry_limit": retry.get("limit"),
                }
            )
            break
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
        phase = str(publication.get("phase", "pending"))
        role = _worker_role(publication, phase)
        approval_grant = publication.get("approval_grant")
        required_checks = publication.get("required_checks_evidence")
        return _with_human_blockers({
            "kind": "run_publication",
            "status": status,
            "worker": _worker_name(role, run=True),
            "attempt": _worker_attempt(publication, role),
            "thread_id": _thread_id_for_role(publication, role),
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
        subject_phase = str(subject.get("phase", phase))
        role = _worker_role(subject, subject_phase)
        return _with_human_blockers({
            "kind": "run_acceptance",
            "status": status,
            "worker": _worker_name(role, run=not isinstance(repair, dict)),
            "attempt": _worker_attempt(subject, role),
            "thread_id": _thread_id_for_role(subject, role),
            "phase": subject_phase,
            "pr_number": subject.get("pr_number"),
            "commit_sha": subject.get("integrated_sha"),
        }, subject)
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        return _job_timeline_marker(active, status)
    parent_job = state.get("parent_job")
    if isinstance(parent_job, dict):
        return _job_timeline_marker(parent_job, status)
    return {"kind": "run_status", "status": status}


def _job_timeline_marker(job: dict[str, Any], status: str) -> dict[str, object]:
    phase = str(job.get("phase", "pending"))
    role = _worker_role(job, phase)
    ticket = job.get("ticket_number")
    grant = job.get("approval_grant")
    return _with_human_blockers({
        "kind": "ticket_phase" if isinstance(ticket, int) else "parent_phase",
        "status": status,
        "ticket": ticket if isinstance(ticket, int) else None,
        "worker": _worker_name(role),
        "attempt": _worker_attempt(job, role),
        "thread_id": _thread_id_for_role(
            job,
            role,
            hide_pending_development=phase in {"developing", "repairing"},
        ),
        "phase": phase,
        "pr_number": job.get("pr_number"),
        "approval_granted_at": grant.get("granted_at") if isinstance(grant, dict) else None,
        "commit_sha": job.get("integrated_sha")
        or job.get("publication_sha")
        or job.get("candidate_sha"),
    }, job)


def _with_human_blockers(
    marker: dict[str, object], subject: dict[str, Any]
) -> dict[str, object]:
    blockers = subject.get("human_blockers")
    if isinstance(blockers, list) and all(isinstance(item, str) for item in blockers):
        marker["human_blockers"] = list(blockers)
    return marker


def _worker_role(subject: dict[str, Any], phase: str) -> str | None:
    blocked_reason = subject.get("blocked_reason")
    if blocked_reason == "reviewer_requires_human":
        return "reviewer"
    if phase in {"reviewing", "validating"}:
        return "reviewer"
    if phase in {"developing", "repairing", "committing_candidate"}:
        return "development"
    if phase in {"publishing", "publication_pending"}:
        return "publication"
    if phase not in {"blocked", "ready_for_human"}:
        return None
    blocked_phase = str(subject.get("human_blocker_phase", ""))
    if blocked_phase in {"developing", "repairing"}:
        return "development"
    if blocked_phase in {"candidate", "reviewing", "validating"}:
        return "reviewer"
    if blocked_phase in {"accepted", "publishing", "pending"}:
        return "publication"
    return None


def _worker_name(role: str | None, *, run: bool = False) -> str | None:
    if role == "development":
        return "开发工作代理"
    if role == "reviewer":
        return "运行验收工作代理" if run else "独立验收工作代理"
    if role == "publication":
        return "运行发布工作代理" if run else "发布工作代理"
    return None


def _worker_attempt(subject: dict[str, Any], role: str | None) -> int | None:
    if role == "development":
        value = subject.get("pending_attempt", subject.get("modification_attempts"))
    elif role == "reviewer":
        value = subject.get("validation_attempts")
    elif role == "publication":
        value = subject.get("publication_attempts")
    else:
        return None
    return value if isinstance(value, int) else None


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

    sanitized = _sanitize_error_value(value)
    if not isinstance(sanitized, dict):  # pragma: no cover - typed input is a mapping
        raise ValueError("durable Run state must be a mapping")
    diagnostics = sanitized.get("diagnostics")
    if isinstance(diagnostics, list):
        sanitized["diagnostics"] = [
            _bound_diagnostic(item)
            for item in diagnostics[:MAX_DIAGNOSTIC_ENTRIES]
            if isinstance(item, dict)
        ]
    return sanitized


def _sanitize_error_value(
    value: object, *, diagnostic: bool = False, path: tuple[str, ...] = ()
) -> object:
    if isinstance(value, list):
        return [
            _sanitize_error_value(item, diagnostic=diagnostic, path=path)
            for item in value
        ]
    if isinstance(value, str):
        return bounded_error(value) if diagnostic else redact_credentials(value)
    if not isinstance(value, dict):
        # JSON scalars are immutable. Extensions such as tuples may contain
        # mutable values, so retain their previous deepcopy isolation.
        return value if value is None or type(value) in (bool, int, float) else deepcopy(value)
    sanitized: dict[object, object] = {}
    for key, item in value.items():
        if isinstance(key, str) and _is_ephemeral_payload_key(key):
            continue
        item_path = (*path, key) if isinstance(key, str) else path
        if (
            isinstance(key, str)
            and _is_credential_key(key)
            and not _is_durable_protocol_identity(item_path)
        ):
            sanitized[key] = "[REDACTED]"
            continue
        is_diagnostic = key == "diagnostics"
        if diagnostic and isinstance(item, str):
            sanitized[key] = bounded_error(item)
        elif isinstance(key, str) and (
            key == "error" or key.endswith("_error")
        ) and isinstance(item, str):
            sanitized[key] = bounded_error(item)
        else:
            sanitized[key] = _sanitize_error_value(
                item,
                diagnostic=diagnostic or is_diagnostic,
                path=item_path,
            )
    return sanitized


def _normalized_key(value: str) -> str:
    return value.casefold().replace("_", "").replace("-", "").replace(" ", "")


def _is_ephemeral_payload_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return (
        normalized in {"env", "stdout", "stderr"}
        or "environment" in normalized
        or "transcript" in normalized
        or "rawoutput" in normalized
    )


def _is_credential_key(value: str) -> bool:
    normalized = _normalized_key(value)
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
    } or path[-2:] == ("candidate_commit_intent", "token") or (
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
