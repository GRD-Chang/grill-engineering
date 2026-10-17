"""控制授权每次核验 payload，但无需重复解析刚读取的 JSON。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run import task_control
from agent_run.task_control import TaskControlError, TaskControlStore, TaskKey


def _starting_executor(tmp_path: Path, payload: dict):
    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload=payload)
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=None).generation
    return control, task, {"action_id": claim.action_id, "generation": generation}


def test_each_authorization_decodes_current_and_historical_payloads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = TaskKey(tmp_path, "example/project", 1)
    previous_payload = {"message": "原始\x00\n意图🙂", "nested": [None, True, 1.25, {"a": 2}]}
    first = control.claim_action(task, kind="run", payload=previous_payload)
    control.fail_action(task, action_id=first.action_id, failure="before Executor allocation")
    current_payload = {"message": "当前冻结意图", "parent": 1}
    current = control.claim_action(task, kind="resume", payload=current_payload)
    generation = control.begin_executor(task, action_id=current.action_id, run_id=None).generation
    original_loads = json.loads
    decoded = []

    def loads(raw, *args, **kwargs):
        value = original_loads(raw, *args, **kwargs)
        decoded.append(value)
        return value

    with monkeypatch.context() as reading:
        reading.setattr(task_control.json, "loads", loads)
        for _ in range(2):
            decoded.clear()
            control.assert_executor_current(task, action_id=current.action_id, generation=generation)
            assert len(decoded) == 2
            assert previous_payload in decoded and current_payload in decoded
    reopened = TaskControlStore(control.root).load(task)
    assert reopened["action"]["payload"] == current_payload
    assert reopened["action_history"][0]["action"]["payload"] == previous_payload


@pytest.mark.parametrize("damage", ["raw-digest", "canonical-digest", "declared-size", "default-size"])
def test_authorization_rechecks_raw_material_canonical_digest_and_original_size_bound(
    tmp_path: Path, damage: str,
) -> None:
    control, task, identity = _starting_executor(tmp_path, {"value": "original"})
    control.assert_executor_current(task, **identity)
    if damage == "canonical-digest":
        # SQL's raw digest is valid, but decoding duplicate keys changes the
        # canonical payload. The independent canonical digest must still fail.
        raw = '{"value":1,"value":2}'
    elif damage == "default-size":
        raw = json.dumps({"items": ["x"] * 7000}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert len(raw.encode()) <= 32 * 1024
        assert len(json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True).encode()) > 32 * 1024
    else:
        raw = '{"value":"changed"}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    declared = len(raw.encode()) + (1 if damage == "declared-size" else 0)
    with closing(sqlite3.connect(control._repository.path)) as connection, connection:
        connection.execute("UPDATE control_payloads SET value=?,byte_length=? WHERE task_id=? AND action_id=?",
                           (raw, declared, task.fingerprint, identity["action_id"]))
        if damage != "raw-digest":
            connection.execute("UPDATE control_actions SET payload_digest=? WHERE task_id=? AND action_id=?",
                               (digest, task.fingerprint, identity["action_id"]))
    with pytest.raises(TaskControlError, match="payload.*(invalid|digest)"):
        control.assert_executor_current(task, **identity)


def test_authorization_accepts_exact_original_utf8_payload_limit(tmp_path: Path) -> None:
    overhead = len(json.dumps({"body": ""}, ensure_ascii=False, sort_keys=True).encode())
    count, remaining = divmod(32 * 1024 - overhead, len("界".encode()))
    payload = {"body": "界" * count + "x" * remaining}
    assert len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()) == 32 * 1024
    control, task, identity = _starting_executor(tmp_path, payload)
    control.assert_executor_current(task, **identity)
    assert control.load(task)["action"]["payload"] == payload
    with pytest.raises(TaskControlError, match="exceeds the size bound"):
        control.claim_action(task, kind="run", payload={"body": payload["body"] + "x"})


def test_admission_still_normalizes_non_string_keys_before_digesting() -> None:
    encoded = json.dumps({"1": {"3": "保留🙂"}, "2": [True, None]}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    assert task_control.payload_digest({1: {3: "保留🙂"}, 2: [True, None]}) == hashlib.sha256(encoded.encode()).hexdigest()
