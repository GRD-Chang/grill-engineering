"""Complete work fields require the exact current node and material closure."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run import sqlite_work_objects
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository


def _state() -> dict[str, Any]:
    return {"phase": "active", "creation_configuration": {
        "policy": {"attempts": 3, "roles": ["reviewer", "executor"]},
        "evidence": "中文🙂\x00evidence" * 200,
    }, "graph": {"a": {"children": [2, 3], "enabled": True}}}


def _observe(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    contexts: list[Any] = []
    decode = sqlite_work_objects._decode

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = decode(*args, **kwargs)
        if kwargs.get("context") is not None:
            contexts.append(kwargs["context"])
        return result

    monkeypatch.setattr(sqlite_work_objects, "_decode", observe)
    return contexts


def test_complete_work_fields_use_fresh_source_and_independent_cjson_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    contexts = _observe(monkeypatch)
    assert repository.load_work("run-1", 2) == original
    assert not contexts[-1].fields
    assert repository.load("run-1") == original
    with repository.atomic():
        loaded = repository.load_work("run-1", 2)
        assert loaded == original
        assert {"creation_configuration", "graph"} <= contexts[-1].fields.keys()
        baseline = repository.current_work_baseline("run-1")
        assert baseline == original
        loaded["creation_configuration"]["policy"]["roles"].append("caller")
        assert baseline == original
    assert repository.load_work("run-1", 2) == original


@pytest.mark.parametrize("change", ["add", "delete", "scalar", "kind", "header"])
def test_complete_field_reuse_observes_current_changed_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    contexts = _observe(monkeypatch)
    expected = deepcopy(original)
    if change == "add":
        expected["creation_configuration"]["new"] = {"actual": 1}
    elif change == "delete":
        del expected["creation_configuration"]["policy"]["attempts"]
    elif change == "scalar":
        expected["creation_configuration"]["policy"]["attempts"] = 4
    elif change == "kind":
        expected["creation_configuration"]["policy"]["attempts"] = {"nested": 4}
    else:
        expected["creation_configuration"]["evidence"] = "changed body" * 200
    SQLiteStateRepository(tmp_path, max_bytes=65536).save_objects("run-1", expected, fields={"creation_configuration"})
    assert repository.load_work("run-1", 2) == expected
    assert "creation_configuration" not in contexts[-1].fields
    assert "graph" in contexts[-1].fields


@pytest.mark.parametrize("damage", ["sha", "length", "run_link", "refs", "blob"])
def test_complete_work_field_reuse_never_bypasses_actual_material_checks(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "sha":
            connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence')")
        elif damage == "length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "run_link":
            connection.execute("DELETE FROM run_material_refs WHERE run_id='run-1'")
        elif damage == "refs":
            connection.execute("DELETE FROM run_object_material_refs WHERE run_id='run-1'")
        else:
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        connection.commit()
    with pytest.raises(ValueError, match="material|persistence limit"):
        repository.load_work("run-1", 2)


@pytest.mark.parametrize("change", ["alias", "duplicate", "override"])
def test_complete_work_field_reuse_preserves_node_alias_and_override_checks(tmp_path: Path, change: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    path = encode_json(("creation_configuration", "policy", "attempts"))
    with closing(sqlite3.connect(repository.path)) as connection:
        if change == "alias":
            connection.execute("UPDATE run_objects SET object_path=? WHERE object_path=?",
                               ('["creation_configuration", "policy", "attempts"]', path))
        elif change == "duplicate":
            connection.execute("INSERT INTO run_objects SELECT run_id,?,kind,value_json FROM run_objects WHERE object_path=?",
                               ('["creation_configuration", "policy", "attempts"]', path))
        else:
            connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)", (
                "run-1", encode_json(("creation_configuration", "policy", "attempts", "override")), "scalar", "1"))
        connection.commit()
    if change == "alias":
        assert repository.load_work("run-1", 2) == original
    else:
        with pytest.raises(ValueError, match="Duplicate|parent"):
            repository.load_work("run-1", 2)


def test_complete_work_field_large_view_has_no_reusable_field_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=2 * 1024 * 1024)
    original = _state()
    original["creation_configuration"]["evidence"] = "large" * 110000
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    assert repository._read_view is None
    contexts = _observe(monkeypatch)
    assert repository.load_work("run-1", 2) == original
    assert not contexts[-1].fields


def test_complete_fields_never_load_thin_timeline_continuation(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    original.update(timeline=[{"summary": "old"}],
                    timeline_continuation=[{"summary": str(i)} for i in range(4)])
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    with repository.atomic():
        current = repository.load_work("run-1", 2, thin_timeline=True)
        assert "timeline" not in current
        assert "timeline_continuation" not in current
        assert "timeline_continuation" not in repository.current_work_baseline("run-1")
        assert set(repository.work_timelines()) == {"timeline", "timeline_continuation"}


@pytest.mark.parametrize("change", ["same", "closure", "override"])
def test_complete_field_snapshot_closure_and_descendants_are_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    original["graph"]["a"]["review_context"] = {"body": "nested evidence" * 200, "number": 1e100}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    contexts = _observe(monkeypatch)
    expected = deepcopy(original)
    path = encode_json(("graph", "a", "review_context"))
    with closing(sqlite3.connect(repository.path)) as connection:
        if change == "closure":
            # Leave the snapshot's own identity but remove a nested dependency.
            identity = connection.execute("SELECT value_json FROM run_objects WHERE object_path=?", (path,)).fetchone()[0]
            connection.execute("DELETE FROM run_object_material_refs WHERE object_path=? AND material_id<>json_extract(?,'$')",
                               (path, identity))
        elif change == "override":
            expected["graph"]["a"]["review_context"]["number"] = 2
            connection.execute("INSERT INTO run_objects VALUES (?,?,?,?)", (
                "run-1", encode_json(("graph", "a", "review_context", "number")), "scalar", "2"))
            connection.execute("UPDATE run_sizes SET logical_bytes=?", (repository._check_size(expected),))
        connection.commit()
    if change == "closure":
        with pytest.raises(ValueError, match="material references"):
            repository.load_work("run-1", 2)
    else:
        with repository.atomic():
            current = repository.load_work("run-1", 2)
            assert current == expected
            assert ("graph" in contexts[-1].fields) == (change == "same")
            current["graph"]["a"]["review_context"]["body"] = "caller mutation"
            assert repository.current_work_baseline("run-1") == expected
