"""Consumer regressions from the independent Run-ref ownership review."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run_kernel.model import wire_json
from test_kernel_whole_resource_proof import imported_cleanup


@pytest.mark.parametrize("head_evidence", [
    [{"table": "work_jobs", "key": ["foreign-run", "ticket:999:generation:7"]}],
    [None],
], ids=["foreign-run-generation", "non-record"])
def test_final_cleanup_rejects_unproven_run_ref(tmp_path: Path, head_evidence: object) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1")
    with kernel.storage.transaction() as tx:
        row = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
        manifest = json.loads(tx.get(row["material_id"], row["subject"]))
        manifest["run_ref_ownership"]["head_evidence"] = head_evidence
        updated = tx.put(row["subject"], "v6_run_import", wire_json(manifest))
        tx.execute("UPDATE history SET material_id=? WHERE kind='v6_run_import'", (updated,))
    step = kernel.advance_cleanup("final:1", execution, world)
    assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)


@pytest.mark.parametrize("changed", ["oid", "generation", "returned_owner", "archive_source", "run_ref"])
def test_final_ref_is_bound_to_the_original_archive_not_only_native_material(tmp_path: Path, changed: str) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1")
    with kernel.storage.transaction() as tx:
        row = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
        manifest = json.loads(tx.get(row["material_id"], row["subject"]))
        if changed == "oid":
            manifest["run_ref_ownership"]["expected_oid"] = "f" * 40
        elif changed == "generation":
            manifest["run_ref_ownership"]["head_evidence"][0]["key"][1] = "ticket:2:generation:2"
        elif changed == "returned_owner":
            manifest["run_ref_ownership"]["head_evidence"][0]["key"][1] = "ticket:3:generation:1"
        elif changed == "archive_source":
            archive = tx.one("SELECT * FROM history WHERE kind='v6_record_import'")
            body = json.loads(tx.get(archive["material_id"], archive["subject"]))
            body["source"]["sha256"] = "00" * 32
            material = tx.put(archive["subject"], "v6_record_manifest", wire_json(body))
            tx.execute("UPDATE history SET material_id=? WHERE kind='v6_record_import'", (material,))
        else:
            manifest["run_ref_ownership"]["source_ref"] = "refs/heads/foreign"
            tx.execute("UPDATE subjects SET source_ref='refs/heads/foreign' WHERE id='final:1'")
            tx.execute("UPDATE resource_cleanup SET source_ref='refs/heads/foreign' WHERE subject='final:1'")
            tx.execute("UPDATE effects SET source_ref='refs/heads/foreign' WHERE subject='final:1'")
        material = tx.put(row["subject"], "v6_run_import", wire_json(manifest))
        tx.execute("UPDATE history SET material_id=? WHERE kind='v6_run_import'", (material,))
    step = kernel.advance_cleanup("final:1", execution, world)
    assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)
    assert kernel.status("final:1")["resource_cleanup"]["effect_id"] is None


@pytest.mark.parametrize("source", ["work_jobs", "run_condition_base"])
def test_exact_archived_head_can_dispatch_without_inventing_a_new_authority(tmp_path: Path, source: str) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1", head_source=source)
    head = "e" * 40 if source == "work_jobs" else "a" * 40
    step = kernel.advance_cleanup("final:1", execution, world)
    assert step.effect is not None and step.effect.before_sha == head
    assert kernel.dispatch(step.effect, execution, world)


def test_historical_base_does_not_override_an_original_completed_head(tmp_path: Path) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1")
    with kernel.storage.transaction() as tx:
        row = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
        manifest = json.loads(tx.get(row["material_id"], row["subject"]))
        manifest["run_ref_ownership"]["head_evidence"] = [
            {"table": "run_condition_base", "key": [execution.run_id]}]
        manifest["run_ref_ownership"]["expected_oid"] = "a" * 40
        material = tx.put(row["subject"], "v6_run_import", wire_json(manifest))
        tx.execute("UPDATE history SET material_id=? WHERE kind='v6_run_import'", (material,))
    step = kernel.advance_cleanup("final:1", execution, world)
    assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)
    assert kernel.status("final:1")["resource_cleanup"]["effect_id"] is None


@pytest.mark.parametrize("change", ["missing", "unfenced", "foreign_nonce"])
def test_native_archive_claims_do_not_replace_the_retired_source_fence(tmp_path: Path, change: str) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1")
    source = kernel.storage.path.parent.parent / "state.sqlite3"
    if change == "missing":
        source.unlink()
    else:
        from agent_run_kernel.legacy_admission import read_source_fence, source_fence

        with closing(sqlite3.connect(source)) as db, db:
            receipt = read_source_fence(db.execute("SELECT value FROM schema_meta").fetchone()[0])
            receipt["nonce"] = "00" * 16
            protocol = "sqlite-work-records-v6" if change == "unfenced" else source_fence(receipt)
            db.execute("UPDATE schema_meta SET value=?", (protocol,))
    step = kernel.advance_cleanup("final:1", execution, world)
    assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)
    assert kernel.status("final:1")["resource_cleanup"]["effect_id"] is None


def test_archive_reader_accepts_storage_without_a_kernel_handle_or_writes(tmp_path: Path) -> None:
    from agent_run_kernel.migration_records import V6RecordReader
    from agent_run_kernel.storage import Storage

    kernel, execution, _ = imported_cleanup(tmp_path)
    before = kernel.storage.path.read_bytes()
    reader = V6RecordReader(Storage(kernel.storage.path))
    assert reader.row("run_root", [execution.run_id]) == V6RecordReader(kernel).row("run_root", [execution.run_id])
    assert kernel.storage.path.read_bytes() == before
