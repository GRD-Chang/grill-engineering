"""领域材料保留原文、准确拥有者、读取边界及独立查询视图。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_work_evidence import MAX_EVIDENCE_BYTES


READERS = ("full", "work", "objects", "fields", "paths")


def _read(repository: SQLiteStateRepository, reader: str) -> dict[str, Any]:
    if reader == "full":
        return repository.load("run-1")
    if reader == "work":
        return repository.load_work("run-1", 2)
    if reader == "objects":
        return repository.load_objects("run-1")[0]
    if reader == "fields":
        return repository.load_fields("run-1", {"parent_job", "ticket_jobs"})
    return repository.load_paths("run-1", {("parent_job",), ("ticket_jobs", "2"), ("ticket_jobs", "3")})


@pytest.mark.parametrize("reader", READERS)
def test_owned_materials_preserve_exact_text_types_and_independent_views(tmp_path: Path, reader: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    context = {"body": '中文🙂\x00\\\n["snapshot","literal"]' * 50,
               "report": '{ "value": 1e+0099, "enabled": true }',
               "values": ["", None, False, 0, 1, 1.0, -0.0, 1e99, {}, []],
               "checks": [{"name": "CI", "status": "passed"}]}
    original = {"parent_job": {"review_context": context}, "ticket_jobs": {
        str(number): {"ticket_number": number, "review_context": deepcopy(context)} for number in (2, 3)}}
    repository.save("run-1", original)
    loaded = _read(repository, reader)
    assert json.dumps(loaded, sort_keys=True) == json.dumps(original, sort_keys=True)
    loaded["ticket_jobs"]["2"]["review_context"]["checks"][0]["status"] = "caller mutation"
    assert loaded["ticket_jobs"]["3"]["review_context"] == context
    assert loaded["parent_job"]["review_context"] == context
    assert _read(repository, reader) == original
    assert SQLiteStateRepository(tmp_path).load("run-1") == original


@pytest.mark.parametrize("damage", ["same_length_body", "body_blob", "length", "missing", "owner", "reference"])
def test_current_materials_are_revalidated_after_successful_reads(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"parent_job": {"review_context": {"body": "original exact evidence"}}}
    repository.save("run-1", original)
    assert repository.load("run-1") == original
    assert repository.load_work("run-1", 2) == original
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        if damage == "same_length_body":
            connection.execute("UPDATE work_evidence_materials SET body=replace(body,'original','Original')")
        elif damage == "body_blob":
            connection.execute("UPDATE work_evidence_materials SET body=CAST(body AS BLOB)")
        elif damage == "length":
            connection.execute("UPDATE work_evidence_materials SET byte_length=byte_length+1")
        elif damage == "missing":
            connection.execute("DELETE FROM work_evidence_materials")
        elif damage == "owner":
            connection.execute("UPDATE work_evidence SET owner_id='foreign owner'")
        else:
            connection.execute("UPDATE work_jobs SET value_review_context='missing evidence'")
        before = tuple(connection.iterdump())
    for operation in (lambda: repository.load("run-1"), lambda: repository.load_work("run-1", 2)):
        with pytest.raises(ValueError, match="evidence|material"):
            operation()
    with closing(sqlite3.connect(repository.path)) as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("foreign", ["run", "job"])
def test_material_reference_cannot_borrow_another_owner(tmp_path: Path, foreign: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    state = {"ticket_jobs": {str(number): {"ticket_number": number,
             "review_context": {"body": f"exact evidence for {number}"}} for number in (2, 3)}}
    repository.save("run-1", state)
    repository.save("run-2", {"parent_job": {"review_context": {"body": "foreign run evidence"}}})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        source_run, source_owner = ("run-2", "parent") if foreign == "run" else ("run-1", "ticket")
        source_key = "" if foreign == "run" else "3"
        foreign_id = connection.execute(
            "SELECT value_review_context FROM work_jobs WHERE run_id=? AND owner_kind=? "
            "AND (?='' OR owner_key=?)", (source_run, source_owner, source_key, source_key),
        ).fetchone()[0]
        changed = connection.execute(
            "UPDATE work_jobs SET value_review_context=? WHERE run_id='run-1' AND owner_key='2'", (foreign_id,),
        )
        assert changed.rowcount == 1
    with pytest.raises(ValueError, match="evidence"):
        repository.load_paths("run-1", {("ticket_jobs", "2")})
    assert repository.load("run-2") == {"parent_job": {"review_context": {"body": "foreign run evidence"}}}


def test_oversized_material_is_rejected_before_fetching_its_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"parent_job": {"review_context": {"body": "kept evidence"}}})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute("UPDATE work_evidence_materials SET body=CAST(zeroblob(?) AS TEXT),byte_length=?",
                           (MAX_EVIDENCE_BYTES + 1, MAX_EVIDENCE_BYTES + 1))
        connection.execute("UPDATE work_evidence SET value_bytes=?", (MAX_EVIDENCE_BYTES + 1,))
    connect = sqlite3.connect
    def guarded_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        def text(raw):
            assert len(raw) <= MAX_EVIDENCE_BYTES, "oversized material body was fetched before its bound"
            return raw.decode("utf-8")
        connection.text_factory = text
        return connection
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    with pytest.raises(ValueError, match="size|identity|byte length|limit"):
        repository.load("run-1")


def test_more_than_one_material_batch_keeps_all_selected_jobs(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    jobs = {str(number): {"ticket_number": number, "review_context": {
        "body": f'原文🙂\x00 quoted "ticket {number}"', "values": [True, None, number],
    }} for number in range(1, 131)}
    repository.save("run-1", {"ticket_jobs": jobs})
    loaded = repository.load_paths("run-1", {("ticket_jobs", key) for key in jobs})
    assert loaded == {"ticket_jobs": jobs}
    loaded["ticket_jobs"]["1"]["review_context"]["values"].append("caller mutation")
    assert repository.load("run-1") == {"ticket_jobs": jobs}
