from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAX_TIMELINE_EVENTS = 256


class SimulatedProcessCrash(OSError):
    """Fault-injection signal whose cleanup matches an abrupt process exit."""


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_directory = root / "runs"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        destination = self.runs_directory / f"{run_id}.json"
        previous = self.load_run(run_id)
        if "run_id" in state:
            _append_timeline_event(state, previous)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.runs_directory,
            prefix=f".{run_id}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    state,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary_file.write("\n")
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
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid run state: {path}")
        return loaded

    def find_run(self, repository: str, parent_number: int) -> dict[str, Any] | None:
        if not self.runs_directory.exists():
            return None
        matches: list[dict[str, Any]] = []
        for path in self.runs_directory.glob("*.json"):
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
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
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
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
    marker["result"] = _timeline_result(state)
    if previous is not None:
        previous_marker = _timeline_marker(previous)
        previous_marker["result"] = _timeline_result(previous)
        if marker == previous_marker:
            return
    timeline = state.setdefault("timeline", [])
    if not isinstance(timeline, list):
        raise ValueError("timeline must be an array")
    if state.get("timeline_at_capacity") is True:
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
        return
    event = {
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
        "result",
    ):
        value = marker.get(key)
        if value is not None:
            event[key] = value
    if _event_matches_marker(timeline[-1] if timeline else None, marker):
        return
    timeline.append(event)


def _timeline_marker(state: dict[str, Any]) -> dict[str, object]:
    status = str(state.get("status", "unknown"))
    publication = state.get("run_publication")
    if status in {
        "run_publication_pending",
        "waiting_checks",
        "run_approval_pending",
        "parent_closeout_pending",
        "completed",
        "abandoned",
    } and isinstance(publication, dict):
        phase = str(publication.get("phase", "pending"))
        return {
            "kind": "run_publication",
            "status": status,
            "worker": "运行发布工作代理" if phase == "publishing" else None,
            "attempt": publication.get("publication_attempts"),
            "thread_id": publication.get("thread_id"),
            "phase": phase,
            "pr_number": publication.get("pr_number"),
            "commit_sha": publication.get("integrated_sha"),
        }
    run_acceptance = state.get("run_acceptance")
    if status in {"run_acceptance_pending", "ready_for_human"} and isinstance(
        run_acceptance, dict
    ):
        phase = str(run_acceptance.get("phase", "pending"))
        return {
            "kind": "run_acceptance",
            "status": status,
            "worker": "运行验收工作代理" if phase == "reviewing" else None,
            "attempt": run_acceptance.get("validation_attempts"),
            "thread_id": _latest_thread_id(run_acceptance, reviewing=phase == "reviewing"),
            "phase": phase,
        }
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        return _job_timeline_marker(active, status)
    parent_job = state.get("parent_job")
    if isinstance(parent_job, dict):
        return _job_timeline_marker(parent_job, status)
    return {"kind": "run_status", "status": status}


def _job_timeline_marker(job: dict[str, Any], status: str) -> dict[str, object]:
    phase = str(job.get("phase", "pending"))
    if phase in {"developing", "repairing", "committing_candidate"}:
        worker = "开发工作代理"
        attempt = job.get("pending_attempt", job.get("modification_attempts"))
    elif phase in {"reviewing", "validating"}:
        worker = "独立验收工作代理"
        attempt = job.get("validation_attempts")
    elif phase in {"publishing", "publication_pending"}:
        worker = "发布工作代理"
        attempt = job.get("publication_attempts")
    else:
        worker = None
        attempt = None
    ticket = job.get("ticket_number")
    return {
        "kind": "ticket_phase" if isinstance(ticket, int) else "parent_phase",
        "status": status,
        "ticket": ticket if isinstance(ticket, int) else None,
        "worker": worker,
        "attempt": attempt if isinstance(attempt, int) else None,
        "thread_id": _latest_thread_id(
            job,
            reviewing=phase in {"reviewing", "validating"},
            developing=phase in {"developing", "repairing"},
        ),
        "phase": phase,
        "pr_number": job.get("pr_number"),
        "commit_sha": job.get("integrated_sha")
        or job.get("publication_sha")
        or job.get("candidate_sha"),
    }


def _latest_thread_id(
    job: dict[str, Any], *, reviewing: bool = False, developing: bool = False
) -> object:
    if developing and isinstance(job.get("pending_attempt"), int):
        return None
    if not reviewing:
        publication_thread_id = job.get("publication_thread_id")
        if isinstance(publication_thread_id, str):
            return publication_thread_id
    reviewer_ids = job.get("reviewer_thread_ids")
    if reviewing and isinstance(reviewer_ids, list) and reviewer_ids:
        latest = reviewer_ids[-1]
        if isinstance(latest, str):
            return latest
    thread_id = job.get("development_thread_id")
    if isinstance(thread_id, str):
        return thread_id
    if isinstance(reviewer_ids, list) and reviewer_ids:
        latest = reviewer_ids[-1]
        if isinstance(latest, str):
            return latest
    return None


def _event_matches_marker(event: object, marker: dict[str, object]) -> bool:
    if not isinstance(event, dict):
        return False
    return all(event.get(key) == value for key, value in marker.items())


def _timeline_result(state: dict[str, Any]) -> object:
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


class FaultInjectingStateStore(StateStore):
    """Black-box test seam that crashes after one durable state write."""

    def __init__(self, root: Path, *, crash_after_save: int) -> None:
        if crash_after_save < 1:
            raise ValueError("crash_after_save must be positive")
        super().__init__(root)
        self.crash_after_save = crash_after_save
        self.save_count = 0
        self.injected = False

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        super().save_run(run_id, state)
        self.save_count += 1
        if not self.injected and self.save_count == self.crash_after_save:
            self.injected = True
            raise SimulatedProcessCrash(
                f"simulated crash after durable save #{self.save_count}"
            )
