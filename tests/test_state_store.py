from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_errors import IncompatibleRunStateError

from agent_run.run_locator import MAX_LOCATOR_ENTRIES, RunLocatorIndex
from agent_run.state import (
    ConcurrentRunStateError,
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_ENTRIES,
    MAX_RUN_STATE_BYTES,
    MAX_TIMELINE_CONTINUATION_EVENTS,
    MAX_TIMELINE_EVENTS,
    StateStore,
)


def test_shared_save_sanitizer_keeps_path_and_diagnostic_rules() -> None:
    from agent_run.state import _sanitize_durable_errors

    redacted: dict[str, str] = {}
    kinds: dict[str, tuple[bool, bool, bool]] = {}
    text = "plain evidence " * 900
    first = _sanitize_durable_errors(
        {"candidate_commit_intent": {"token": "same-nonce"}, "note": text},
        redacted=redacted, key_kinds=kinds,
    )
    second = _sanitize_durable_errors(
        {"untrusted": {"token": "same-nonce", "environment": {"value": "secret"}},
         "note": text, "diagnostics": [{"code": "failed", "message": text}]},
        redacted=redacted, key_kinds=kinds,
    )
    assert first["candidate_commit_intent"]["token"] == "same-nonce"
    assert second["untrusted"] == {"token": "[REDACTED]"}
    assert second["note"] == text
    assert len(second["diagnostics"][0]["message"].encode()) <= 8 * 1024
    assert first["note"] == text


def test_run_state_saves_only_sqlite_and_reopens_the_same_data(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    state = {"nested": [{"message": "中文状态", "values": [1, True, None]}]}
    store.save_run("run-1", state)
    assert StateStore(store.root).load_run("run-1") == state
    assert (store.root / "state.sqlite3").is_file()
    assert not list(store.runs_directory.glob("*.json"))


def test_legacy_json_is_rejected_without_reading_or_migrating(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.runs_directory.mkdir()
    path = store.runs_directory / "run-1.json"
    path.write_bytes(b"{broken legacy state")
    before = path.read_bytes()
    for operation in (lambda: store.load_run("run-1"),
                      lambda: store.save_run("run-1", {"version": 1}),
                      lambda: store.find_run("example/project", 1)):
        with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
            operation()
    assert path.read_bytes() == before
    assert store.has_run("run-1")
    assert store.list_run_ids() == ["run-1"]


@pytest.mark.parametrize("body", [
    "- [ ] 保持原文\n- [x] 已完成\n示例：[ 1,  2 ]",
    "- [ ] 保持原文\r\n- [x] 已完成\r\n",
    '{ "enabled": true, "limits": [ 1, 2 ] }',
    '{ "example": "first", "example": "last" }',
    '示例：{ "enabled": true }，数值 1e2，- [ ] 验收',
])
def test_requirement_body_survives_persistence_verbatim(tmp_path: Path, body: str) -> None:
    store = StateStore(tmp_path)
    state = {"parent": {"body": body}, "ticket_graph": {"tickets": {"2": {"body": body}}}}
    store.save_run("run-1", state)
    assert store.load_run("run-1") == state


@pytest.mark.parametrize("body", [
    '正文 {"to\\u006ben": "private-credential"} - [ ] 验收',
    '正文 {"x":"\\u0067hp_123456789abcdef", "x":"safe", "token":"private-credential"} - [ ] 验收',
    '正文 {"message": "ghp_12345678\\u0039abcdef"} - [ ] 验收',
    '正文 {"password": {"nested": "private-credential"}} - [ ] 验收',
])
def test_requirement_body_keeps_credential_redaction(tmp_path: Path, body: str) -> None:
    store = StateStore(tmp_path)
    store.save_run("run-1", {"parent": {"body": body}})
    saved = store.load_run("run-1")["parent"]["body"]
    assert "private-credential" not in saved
    assert "ghp_" not in saved
    assert "[REDACTED]" in saved
    assert "- [ ] 验收" in saved


def test_controller_refresh_rejects_a_concurrent_run_write(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save_run("run-1", {"version": 1})
    baseline = store.load_run("run-1")
    assert baseline is not None

    with store.require_unchanged_run("run-1", baseline):
        store.save_run("run-1", {"version": 2})
        store.save_run("run-1", {"version": 3})
        StateStore(store.root).save_run("run-1", {"version": 4})
        with pytest.raises(ConcurrentRunStateError, match="changed"):
            store.save_run("run-1", {"version": 5})

    assert store.load_run("run-1") == {"version": 4}


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
    with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
        store.load_run("run-external")


def test_run_discovery_lists_ids_without_loading_or_creating_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")

    assert store.list_run_ids() == []
    assert not store.has_run("missing")
    assert not store.root.exists()

    store.runs_directory.mkdir(parents=True)
    (store.runs_directory / "broken.json").write_text("{", encoding="utf-8")
    store.save_run("usable", {"run_id": "usable"})

    assert store.list_run_ids() == ["broken", "usable"]
    assert store.has_run("broken")
    assert store.load_run("usable")["run_id"] == "usable"
    with pytest.raises(IncompatibleRunStateError, match="legacy JSON"):
        store.load_run("broken")


def test_run_discovery_enforces_a_caller_supplied_bound_without_loading_records(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.runs_directory.mkdir(parents=True)
    for run_id in ("run-a", "run-b"):
        (store.runs_directory / f"{run_id}.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds the requested limit"):
        store.list_run_ids(limit=1)


def test_state_save_preserves_shared_input_while_redacting_its_durable_copy(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    shared = {
        "values": [None, True, 3, 1.25, "中文状态"],
        "detail": "token=private-value",
        "environment": {"LOCAL_VALUE": "ephemeral-value"},
    }
    state = {"first": shared, "nested": [{"second": shared}]}
    before = deepcopy(state)

    store.save_run("run-1", state)

    assert state == before
    assert state["first"] is state["nested"][0]["second"]
    expected = {
        "values": [None, True, 3, 1.25, "中文状态"],
        "detail": "token=[REDACTED]",
    }
    assert store.load_run("run-1") == {
        "first": expected, "nested": [{"second": expected}],
    }


@pytest.mark.parametrize("diagnostic_first", [False, True])
def test_repeated_text_keeps_contextual_redaction_and_resource_isolation(
    tmp_path: Path, diagnostic_first: bool,
) -> None:
    store = StateStore(tmp_path)
    message = "prefix " + "中文" * 3000 + " token=private-value"
    resource = {"method": "token=personal-instruction", "environment": "keep"}
    diagnostic = {"message": message, "token": "private-value"}
    fields = [("ordinary", message), ("diagnostics", [diagnostic])]
    state = dict(reversed(fields) if diagnostic_first else fields)
    state.update({
        "repeated": ["token=private-value"] * 300,
        "unique": [f"label-{number}" for number in range(300)],
        "after_unique": "token=another-private-value",
        "many_keys": {**{f"label-{n}": "ok" for n in range(300)},
                      "AUTHORIZATION": "private-value", "environ ment": "ephemeral"},
        "prompt_resources": resource,
        "nested": {"prompt_resources": resource},
        "candidate_commit_intent": {"token": "same-value"},
        "untrusted": {"token": "same-value"},
    })
    before = deepcopy(state)

    store.save_run("run-1", state)

    assert state == before
    saved = store.load_run("run-1")
    assert saved is not None
    assert saved["ordinary"] == "prefix " + "中文" * 3000 + " token=[REDACTED]"
    assert len(saved["diagnostics"][0]["message"].encode()) <= 8192
    assert saved["diagnostics"][0]["token"] == "[REDACTED]"
    assert saved["repeated"] == ["token=[REDACTED]"] * 300
    assert saved["unique"] == state["unique"]
    assert saved["after_unique"] == "token=[REDACTED]"
    assert saved["many_keys"] == {**{f"label-{n}": "ok" for n in range(300)},
                                  "AUTHORIZATION": "[REDACTED]"}
    assert saved["prompt_resources"] == resource
    assert saved["nested"]["prompt_resources"] == {"method": "token=[REDACTED]"}
    assert saved["candidate_commit_intent"]["token"] == "same-value"
    assert saved["untrusted"]["token"] == "[REDACTED]"
    resource["method"] = "updated instruction"
    state["repeated"][0] = "token=changed-private-value"
    store.save_run("run-1", state)
    assert saved["prompt_resources"]["method"] == "token=personal-instruction"
    updated = store.load_run("run-1")
    assert updated is not None
    assert updated["prompt_resources"]["method"] == "updated instruction"
    assert updated["repeated"] == ["token=[REDACTED]"] * 300


def test_scalar_leaves_keep_redaction_bounds_and_json_types(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    message = "中文" * 3000
    leaves = {"empty": "", "null": None, "boolean": False, "integer": 2, "float": 1.5}
    state = {
        "leaves": leaves,
        "list": [leaves, "", "token=private", None, False, 2, 1.5],
        "ordinary": message,
        "nested": {"diagnostics": message, "failure_error": message},
    }
    store.save_run("leaves", state)
    saved = store.load_run("leaves")
    assert saved is not None
    assert saved["leaves"] == leaves
    assert saved["list"] == [leaves, "", "token=[REDACTED]", None, False, 2, 1.5]
    assert saved["ordinary"] == message
    for field in ("diagnostics", "failure_error"):
        assert saved["nested"][field] == message.encode()[:8192].decode(errors="ignore")


@pytest.mark.parametrize("container", [list, tuple])
def test_saved_history_does_not_share_nested_values_with_the_callers_payload(
    tmp_path: Path, container: type[list] | type[tuple],
) -> None:
    store = StateStore(tmp_path)
    shared = [{"message": "original"}]
    state = {
        "run_id": "run-1", "status": "created", "payload": shared,
        "timeline": [{
            "at": "2026-09-12T00:00:00+00:00", "kind": "run_status",
            "status": "created", "context": container([shared]),
        }],
    }

    store.save_run("run-1", state)
    state["timeline"][0]["context"][0][0]["message"] = "edited history"

    assert shared == [{"message": "original"}]
    persisted = store.load_run("run-1")
    assert persisted is not None
    assert persisted["payload"] == [{"message": "original"}]
    assert persisted["timeline"][0]["context"] == [[{"message": "original"}]]


def test_unserializable_extension_keeps_the_previous_durable_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    store.save_run("run-1", {"version": 1})
    unsupported = {"version": 2, "extension": {"not-json"}}

    with pytest.raises(TypeError, match="not JSON serializable"):
        store.save_run("run-1", unsupported)

    assert unsupported == {"version": 2, "extension": {"not-json"}}
    assert store.load_run("run-1") == {"version": 1}
    assert not list(store.runs_directory.glob("*.tmp"))


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

    persisted_bytes = (store.root / "state.sqlite3").read_bytes()
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


def test_interrupted_transaction_preserves_previous_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save_run("run-1", {"attempt": 1})
    repository = SQLiteStateRepository(tmp_path, max_bytes=MAX_RUN_STATE_BYTES)
    with pytest.raises(OSError, match="simulated interruption"):
        with repository.atomic():
            repository.save("run-1", {"attempt": 2})
            raise OSError("simulated interruption")
    assert StateStore(tmp_path).load_run("run-1") == {"attempt": 1}
    assert not list(tmp_path.glob("*journal"))


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
            "repairing",
            "开发工作代理",
            "development-thread",
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


@pytest.mark.parametrize("identity", [
    {"source": "host"},
    {"source": "pat", "pat": {"env": "CUSTOM_WORKER_PAT"}},
    {"source": "pat", "pat": {"file": "/secure/credential"}},
    *({"source": "app", "app_profile": {"app_id": "1", "installation_id": "2",
                                         "private_key_path": "/secure/app.pem"},
       "app_read_only": read_only, "app_current_repository_only": current_repository_only}
      for read_only in (False, True) for current_repository_only in (False, True)),
])
def test_identity_reference_survives_state_redaction(tmp_path: Path, identity: dict) -> None:
    store = StateStore(tmp_path)
    store.save_run("identity", {"github_identity": identity,
                               "creation_configuration": {"github_identity": identity},
                               "extension": {"env": {"CUSTOM_WORKER_PAT": "secret"}}})
    persisted = store.load_run("identity")
    assert persisted["github_identity"] == identity
    assert persisted["creation_configuration"]["github_identity"] == identity
    assert persisted["extension"] == {}
    with pytest.raises(ValueError):
        store.save_run("identity", {"github_identity": {**identity, "token": "secret"}})
    assert store.load_run("identity") == persisted

    if identity["source"] == "app":
        for field in ("app_read_only", "app_current_repository_only"):
            legacy = {key: value for key, value in identity.items() if key != field}
            with pytest.raises(ValueError):
                store.save_run("identity", {"github_identity": legacy})
            assert store.load_run("identity") == persisted
