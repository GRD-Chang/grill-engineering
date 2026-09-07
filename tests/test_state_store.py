from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from agent_run.run_locator import MAX_LOCATOR_ENTRIES, RunLocatorIndex
from agent_run.state import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_ENTRIES,
    MAX_RUN_STATE_BYTES,
    MAX_TIMELINE_CONTINUATION_EVENTS,
    MAX_TIMELINE_EVENTS,
    StateStore,
)


def test_run_state_persistence_rejects_oversized_write_and_read(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    with pytest.raises(ValueError, match="persistence limit"):
        store.save_run("run-large", {"payload": "x" * MAX_RUN_STATE_BYTES})
    assert not (store.runs_directory / "run-large.json").exists()

    store.runs_directory.mkdir(parents=True, exist_ok=True)
    oversized = store.runs_directory / "run-external.json"
    oversized.write_bytes(b" " * (MAX_RUN_STATE_BYTES + 1))
    with pytest.raises(ValueError, match="persistence limit"):
        store.load_run("run-external")


def test_run_state_persistence_bounds_diagnostics(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    state = {
        "diagnostics": [
            {"code": f"code-{number}", "message": "bounded"}
            for number in range(MAX_DIAGNOSTIC_ENTRIES + 5)
        ]
    }

    store.save_run("run-1", state)

    persisted = store.load_run("run-1")
    assert persisted is not None
    assert len(persisted["diagnostics"]) == MAX_DIAGNOSTIC_ENTRIES


def test_run_state_persistence_bounds_each_diagnostic_and_drops_raw_payloads(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    credential = "ghp_1234567890abcdef"
    raw_output = "raw-output-sentinel" * 32_768
    state = {
        "diagnostics": [
            {
                "code": "producer_failure",
                "message": f"token={credential}\n" + ("failure " * 20_000),
                "context": {
                    "credentials": {"authorization": f"Bearer {credential}"},
                    "transcript": raw_output,
                    "raw_output": raw_output,
                    "detail": "bounded detail " * 5_000,
                },
            }
        ],
        "extension": {
            "environment": {"GH_TOKEN": credential},
            "credentials": {"token": credential},
            "transcript": raw_output,
            "raw-output": raw_output,
            "full_transcript": raw_output,
            "raw_output_bytes": raw_output,
            "credential_bundle": {"session_cookie": credential},
        },
        "active_ticket_job": {
            "candidate_commit_intent": {"token": "candidate-intent-nonce"}
        },
        "credential_availability": {
            "status": "waiting",
            "detail": {"token": credential},
        },
    }

    store.save_run("run-1", state)

    path = store.runs_directory / "run-1.json"
    persisted_bytes = path.read_bytes()
    persisted = store.load_run("run-1")
    assert persisted is not None
    diagnostic = persisted["diagnostics"][0]
    assert len(
        json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")).encode()
    ) <= MAX_DIAGNOSTIC_BYTES
    assert credential.encode() not in persisted_bytes
    assert b"raw-output-sentinel" not in persisted_bytes
    assert "environment" not in persisted["extension"]
    assert "transcript" not in persisted["extension"]
    assert "raw-output" not in persisted["extension"]
    assert "full_transcript" not in persisted["extension"]
    assert "raw_output_bytes" not in persisted["extension"]
    assert persisted["extension"]["credentials"] == "[REDACTED]"
    assert persisted["extension"]["credential_bundle"] == "[REDACTED]"
    assert (
        persisted["active_ticket_job"]["candidate_commit_intent"]["token"]
        == "candidate-intent-nonce"
    )
    assert persisted["credential_availability"] == {
        "status": "waiting",
        "detail": {"token": "[REDACTED]"},
    }


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

    assert store.load_run("run-1")["timeline"] == state["timeline"]
    assert MAX_TIMELINE_EVENTS == MAX_TIMELINE_CONTINUATION_EVENTS == 256
    first_event = dict(state["timeline"][0])
    change = state["unsupported_scope_change"]
    # Existing history is input data. Exercise the actual persistence boundary
    # at its last ordinary slot, capacity marker, and continuation rollover.
    state["timeline"] += [
        {**first_event, "observed_graph_revision": f"history-{number}"}
        for number in range(1, MAX_TIMELINE_EVENTS - 2)
    ]
    change["observed_graph_revision"] = "history-253"
    store.save_run("run-1", state)
    assert len(store.load_run("run-1")["timeline"]) == 254

    change["observed_graph_revision"] = "last-ordinary-event"
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert len(persisted["timeline"]) == 255
    assert persisted["timeline"][-1]["observed_graph_revision"] == "last-ordinary-event"
    assert persisted.get("timeline_at_capacity", False) is False

    change["observed_graph_revision"] = "first-continuation-event"
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert len(persisted["timeline"]) == 256
    assert persisted["timeline"][0] == first_event
    assert persisted["timeline"][-1]["kind"] == "timeline_capacity"
    assert persisted["timeline_at_capacity"] is True
    frozen_timeline = persisted["timeline"]
    first_continuation = persisted["timeline_continuation"][0]
    assert first_continuation["observed_graph_revision"] == "first-continuation-event"

    state["timeline_continuation"] += [
        {**first_continuation, "observed_graph_revision": f"continuation-{number}"}
        for number in range(1, MAX_TIMELINE_CONTINUATION_EVENTS - 1)
    ]
    change["observed_graph_revision"] = "continuation-254"
    store.save_run("run-1", state)
    assert len(store.load_run("run-1")["timeline_continuation"]) == 255

    change["observed_graph_revision"] = "last-continuation-slot"
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert len(persisted["timeline_continuation"]) == 256
    assert persisted["timeline_continuation"][0] == first_continuation
    assert persisted["timeline_continuation"][-1]["observed_graph_revision"] == (
        "last-continuation-slot"
    )

    change["observed_graph_revision"] = "continuation-after-rollover"
    store.save_run("run-1", state)
    persisted = store.load_run("run-1")
    assert persisted["timeline"] == frozen_timeline
    assert persisted["timeline_at_capacity"] is True
    assert len(persisted["timeline_continuation"]) == 256
    assert persisted["timeline_continuation"][0]["observed_graph_revision"] == (
        "continuation-1"
    )
    assert persisted["timeline_continuation"][-1]["observed_graph_revision"] == (
        "continuation-after-rollover"
    )

    store.save_run("run-1", state)
    assert store.load_run("run-1") == persisted
