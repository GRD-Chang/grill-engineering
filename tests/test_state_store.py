from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from agent_run.run_locator import MAX_LOCATOR_ENTRIES, RunLocatorIndex
from agent_run.state import (
    MAX_TIMELINE_CONTINUATION_EVENTS,
    MAX_TIMELINE_EVENTS,
    StateStore,
)


def test_run_locator_prunes_missing_directories_and_bounds_entries(tmp_path: Path) -> None:
    times = iter(
        datetime.fromisoformat(f"2026-08-15T00:00:{second:02d}+00:00")
        for second in range(MAX_LOCATOR_ENTRIES + 3)
    )
    index = RunLocatorIndex(tmp_path / "locator.json", now=lambda: next(times))
    for number in range(MAX_LOCATOR_ENTRIES + 1):
        state_dir = tmp_path / f"state-{number}"
        state_dir.mkdir()
        index.register(
            run_id=f"run-{number}",
            repository_root=tmp_path / "repo",
            state_dir=state_dir,
        )

    entries = json.loads(
        (tmp_path / "locator.json").read_text(encoding="utf-8")
    )["entries"]
    assert len(entries) == MAX_LOCATOR_ENTRIES
    assert {entry["run_id"] for entry in entries} == {
        f"run-{number}" for number in range(1, MAX_LOCATOR_ENTRIES + 1)
    }
    stale = tmp_path / "state-1"
    stale.rmdir()
    fresh = tmp_path / "state-fresh"
    fresh.mkdir()

    index.register(
        run_id="run-fresh", repository_root=tmp_path / "repo", state_dir=fresh
    )

    entries = json.loads(
        (tmp_path / "locator.json").read_text(encoding="utf-8")
    )["entries"]
    assert len(entries) == MAX_LOCATOR_ENTRIES
    assert "run-1" not in {entry["run_id"] for entry in entries}
    assert "run-fresh" in {entry["run_id"] for entry in entries}


def test_run_locator_atomic_replace_preserves_previous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = tmp_path / "locator.json"
    index = RunLocatorIndex(index_path)
    first_state = tmp_path / "first-state"
    second_state = tmp_path / "second-state"
    first_state.mkdir()
    second_state.mkdir()
    index.register(
        run_id="run-1", repository_root=tmp_path / "repo", state_dir=first_state
    )
    before = index_path.read_text(encoding="utf-8")
    original_replace = os.replace

    def fail_locator_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == index_path:
            raise OSError("simulated locator interruption")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_locator_replace)

    with pytest.raises(OSError, match="simulated locator interruption"):
        index.register(
            run_id="run-2", repository_root=tmp_path / "repo", state_dir=second_state
        )

    assert index_path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob(".run-locator.*.tmp"))


def test_interrupted_replace_preserves_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save_run("run-1", {"attempt": 1})
    original_replace = os.replace

    def fail_run_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "run-1.json":
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_run_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        store.save_run("run-1", {"attempt": 2})

    persisted = json.loads(
        (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")
    )
    assert persisted == {"attempt": 1}
    assert not list((tmp_path / "runs").glob("*.tmp"))


def test_fresh_reviewer_human_blocker_history_uses_reviewer_thread(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    state = {
        "run_id": "run-1",
        "status": "ready_for_human",
        "active_ticket_job": {
            "ticket_number": 3,
            "phase": "blocked",
            "blocked_reason": "reviewer_requires_human",
            "human_blocker_phase": "candidate",
            "human_blockers": ["A maintainer must restore GitHub access."],
            "development_thread_id": "development-thread",
            "reviewer_thread_ids": ["blocked-reviewer-thread"],
            "validation_attempts": 1,
        },
    }

    store.save_run("run-1", state)

    event = state["timeline"][-1]
    assert event["worker"] == "独立验收工作代理"
    assert event["thread_id"] == "blocked-reviewer-thread"
    assert event["human_blockers"] == [
        "A maintainer must restore GitHub access."
    ]


def test_blocked_repair_history_ignores_stale_publication_thread(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    state = {
        "run_id": "run-1",
        "status": "ready_for_human",
        "parent_job": {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blocker_phase": "repairing",
            "human_blockers": ["A maintainer must restore GitHub access."],
            "development_thread_id": "blocked-development-thread",
            "publication_thread_id": "stale-publication-thread",
            "pending_attempt": 2,
        },
    }

    store.save_run("run-1", state)

    event = state["timeline"][-1]
    assert event["worker"] == "开发工作代理"
    assert event["thread_id"] == "blocked-development-thread"


@pytest.mark.parametrize(
    ("blocked_reason", "human_blocker_phase", "expected_worker", "expected_thread"),
    [
        (
            "reviewer_requires_human",
            "candidate",
            "独立验收工作代理",
            "blocked-reviewer-thread",
        ),
        (
            "agent_requires_human",
            "accepted",
            "发布工作代理",
            "blocked-publication-thread",
        ),
    ],
)
def test_run_repair_blocker_history_uses_repair_worker_context(
    tmp_path: Path,
    blocked_reason: str,
    human_blocker_phase: str,
    expected_worker: str,
    expected_thread: str,
) -> None:
    store = StateStore(tmp_path)
    state = {
        "run_id": "run-1",
        "status": "ready_for_human",
        "run_acceptance": {
            "phase": "repairing",
            "validation_attempts": 2,
            "repair_job": {
                "phase": "blocked",
                "blocked_reason": blocked_reason,
                "human_blocker_phase": human_blocker_phase,
                "human_blockers": ["A maintainer must restore GitHub access."],
                "development_thread_id": "development-thread",
                "reviewer_thread_ids": ["blocked-reviewer-thread"],
                "publication_thread_id": "blocked-publication-thread",
                "validation_attempts": 1,
                "publication_attempts": 1,
            },
        },
    }

    store.save_run("run-1", state)

    event = state["timeline"][-1]
    assert event["worker"] == expected_worker
    assert event["thread_id"] == expected_thread


def test_final_publication_blocker_history_identifies_publication_worker(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    state = {
        "run_id": "run-1",
        "status": "ready_for_human",
        "run_publication": {
            "phase": "ready_for_human",
            "human_blocker_phase": "pending",
            "human_blockers": ["A maintainer must restore GitHub access."],
            "thread_id": "blocked-final-publication-thread",
            "publication_attempts": 1,
        },
    }

    store.save_run("run-1", state)

    event = state["timeline"][-1]
    assert event["worker"] == "运行发布工作代理"
    assert event["thread_id"] == "blocked-final-publication-thread"


def test_unsupported_scope_change_uses_bounded_deduplicated_history(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    summary = {
        "summary": "Ticket graph changed.",
        "added_tickets": [3],
        "removed_tickets": [],
        "added_dependencies": [],
        "removed_dependencies": [],
    }
    state = {
        "run_id": "run-1",
        "status": "unsupported_scope_change",
        "unsupported_scope_change": {
            "accepted_graph_revision": "accepted",
            "observed_graph_revision": "observed",
            "graph_change_summary": summary,
        },
    }

    store.save_run("run-1", state)
    store.save_run("run-1", state)

    assert state["timeline"] == [
        {
            "at": state["timeline"][0]["at"],
            "kind": "unsupported_scope_change",
            "status": "unsupported_scope_change",
            "accepted_graph_revision": "accepted",
            "observed_graph_revision": "observed",
            "graph_change_summary": summary,
            "next_action": "restore_graph_or_abandon",
            "result": "Ticket graph changed.",
        }
    ]

    final_revision = f"observed-{MAX_TIMELINE_EVENTS * 2 + 9}"
    for number in range(MAX_TIMELINE_EVENTS * 2 + 10):
        change = state["unsupported_scope_change"]
        assert isinstance(change, dict)
        change["observed_graph_revision"] = f"observed-{number}"
        store.save_run("run-1", state)

    assert len(state["timeline"]) == MAX_TIMELINE_EVENTS
    assert state["timeline"][-1]["kind"] == "timeline_capacity"
    assert state["timeline_at_capacity"] is True
    assert len(state["timeline_continuation"]) == MAX_TIMELINE_CONTINUATION_EVENTS
    assert state["timeline_continuation"][-1]["observed_graph_revision"] == (
        final_revision
    )
