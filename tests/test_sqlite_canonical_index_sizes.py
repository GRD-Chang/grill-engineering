"""索引大小来自本次恢复的规范文本，覆写与实际元数据仍须验证。"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository


def _state() -> dict[str, Any]:
    invocation = {"work_subject": "ticket:2", "started_at": "2026-01-01T00:00:00Z",
                  "number": 1e100, "review_context": {"body": "中文🙂证据" * 200},
                  "execution_segments": [{"started_at": "start", "ended_at": "end"}]}
    job = {"ticket_number": 2, "semantic_attempt_history": [
        {"role": "reviewer", "ordinal": 1, "status": "completed", "number": 1e100},
    ], "human_blocker_history": [{"number": 1e100, "message": "历史问题🙂"}]}
    return {"phase": "active", "ticket_agent_invocations": {"2": deepcopy(invocation)},
            "agent_invocation_history": [invocation], "ticket_jobs": {"2": job}}


def _rewrite_numbers(repository: SQLiteStateRepository) -> None:
    with closing(sqlite3.connect(repository.path)) as connection:
        rows = connection.execute("SELECT material_id,value FROM run_materials WHERE instr(value,'1e+100')>0").fetchall()
        for previous, raw in rows:
            text = raw.replace("1e+100", "1e+0100")
            identity = hashlib.sha256(text.encode("utf-8")).hexdigest()
            connection.execute("INSERT INTO run_materials VALUES (?,?,?)", (identity, text, len(text.encode("utf-8"))))
            connection.execute("UPDATE run_objects SET value_json=? WHERE value_json=?", (encode_json(identity), encode_json(previous)))
            connection.execute("UPDATE run_object_material_refs SET material_id=? WHERE material_id=?", (identity, previous))
            connection.execute("UPDATE run_material_refs SET material_id=? WHERE material_id=?", (identity, previous))
            connection.execute("UPDATE run_object_materials SET material_id=? WHERE material_id=?", (identity, previous))
            connection.execute("DELETE FROM run_materials WHERE material_id=?", (previous,))
        connection.commit()
        assert rows


@pytest.mark.parametrize("reader", ["full", "work"])
def test_index_sizes_use_canonical_restoration_of_noncanonical_numbers(tmp_path: Path, reader: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    _rewrite_numbers(repository)
    read = lambda: repository.load("run-1") if reader == "full" else repository.load_work("run-1", 2)
    assert read() == original
    # Same-source cached encodings remain independent from returned containers.
    loaded = read()
    loaded["agent_invocation_history"][0]["review_context"]["body"] = "caller mutation"
    assert read() == original
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_history SET logical_bytes=logical_bytes+1")
        connection.commit()
    with pytest.raises(ValueError, match="history index"):
        read()


@pytest.mark.parametrize("reader", ["full", "work"])
@pytest.mark.parametrize("archive", ["semantic_attempt_history", "human_blocker_history"])
def test_canonical_archive_sizes_still_reject_actual_logical_size_damage(
    tmp_path: Path, reader: str, archive: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    _rewrite_numbers(repository)
    read = lambda: repository.load("run-1") if reader == "full" else repository.load_work("run-1", 2)
    assert read() == original
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_job_history SET logical_bytes=logical_bytes+1 WHERE object_path=?",
                           (encode_json(("ticket_jobs", "2", archive)),))
        connection.commit()
    with pytest.raises(ValueError, match="Job history size"):
        read()


def test_complete_history_descendant_override_sizes_the_assembled_entry(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    expected = deepcopy(original)
    expected["agent_invocation_history"][0]["summary"] = "actual descendant🙂"
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)", (
            "run-1", encode_json(("agent_invocation_history", 0, "summary")), "scalar", json.dumps("actual descendant🙂")))
        connection.execute("UPDATE run_sizes SET logical_bytes=?", (repository._check_size(expected),))
        connection.execute("UPDATE run_history SET logical_bytes=?", (len(encode_json(expected["agent_invocation_history"][0]).encode()),))
        connection.commit()
    # The second read reuses a complete field encoding but must still exclude
    # the separately overridden snapshot when deriving its history metadata.
    assert repository.load("run-1") == expected
    expected["phase"] = "paused"
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?",
                           (encode_json("paused"), encode_json(("phase",))))
        connection.commit()
    assert repository.load("run-1") == expected


def test_archive_descendant_override_sizes_the_assembled_archive(tmp_path: Path) -> None:
    archive = "semantic_attempt_history"
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    expected = deepcopy(original)
    expected["ticket_jobs"]["2"][archive][0]["summary"] = "actual descendant🙂"
    path = encode_json(("ticket_jobs", "2", archive))
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)", (
            "run-1", encode_json(("ticket_jobs", "2", archive, 0, "summary")), "scalar", json.dumps("actual descendant🙂")))
        connection.execute("UPDATE run_sizes SET logical_bytes=?", (repository._check_size(expected),))
        connection.execute("UPDATE run_job_history SET logical_bytes=? WHERE object_path=?",
                           (len(encode_json(expected["ticket_jobs"]["2"][archive]).encode()), path))
        connection.commit()
    assert repository.load("run-1") == expected
    expected["phase"] = "paused"
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_objects SET value_json=? WHERE object_path=?",
                           (encode_json("paused"), encode_json(("phase",))))
        connection.commit()
    assert repository.load("run-1") == expected
    assert repository.load_work("run-1", 2) == expected


@pytest.mark.parametrize("missing_proof", [False, True])
def test_native_selected_history_sizes_local_positions_and_isolates_writer_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_proof: bool,
) -> None:
    from agent_run import sqlite_work_objects

    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    first = original["agent_invocation_history"][0]
    unrelated = {"work_subject": "ticket:3", "started_at": "unselected"}
    resumed = deepcopy(first)
    resumed.update(started_at="resumed", resume_id="audit-two")
    original["agent_invocation_history"] = [first, unrelated, resumed]
    original["resume_audit"] = {"history": [
        {"resume_id": "audit-two", "successor_invocation_started_at": "resumed"},
    ]}
    repository.save("run-1", original)
    if missing_proof:
        prove = sqlite_work_objects._verified_snapshot_encodings

        def incomplete_proof(*args: Any, **kwargs: Any) -> dict[tuple[str | int, ...], str]:
            encodings = prove(*args, **kwargs)
            encodings.pop(("agent_invocation_history", 2), None)
            return encodings

        monkeypatch.setattr(sqlite_work_objects, "_verified_snapshot_encodings", incomplete_proof)
    expected = deepcopy(original)
    expected["agent_invocation_history"] = [deepcopy(first), deepcopy(resumed)]
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == expected
        assert repository._local.work_view.positions == [0, 2]
        assert repository._local.work_projection_bytes == repository._check_size(expected)
        baseline = repository.current_work_baseline("run-1")
        current["agent_invocation_history"][0]["review_context"]["body"] = "caller mutation"
        assert baseline == expected
        assert current["agent_invocation_history"][1] == resumed
        assert current["ticket_agent_invocations"]["2"] == first
    assert repository.load("run-1") == original


def test_native_empty_history_baseline_is_independent(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    original = {"agent_invocation_history": [], "ticket_agent_invocations": {}}
    repository.save("run-1", original)
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == original
        assert repository._local.work_projection_bytes == repository._check_size(original)
        baseline = repository.current_work_baseline("run-1")
        current["agent_invocation_history"].append({"caller": "mutation"})
        assert baseline == original
    assert repository.load("run-1") == original


@pytest.mark.parametrize("damage", ["metadata", "refs"])
def test_selected_history_encoding_cannot_hide_current_index_damage(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "metadata":
            connection.execute("UPDATE run_history SET logical_bytes=logical_bytes+1")
        else:
            connection.execute("DELETE FROM run_object_material_refs WHERE object_path=?",
                               (encode_json(("agent_invocation_history", 0)),))
        connection.commit()
    with pytest.raises(ValueError, match="history index|material references"):
        repository.load_work("run-1", 2)


@pytest.mark.parametrize("shape", ["repeated_identity", "shared_nested_snapshot", "prior_only", "descendant_override"])
def test_fresh_writer_baseline_keeps_all_caller_and_baseline_containers_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    from agent_run import sqlite_work_objects

    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    evidence = {"body": "中文🙂证据" * 200, "nested": [{"count": 1}]}
    original = {"parent": deepcopy(evidence), "ticket": deepcopy(evidence)}
    if shape == "shared_nested_snapshot":
        original = {"parent": {"name": "parent", "review_context": deepcopy(evidence)},
                    "ticket": {"name": "ticket", "review_context": deepcopy(evidence)}}
    repository.save("run-1", original)
    if shape == "prior_only":
        assert repository.load("run-1") == original
    expected = deepcopy(original)
    if shape == "descendant_override":
        expected["parent"]["summary"] = "current source override"
        with closing(sqlite3.connect(repository.path)) as connection:
            connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)", (
                "run-1", encode_json(("parent", "summary")), "scalar", encode_json("current source override")))
            connection.execute("UPDATE run_sizes SET logical_bytes=?", (repository._check_size(expected),))
            connection.commit()
    contexts = []
    decode = sqlite_work_objects._decode

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = decode(*args, **kwargs)
        if kwargs.get("context") is not None:
            contexts.append(kwargs["context"])
        return result

    monkeypatch.setattr(sqlite_work_objects, "_decode", observe)
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == expected
        baseline = repository.current_work_baseline("run-1")
        assert baseline == expected
        context = contexts[0]
        if shape in {"repeated_identity", "shared_nested_snapshot"}:
            # One independent source tree is reused. The other root must own
            # separate containers even when their nested material is shared.
            roots = [baseline[key] for key in ("parent", "ticket")]
            assert sum(any(root is value for value in context.snapshots.values()) for root in roots) == 1
        elif shape == "prior_only":
            assert not context.restored_encodings
        else:
            assert not any(baseline["parent"] is value for value in context.snapshots.values())
        assert baseline["parent"] is not current["parent"]
        assert baseline["ticket"] is not current["ticket"]
        assert baseline["parent"] is not baseline["ticket"]
        parent = current["parent"]
        baseline_parent = baseline["parent"]
        if shape == "shared_nested_snapshot":
            parent = parent["review_context"]
            baseline_parent = baseline_parent["review_context"]
            assert baseline_parent is not baseline["ticket"]["review_context"]
        parent["nested"][0]["count"] = 10
        assert baseline == expected
        assert current["ticket"] == expected["ticket"]
        # Mutation here tests isolation only; the private baseline is otherwise
        # read-only, and no modified baseline is submitted for persistence.
        baseline_parent["nested"][0]["count"] = 20
        assert current["parent"] != baseline["parent"]
        assert baseline["ticket"] == expected["ticket"]
    assert repository.load("run-1") == expected


def test_prior_sibling_snapshot_copy_stays_independent_when_selected_job_has_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import sqlite_work_objects

    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = {"agent_invocation_history": [], "ticket_jobs": {
        "2": {"ticket_number": 2, "semantic_attempt_history": [
            {"role": "reviewer", "status": "completed", "ordinal": 1},
        ]},
        "3": {"ticket_number": 3, "review_context": {
            "body": "sibling evidence🙂" * 200, "nested": [{"count": 1}],
        }},
    }}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    copy_tree = sqlite_work_objects.copy_json_tree

    def reject_sibling_rewalk(value: Any) -> Any:
        # A prior-only sibling already has a proved encoding. Recursively
        # rebuilding its containers adds work without any new source proof.
        if isinstance(value, dict) and value.get("ticket_number") == 3:
            raise AssertionError("proved sibling was recursively copied")
        return copy_tree(value)

    monkeypatch.setattr(sqlite_work_objects, "copy_json_tree", reject_sibling_rewalk)
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current == original
        baseline = repository.current_work_baseline("run-1")
        current["ticket_jobs"]["3"]["review_context"]["nested"][0]["count"] = 9
        assert baseline == original
        assert repository.load_work("run-1", 2) == original
    assert repository.load("run-1") == original


def test_per_read_parsed_paths_do_not_reuse_node_values_or_hide_duplicates(tmp_path: Path) -> None:
    from agent_run.sqlite_work_objects import DecodeContext, _decode

    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    context = DecodeContext()
    with closing(sqlite3.connect(":memory:")) as connection:
        first, _ = _decode(repository, connection, "run-1", [
            ("[]", "map", "null"), ('["item"]', "scalar", encode_json("first")),
        ], context=context)
        second, _ = _decode(repository, connection, "run-1", [
            ("[]", "map", "null"), ('["item"]', "scalar", encode_json("second")),
        ], context=context)
        assert first == {"item": "first"} and second == {"item": "second"}
        assert first is not second
        with pytest.raises(ValueError, match="Duplicate SQLite Run object path"):
            _decode(repository, connection, "run-1", [
                ("[]", "map", "null"), ('["item"]', "map", "null"), ('[ "item" ]', "map", "null"),
            ], context=context)
        with pytest.raises(ValueError, match="list item"):
            _decode(repository, connection, "run-1", [
                ("[]", "map", "null"), ('["items"]', "list", "1"),
                ('["items",0]', "scalar", "0"), ('["items",1]', "scalar", "1"),
            ], context=context)
