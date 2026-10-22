"""WholeRun cleanup consumes exact imported ownership, never a sibling grant.

These synthetic import records test the Kernel consumer, not v6 conversion or
Task admission. The migration writer has its own original-source oracle.
This file calls the internal consumer transitions explicitly: it is not a runner,
physical deletion or end-to-end migration test, and does not bypass native source
admission by supplying fake ports.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Commit, Delivery, DeliveryKind, Execution, Kernel, KernelError, RunSpec, World
from agent_run_kernel.model import EffectResult
from agent_run_kernel.model import wire_json
from agent_run_kernel.offline import fixture_inputs


def imported_cleanup(tmp_path: Path, *, stage: int = 0, subject: str = "ticket:3", head_source: str = "work_jobs") -> tuple[Kernel, Execution, World]:
    repository = tmp_path / "repository"
    repository.mkdir()
    run_id = "whole-run"
    database = tmp_path / "state" / run_id / "kernel.sqlite3"
    root = database.parent.parent / "worktrees" / run_id
    base = Commit("a" * 40, "1" * 40)
    deliveries = tuple(Delivery(f"ticket:{issue}", DeliveryKind.TICKET, issue, f"revision-{issue}", base,
        f"refs/heads/original/ticket-{issue}", "refs/heads/original/run", checkout=str(root / f"ticket-{issue}"),
        owned_root=str(root), common_git_dir=str(repository / ".git")) for issue in (2, 3, 4))
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "parent", base,
        "refs/heads/original/run", "refs/heads/main", checkout=str(root / "final"),
        owned_root=str(root), common_git_dir=str(repository / ".git"))
    kernel = Kernel.create(database, RunSpec(run_id, "example/project", 1, "parent", "graph",
        (*deliveries, final), repository_root=str(repository), language="en", inputs=fixture_inputs()))
    # Claim before installing synthetic origin records: real imported admission
    # must additionally prove the source fence and Task pointer, outside this seam.
    execution = kernel.claim_executor("native")
    origin = {"nonce": "ab" * 16, "source_db": str(database.parent.parent / "state.sqlite3"),
        "source_sha256": "cd" * 32, "run_id": run_id, "source_uuid": "ef" * 16,
        "target_db": str(database), "task_fingerprint": TaskKey(repository, "example/project", 1).fingerprint}
    # Model the already retired source independently of mutable native materials.
    # The real forward tests supply the original v6 producer and Git ancestry.
    from agent_run_kernel.legacy_admission import source_fence

    head = "e" * 40 if head_source == "work_jobs" else base.sha
    source_tables = {
        "run_root": (["run_id", "value_run_branch", "value_checkout_identity", "value_graph_snapshot_id"],
            [[run_id, "original/run", origin["source_uuid"], "snapshot"]], ["run_id"]),
        "run_condition_base": (["run_id", "value_sha"], [[run_id, base.sha]], ["run_id"]),
        "work_jobs": (["run_id", "job_id", "stage", "value_integrated_sha", "owner_kind", "owner_key",
            "value_ticket_number", "value_ticket_branch_generation"],
            ([[run_id, "ticket:2:generation:1", "completed", head, "ticket", "2", 2, 1]] if head_source == "work_jobs" else [])
            + [[run_id, "ticket:3:generation:1", "reviewing", None, "ticket", "3", 3, 1]], ["run_id", "job_id"]),
    }
    with closing(sqlite3.connect(origin["source_db"])) as db, db:
        db.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO schema_meta VALUES ('protocol',?)", (source_fence(origin),))
        db.execute("CREATE TABLE runs (run_id TEXT,repository TEXT,parent_number INTEGER)")
        db.execute("INSERT INTO runs VALUES (?,?,1)", (run_id, "example/project"))
        for table, (names, rows, primary) in source_tables.items():
            db.execute("CREATE TABLE " + table + " (" + ",".join(names) + ",PRIMARY KEY (" + ",".join(primary) + "))")
            db.executemany("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in names) + ")", rows)
        db.execute("CREATE TABLE run_graph_snapshots (run_id,snapshot_id,version_id)")
        db.execute("INSERT INTO run_graph_snapshots VALUES (?,'snapshot','graph-version')", (run_id,))
        db.execute("CREATE TABLE run_graph_members (run_id,version_id,ticket_number)")
        db.executemany("INSERT INTO run_graph_members VALUES (?,'graph-version',?)", [(run_id, number) for number in (2, 3, 4)])
        db.execute("CREATE TABLE work_job_slots (run_id,container,slot,job_id)")
        db.executemany("INSERT INTO work_job_slots VALUES (?,'ticket_jobs',?,?)",
            [(run_id, str(row[6]), row[1]) for row in source_tables["work_jobs"][1]])
    with kernel.storage.transaction() as tx:
        external = wire_json({"source_kind": "original_run", "source_sha256": origin["source_sha256"],
            "remote_heads": {"refs/heads/original/run": head}})
        external_sha = hashlib.sha256(external).hexdigest()
        external_id = tx.put("ticket:2", "v6_external_readproof", external)
        tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,0)",
            ("ticket:2", "v6_external_readproof", external_sha, external_id))
        material = tx.put("ticket:2", "v6_original_cutover", wire_json(origin))
        tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,0)",
            ("ticket:2", "v6_original_cutover", origin["nonce"], material))
        entries = []
        for issue, kind in ((2, "completed" if head_source == "work_jobs" else "unallocated"), (3, "returned"), (4, "unallocated")):
            key = f"ticket:{issue}"
            record = ({"table": "run_graph_members", "key": [run_id, "graph-version", issue]}
                if kind == "unallocated" else {"table": "work_jobs", "key": [run_id, f"ticket:{issue}:generation:1"]})
            resources = []
            if issue == 3:
                layout = {"version": 1, "nonce": origin["nonce"], "source_sha256": origin["source_sha256"],
                    "source_uuid": origin["source_uuid"], "repository_root": str(repository),
                    "checkout": str(root / "ticket-3"), "owned_root": str(root),
                    "common_git_dir": str(repository / ".git"), "source_ref": "refs/heads/original/ticket-3",
                    "expected_oid": "b" * 40, "before_head": "ref: refs/heads/original/ticket-3\n",
                    "checkout_archive_sha256": "12" * 32}
                resources = [{"layout": layout, "checkout_index_sha256": "34" * 32, "source_record": record}]
            capsule = {"origin_nonce": origin["nonce"], "source_sha256": origin["source_sha256"],
                "subject": key, "generation": 1, "kind": kind, "source_record": record,
                "historical_base": base.sha if kind != "unallocated" else None, "resources": resources}
            proof_id = tx.put(key, "v6_subject_import", wire_json(capsule))
            tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,0)",
                (key, "v6_subject_import", origin["nonce"], proof_id))
            entries.append({"subject": key, "generation": 1, "proof_id": proof_id})
        proof = {"origin_nonce": origin["nonce"], "source_sha256": origin["source_sha256"],
            "resource_mode": "existing-verified", "external_readproof_sha256": external_sha,
            "run_record": {"table": "run_root", "key": [run_id]},
            "run_ref_ownership": {"source_ref": "refs/heads/original/run", "expected_oid": head,
                "source_record": {"table": "run_root", "key": [run_id]},
                "head_evidence": [{"table": "work_jobs", "key": [run_id, "ticket:2:generation:1"]}] if head_source == "work_jobs"
                    else [{"table": "run_condition_base", "key": [run_id]}]},
            "subjects": entries}
        material = tx.put("ticket:2", "v6_run_import", wire_json(proof))
        tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,0)",
            ("ticket:2", "v6_run_import", origin["nonce"], material))
        tables = {}
        for table, (names, rows, primary) in source_tables.items():
            records = [{"key": [row[names.index(name)] for name in primary], "size": 256,
                "cells": [{"type": "text" if isinstance(value, str) else "integer" if isinstance(value, int) else "null",
                    "value": value} for value in row]} for row in rows]
            page = tx.put("ticket:2", "v6_record_page", wire_json(records))
            tables[table] = {"columns": [{"name": name} for name in names], "primary_key": primary,
                "count": len(rows), "pages": [{"start": 0, "count": len(rows),
                    "first": records[0]["key"], "last": records[-1]["key"], "material": page}]}
        archive = tx.put("ticket:2", "v6_record_manifest", wire_json({"format": "v6-records/1",
            "source": {"run_id": run_id, "sha256": origin["source_sha256"], "protocol": "sqlite-work-records-v6",
                "schema_sha256": "56" * 32}, "tables": tables, "documents": {}}))
        tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,0)",
            ("ticket:2", "v6_record_import", run_id, archive))
        tx.execute("UPDATE subjects SET phase='complete',source_sha=?,workspace_sha=? WHERE id=?",
            ("d" * 40, "c" * 40, subject))
        closed = tx.put(subject, "imported_close", wire_json({"disposition": "target"}))
        tx.execute("INSERT INTO effects(id,subject,operation,status,parent_revision,graph_revision,revision,generation,"
            "source_ref,target_ref,before_sha,base_sha,base_tree,payload_id,receipt_id) "
            "SELECT ?,id,'close','confirmed','parent','graph',revision,generation,source_ref,target_ref,"
            "source_sha,base_sha,base_tree,?,? FROM subjects WHERE id=?", ("source-close", closed, closed, subject))
        if subject == "final:1":
            intent = tx.put(subject, "effect_intent", wire_json({"create_ref": False,
                "checkout": final.checkout, "owned_root": final.owned_root, "common_git_dir": final.common_git_dir}))
            receipt = tx.put(subject, "effect_receipt", wire_json({"disposition": "target",
                "commit": {"sha": base.sha, "tree": base.tree}}))
            tx.execute("INSERT INTO effects(id,subject,operation,status,parent_revision,graph_revision,revision,generation,"
                "source_ref,target_ref,before_sha,base_sha,base_tree,payload_id,receipt_id) "
                "SELECT 'native-final-workspace',id,'workspace','confirmed','parent','graph',revision,generation,source_ref,target_ref,"
                "base_sha,base_sha,base_tree,?,? FROM subjects WHERE id=?", (intent, receipt, subject))
        tx.execute("INSERT INTO resource_cleanup(subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir,stage) "
            "SELECT id,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir,? FROM subjects WHERE id=?",
            (stage, subject))
    world = World("parent", "graph", "revision-3", base)
    return kernel, execution, world


@pytest.mark.parametrize(("stage", "operation", "head"), [(0, "cleanup_worktree", "c" * 40), (1, "cleanup_local_ref", "b" * 40)])
def test_returned_ticket_cleanup_uses_its_proof_when_another_ticket_owns_the_run_manifest(
    tmp_path: Path, stage: int, operation: str, head: str,
) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=stage)
    step = kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert step.kind == "effect" and step.effect is not None
    assert step.effect.operation == operation
    assert step.effect.before_sha == step.effect.payload["expected_head"] == head
    assert step.effect.payload["source_uuid"] == "ef" * 16


def test_final_cleanup_uses_the_original_run_root_ref_not_the_later_published_head(tmp_path: Path) -> None:
    kernel, execution, world = imported_cleanup(tmp_path, stage=1, subject="final:1")
    step = kernel._advance_cleanup("final:1", execution, world, token=kernel._begin_automatic_step(execution, "final:1", cleanup=True))
    assert step.kind == "effect" and step.effect is not None
    assert step.effect.operation == "cleanup_local_ref"
    assert step.effect.source_ref == "refs/heads/original/run"
    assert step.effect.before_sha == step.effect.payload["expected_head"] == "e" * 40
    assert step.effect.payload["source_uuid"] == "ef" * 16


@pytest.mark.parametrize("corruption", ["sibling", "generation", "uuid", "checkout", "missing_subject",
    "duplicate_ref", "missing_resource", "source_record", "unallocated_resource", "run_ref_collision"])
def test_whole_run_cleanup_refuses_unproven_or_cross_subject_ownership(tmp_path: Path, corruption: str) -> None:
    kernel, execution, world = imported_cleanup(tmp_path)
    with kernel.storage.transaction() as tx:
        manifest = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
        body = json.loads(tx.get(manifest["material_id"], manifest["subject"]))
        selected = body["subjects"][1]
        capsule = json.loads(tx.get(selected["proof_id"], "ticket:3"))
        if corruption == "sibling":
            selected["proof_id"] = body["subjects"][0]["proof_id"]
        elif corruption == "generation":
            selected["generation"] = 2
        elif corruption == "missing_subject":
            body["subjects"].pop()
        elif corruption == "run_ref_collision":
            body["run_ref_ownership"]["source_ref"] = "refs/heads/original/ticket-3"
        elif corruption in {"duplicate_ref", "unallocated_resource"}:
            other = body["subjects"][0 if corruption == "duplicate_ref" else 2]
            old_id = other["proof_id"]
            other_body = json.loads(tx.get(old_id, other["subject"]))
            other_body["resources"] = capsule["resources"]
            if corruption == "duplicate_ref":
                other_body["source_record"] = capsule["source_record"]
            other["proof_id"] = tx.put(other["subject"], "v6_subject_import", wire_json(other_body))
            tx.execute("UPDATE history SET material_id=? WHERE kind='v6_subject_import' AND material_id=?",
                (other["proof_id"], old_id))
        else:
            old_id = selected["proof_id"]
            if corruption == "missing_resource":
                capsule["resources"] = []
            elif corruption == "source_record":
                capsule["source_record"]["key"][1] = "ticket:2:generation:1"
            elif corruption == "uuid":
                capsule["resources"][0]["layout"]["source_uuid"] = "ff" * 16
            elif corruption == "checkout":
                capsule["resources"][0]["layout"]["checkout"] = str(tmp_path / "foreign-checkout")
            selected["proof_id"] = tx.put("ticket:3", "v6_subject_import", wire_json(capsule))
            tx.execute("UPDATE history SET material_id=? WHERE kind='v6_subject_import' AND material_id=?",
                (selected["proof_id"], old_id))
        updated = tx.put(manifest["subject"], "v6_run_import", wire_json(body))
        tx.execute("UPDATE history SET material_id=? WHERE kind='v6_run_import'", (updated,))
    step = kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)
    assert kernel.status("ticket:3")["resource_cleanup"]["effect_id"] is None


def test_imported_cleanup_unknown_result_remains_the_same_intent_across_reopen(tmp_path: Path) -> None:
    kernel, execution, world = imported_cleanup(tmp_path)
    step = kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert step.effect is not None and kernel._dispatch(step.effect, execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    kernel = Kernel(kernel.storage.path)
    assert kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True)).effect == step.effect
    kernel._reconcile(step.effect, EffectResult("unknown"), execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    pending = kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert pending.kind == "reconcile" and pending.effect == step.effect
    assert kernel.status("ticket:3")["resource_cleanup"]["stage"] == 0
    kernel._reconcile(step.effect, EffectResult("target"), execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True)).kind == "progress"
    assert kernel.status("ticket:3")["resource_cleanup"]["stage"] == 1


def test_stop_after_preparation_fences_imported_cleanup_before_dispatch(tmp_path: Path) -> None:
    kernel, execution, world = imported_cleanup(tmp_path)
    step = kernel._advance_cleanup("ticket:3", execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert step.effect is not None
    kernel.control("stop", "stop-before-imported-delete")
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel._dispatch(step.effect, execution, world, token=kernel._begin_automatic_step(execution, "ticket:3", cleanup=True))
    assert kernel.status("ticket:3")["resource_cleanup"]["stage"] == 0
