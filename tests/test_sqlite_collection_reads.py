"""Collection batches read only selected source trees and current materials."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_collections import load_paths, read_path
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository


def _repository(tmp_path: Path) -> SQLiteStateRepository:
    repository = SQLiteStateRepository(tmp_path, max_bytes=2 * 1024 * 1024)
    body = {"phase": "active", "review_context": {"body": "证据🙂\x00 evidence" * 200}}
    repository.save("run-1", {"ticket_jobs": {"2": deepcopy(body), "3": deepcopy(body),
        "sibling": {"body": "unselected evidence" * 200}}, "variables": {
            "scalar": "value", "list": [{"nested": [1, 2]}, 3], "empty": {},
        }})
    return repository


def test_batched_collection_children_are_independent_and_skip_damaged_siblings(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE run_materials SET value='broken' WHERE value=?", ("unselected evidence" * 200,))
        connection.commit()
    selected = {("ticket_jobs", "2"), ("ticket_jobs", "3"), ("ticket_jobs", "missing")}
    first = repository.load_paths("run-1", selected)
    assert set(first["ticket_jobs"]) == {"2", "3"}
    first["ticket_jobs"]["2"]["review_context"]["body"] = "caller mutation"
    assert "caller mutation" != first["ticket_jobs"]["3"]["review_context"]["body"]
    assert "caller mutation" != repository.load_paths("run-1", selected)["ticket_jobs"]["2"]["review_context"]["body"]


def test_collection_batch_accepts_scalar_list_map_and_missing_roots(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    selected = {("variables", key) for key in ("scalar", "list", "empty", "absent")}
    assert repository.load_paths("run-1", selected) == {"variables": {
        "scalar": "value", "list": [{"nested": [1, 2]}, 3], "empty": {},
    }}
    assert repository.load_paths("run-1", {("absent", "child")}) == {}
    assert repository.load_paths("run-1", set()) == {}


@pytest.mark.parametrize("paths", [set(), {("variables",)}, {("variables", "list", "unavailable")},
                                  {("variables",), ("absent", "child")}, {("variables", "empty"), ("variables", "scalar")}])
def test_collection_general_paths_keep_original_fallback(tmp_path: Path, paths: set[tuple[str, ...]]) -> None:
    repository = _repository(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        expected: dict[str, Any] = {}
        for path in paths:
            try:
                value = read_path(connection, "run-1", path)
            except KeyError:
                continue
            target = expected
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = value
        assert load_paths(connection, "run-1", paths) == expected


@pytest.mark.parametrize("paths", [{()}, {("variables", 0)}, {("variables", "list", 0)}])
def test_collection_paths_still_require_mapping_ancestors(tmp_path: Path, paths: set[tuple[Any, ...]]) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ValueError, match="mapping ancestors"):
        repository.load_paths("run-1", paths)


@pytest.mark.parametrize("shape", ["missing_root", "missing_list_item", "duplicate_alias", "alias"])
def test_collection_batch_retains_actual_shape_and_alias_validation(tmp_path: Path, shape: str) -> None:
    repository = _repository(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        if shape == "missing_root":
            connection.execute("DELETE FROM run_objects WHERE object_path=?", (encode_json(("variables", "list")),))
        elif shape == "missing_list_item":
            connection.execute("DELETE FROM run_objects WHERE object_path=?", (encode_json(("variables", "list", 1)),))
        elif shape == "duplicate_alias":
            connection.execute("INSERT INTO run_objects SELECT run_id,?,kind,value_json FROM run_objects WHERE object_path=?",
                               ('["variables","list", 1]', encode_json(("variables", "list", 1))))
        else:
            connection.execute("UPDATE run_objects SET object_path=? WHERE object_path=?",
                               ('["variables","list", 1]', encode_json(("variables", "list", 1))))
        connection.commit()
        if shape == "alias":
            assert load_paths(connection, "run-1", {("variables", "list")}) == {"variables": {"list": [{"nested": [1, 2]}, 3]}}
        else:
            with pytest.raises(ValueError, match="Missing|Duplicate"):
                load_paths(connection, "run-1", {("variables", "list"), ("variables", "empty")})


@pytest.mark.parametrize("damage", ["sha", "length", "run_link", "missing", "blob"])
@pytest.mark.parametrize("reader", ["load", "prepare"])
def test_collection_reads_preserve_current_material_checks(tmp_path: Path, damage: str, reader: str) -> None:
    repository = _repository(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "sha":
            connection.execute("UPDATE run_materials SET value=replace(value,'evidence','Evidence')")
        elif damage == "length":
            connection.execute("UPDATE run_materials SET byte_length=byte_length+1")
        elif damage == "run_link":
            connection.execute("DELETE FROM run_material_refs WHERE run_id='run-1'")
        elif damage == "missing":
            connection.execute("DELETE FROM run_materials")
        else:
            connection.execute("UPDATE run_materials SET value=CAST(value AS BLOB)")
        connection.commit()
    with pytest.raises(ValueError, match="material|dependency"):
        if reader == "load":
            repository.load_paths("run-1", {("ticket_jobs", "2"), ("ticket_jobs", "3")})
        else:
            repository.save_objects("run-1", {"ticket_jobs": {"2": {"phase": "changed"}, "3": {"phase": "changed"}}},
                                    fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2", "3"}})


def test_collection_batch_handles_utf8_quoted_keys_and_more_than_eighty_paths(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=2 * 1024 * 1024)
    children = {f'中文🙂\x00"{i}': {"value": i} for i in range(161)}
    repository.save("run-1", {"variables": children})
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 250)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        assert load_paths(connection, "run-1", {("variables", key) for key in children}) == {"variables": children}
        assert sum("WITH requested(path,start,stop)" in sql for sql in statements) == 3


def test_collection_prepare_reads_existing_selected_child_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with repository.atomic():
        statements: list[str] = []
        repository._local.connection.set_trace_callback(statements.append)
        repository.save_objects("run-1", {"ticket_jobs": {"2": {"phase": "changed"}}}, fields={"ticket_jobs"},
                                keyed_changes={"ticket_jobs": {"2"}})
        # Only the exact selected-child subtree reader is counted. Source
        # budget/digest validation keeps its independent real SQL checks.
        child_reads = [sql for sql in statements if "SELECT object_path, kind, value_json FROM run_objects WHERE run_id=" in sql
                       and "[\"ticket_jobs\",\"2\"]" in sql]
        assert len(child_reads) == 1
    assert repository.load_paths("run-1", {("ticket_jobs", "2")}) == {"ticket_jobs": {"2": {"phase": "changed"}}}
