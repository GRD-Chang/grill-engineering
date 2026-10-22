from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.paths import app_data_root
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_records import preserve_v6_records
from agent_run_kernel.query_cli import main
from test_kernel_original_public_query import _original_source, _bytes
from test_kernel_query_cli import _create
from test_kernel_query_discovery import _published


@pytest.mark.parametrize("source_kind", ["original", "archive"])
def test_derived_publication_rejects_foreign_parent_owner(
    tmp_path, git_repo, capsys, source_kind,
):
    source = _original_source(tmp_path, git_repo)
    database = Path(source["database"])
    selected, foreign_run = source["runs"]
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        version = db.execute(
            "SELECT attempt_id,publication_evidence_id "
            "FROM work_attempt_versions WHERE run_id=? "
            "AND publication_evidence_id IS NOT NULL LIMIT 1",
            (selected,),
        ).fetchone()
        assert version is not None
        child = dict(db.execute(
            "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?",
            (selected, version["publication_evidence_id"]),
        ).fetchone())
        raw = dict(db.execute(
            "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?",
            (selected, child["source_evidence_id"]),
        ).fetchone())
        foreign_owner = db.execute(
            "SELECT owner_id FROM work_evidence WHERE run_id=? "
            "AND kind='returned_result' LIMIT 1",
            (foreign_run,),
        ).fetchone()[0]
        assert foreign_owner != child["owner_id"]

        # 保持材料内容、digest、正确的子 owner 和直接 Invocation 引用。
        # 只让派生 Publication 的父 raw 声称属于另一真实 Attempt。
        raw["evidence_id"] = hashlib.sha256(
            b"independent-cross-parent-raw"
        ).hexdigest()
        raw["owner_id"] = foreign_owner
        db.execute(
            "INSERT INTO work_evidence (" + ",".join(raw) + ") VALUES ("
            + ",".join("?" for _ in raw) + ")",
            tuple(raw.values()),
        )
        db.execute(
            "UPDATE work_evidence SET source_evidence_id=? "
            "WHERE run_id=? AND evidence_id=?",
            (raw["evidence_id"], selected, child["evidence_id"]),
        )
        db.commit()

    if source_kind == "archive":
        snapshot = capture_v6(database, tmp_path / "snapshot.sqlite3")
        native = _create(tmp_path / "native", run_id=selected)
        preserve_v6_records(
            snapshot, native, run_id=selected, subject="ticket:2"
        )
        database = native.storage.path

    before = _bytes(tmp_path)
    code = main([
        "history", selected, "--state-dir", str(database),
        "--limit", "128", "--json",
    ])
    output = json.loads(capsys.readouterr().out)
    assert _bytes(tmp_path) == before
    record = output if source_kind == "original" else output.get("original", output)
    foreign_raw = [
        item["record"]["owner_id"]
        for item in record.get("raw_returns", {}).values()
        if item["record"]["owner_id"] == foreign_owner
    ]
    assert code == 2, {
        "actual_code": code,
        "foreign_raw_owners": foreign_raw,
        "child_owner": child["owner_id"],
    }
    assert output["diagnostics"][0]["code"] == "incompatible_run_state"
    assert output["diagnostics"][0]["message"] == "source_evidence_owner"


def test_empty_run_id_cannot_prove_bad_locator_unrelated(tmp_path, capsys):
    _published(
        ManagedWorkspace.for_repository("example/project").state_root,
        run_id="native-exact",
    )
    locator = app_data_root() / "run-locator.json"
    locator.write_text(json.dumps({"entries": [{
        "run_id": "",
        "repository_root": str(tmp_path / "old"),
        "state_dir": "invalid-relative-path",
        "updated_at": "2026-10-22T00:00:00Z",
        "repository": "example/project",
        "parent_number": 1,
    }]}))
    before = _bytes(tmp_path)
    code = main([
        "status", "native-exact", "--repo", "example/project", "--json",
    ])
    output = json.loads(capsys.readouterr().out)
    assert _bytes(tmp_path) == before
    assert code == 2, {
        "actual_code": code,
        "returned_run": output.get("run_id"),
    }
    assert output["diagnostics"][0]["code"] == "run_locator_invalid"
