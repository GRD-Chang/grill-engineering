"""Offline-copy seam; real v6 fixture, never a user database or new expected Run."""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
import select
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_run_kernel.migration import MigrationRefused, capture_v6, restore_v6, validate_snapshot
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import Commit, EffectResult, Policy, Profile, RunSpec, World
from agent_run_kernel.offline import OfflinePort, Scenario
from support.frozen_v6_runtime import old_v6_runtime


@pytest.fixture(scope="session")
def original_v6(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return old_v6_runtime(tmp_path_factory.mktemp("unchanged-v6-runtime"))


def test_capture_keeps_real_controller_database_and_original_runtime_usable(
    git_repo: Path, tmp_path: Path, original_v6: Any,
) -> None:
    old_root = git_repo / ".agent-run"
    fixture = Path(original_v6("fixture", root=old_root,
                               fixture_path=git_repo / "github.json", issues={}))
    started = original_v6("start", root=old_root, git=git_repo, fixture=fixture)
    state = started["state"]
    source = old_root / "state.sqlite3"
    before = source.read_bytes()
    original = started["loaded"]
    destination = tmp_path / "captured-v6.sqlite3"

    snapshot = capture_v6(source, destination)

    assert source.read_bytes() == before == destination.read_bytes()
    assert snapshot.sha256 == hashlib.sha256(before).hexdigest()
    assert snapshot.source_protocol == "sqlite-work-records-v6"
    assert dict(snapshot.table_rows)["runs"] == 1
    assert validate_snapshot(snapshot) == snapshot
    copied = destination.parent / "reopened"
    copied.mkdir()
    restored = restore_v6(snapshot, copied / "state.sqlite3")
    assert restored.sha256 == snapshot.sha256
    assert validate_snapshot(restored) == restored
    assert original_v6("load", root=copied, run_id=state["run_id"]) == original
    # Continue the real old Controller using the untouched original authority.
    continued = original_v6("start", root=old_root, git=git_repo, fixture=fixture)
    resumed = continued["state"]
    assert continued["reused"] is True
    assert resumed["run_id"] == state["run_id"]
    assert continued["loaded"] is not None


def test_selected_idle_v6_ticket_converts_and_new_kernel_can_continue(
    git_repo: Path, tmp_path: Path, original_v6: Any,
) -> None:
    issue = {"number": 2, "title": "Ticket 2", "body": "Deliver the selected ticket.",
             "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": []}
    old_root = git_repo / ".agent-run"
    fixture = Path(original_v6("fixture", root=old_root,
                               fixture_path=git_repo / "github.json", issues={"2": issue}))
    started = original_v6("start", root=old_root, git=git_repo, fixture=fixture)
    state, original_old = started["state"], started["loaded"]
    source = old_root / "state.sqlite3"
    original = source.read_bytes()
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    work_root = old_root / "worktrees"
    work_root.mkdir(exist_ok=True)
    checkout = work_root / "converted-ticket-2"
    base_sha = state["base"]["sha"]
    subprocess.run(["git", "worktree", "add", "--detach", str(checkout), base_sha],
                   cwd=git_repo, check=True, capture_output=True, text=True)
    candidate = tmp_path / "converted.sqlite3"
    kernel = convert_initial_v6(snapshot, candidate, repository_root=git_repo,
                                source_ref=f"refs/heads/agent-run/{state['run_id']}/converted-ticket-2",
                                checkout=checkout, final_checkout=work_root / "converted-final",
                                development_profile=Profile("fixture-development", "high"),
                                review_profile=Profile("fixture-review", "high"),
                                publication_profile=Profile("fixture-publication", "high"))

    assert source.read_bytes() == original
    assert snapshot.path.read_bytes() == original
    current = kernel.status("ticket:2")
    assert current["phase"] == "workspace"
    assert current["pending_attempt"] is None
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT created_at,language,repository_root,policy FROM runs")
        assert run["created_at"] == pytest.approx(datetime.fromisoformat(state["created_at"]).timestamp())
        assert run["language"] == state["language"]
        assert run["repository_root"] == str(git_repo.resolve())
        assert [row["kind"] for row in tx.rows("SELECT kind FROM history ORDER BY position")] == [
            "v6_run_status", "v6_ticket_phase"]
        assert tx.one("SELECT count(*) FROM material WHERE purpose='v6_evidence'")[0] >= 1
    old_copy = tmp_path / "old-restored"
    old_copy.mkdir()
    restore_v6(snapshot, old_copy / "state.sqlite3")
    assert original_v6("load", root=old_copy, run_id=state["run_id"]) == original_old
    execution = kernel.claim_executor("migration-test-owner")
    tree = subprocess.run(["git", "rev-parse", f"{base_sha}^{{tree}}"], cwd=git_repo,
                          check=True, capture_output=True, text=True).stdout.strip()
    world = World(current["parent_revision"], current["graph_revision"], current["revision"],
                  Commit(base_sha, tree), completion_revision=current["completion_revision"])
    workspace = kernel.advance("ticket:2", execution, world)
    assert workspace.kind == "effect" and workspace.effect is not None
    assert workspace.effect.operation == "workspace"
    assert kernel.dispatch(workspace.effect, execution, world)
    LocalGit(git_repo).run("update-ref", workspace.effect.source_ref, base_sha, "0" * len(base_sha))
    kernel.reconcile(workspace.effect, EffectResult("target", commit=Commit(base_sha, tree)), execution, world)
    assert kernel.advance("ticket:2", execution, world).kind == "progress"
    step = kernel.advance("ticket:2", execution, world)
    assert step.kind == "agent"
    assert step.call is not None and step.call.subject == "ticket:2"
    assert kernel.status("ticket:2")["pending_attempt"] == step.call.attempt_id


def test_capture_never_replaces_an_existing_destination(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    before = source.read_bytes()
    destination = tmp_path / "occupied"
    destination.write_bytes(b"existing unrelated bytes")
    with pytest.raises(MigrationRefused, match="destination_exists"):
        capture_v6(source, destination)
    assert destination.read_bytes() == b"existing unrelated bytes"
    assert source.read_bytes() == before


def test_capture_refuses_uncheckpointed_wal_without_touching_source(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    # A committed transaction whose process exits without close leaves a real
    # nonempty WAL, but no live owner. This reaches sidecar admission itself.
    writer = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
assert db.execute('PRAGMA journal_mode').fetchone() == ('wal',)
db.execute("UPDATE runs SET status='execution_failed' WHERE run_id='source'")
db.commit()
os._exit(0)
""", str(source)], capture_output=True, text=True, timeout=10)
    assert writer.returncode == 0, writer.stdout + writer.stderr
    wal = Path(str(source) + "-wal")
    assert wal.stat().st_size > 0
    files = [path for path in old_root.iterdir() if path.is_file()]
    original = {path.name: path.read_bytes() for path in files}
    with pytest.raises(MigrationRefused, match="source_sidecar"):
        capture_v6(source, tmp_path / "target.sqlite3")
    assert {path.name: path.read_bytes() for path in files} == original
    assert not (tmp_path / "target.sqlite3").exists()


def test_snapshot_validation_rejects_changed_material_without_repairing_it(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"parent": {"body": "原始正文"}})
    source = old_root / "state.sqlite3"
    original = source.read_bytes()
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    with snapshot.path.open("r+b") as stream:
        stream.seek(100)
        stream.write(b"damaged")
    damaged = snapshot.path.read_bytes()
    with pytest.raises(MigrationRefused, match="snapshot_changed"):
        validate_snapshot(snapshot)
    with pytest.raises(MigrationRefused, match=r"^snapshot_(changed|corrupt)$"):
        restore_v6(snapshot, tmp_path / "restored.sqlite3")
    assert not (tmp_path / "restored.sqlite3").exists()
    assert source.read_bytes() == original
    assert snapshot.path.read_bytes() == damaged


def test_capture_preserves_raw_and_consumed_prior_budget_before_old_runtime_resume(
    git_repo: Path, tmp_path: Path, original_v6: Any,
) -> None:
    old_root = git_repo / ".agent-run"
    original = original_v6("returned_review", root=old_root, git=git_repo)
    run_id = original["run_id"]
    assert original is not None
    pending = original["run_acceptance"]["pending_semantic_attempt"]
    assert pending["returned_result"]["thread_id"] == "captured-review"
    ticket = original["ticket_jobs"]["2"]
    assert ticket["review_budget"]["development_attempts"] == 1
    assert ticket["review_budget"]["reviewer_invocations"] == 1

    snapshot = capture_v6(old_root / "state.sqlite3", tmp_path / "raw-copy.sqlite3")
    copy_root = tmp_path / "copy-runtime"
    copy_root.mkdir()
    (copy_root / "state.sqlite3").write_bytes(snapshot.path.read_bytes())
    assert original_v6("load", root=copy_root, run_id=run_id) == original

    accepted = original_v6("consume_review", root=old_root, git=git_repo, run_id=run_id)
    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["review_budget"]["reviewer_invocations"] == 1
    assert accepted["ticket_jobs"]["2"]["review_budget"] == ticket["review_budget"]
    validate_snapshot(snapshot)


def test_capture_refuses_a_real_other_process_sqlite_owner(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    before = source.read_bytes()
    code = """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('BEGIN IMMEDIATE')
print('locked', flush=True)
sys.stdin.readline()
db.rollback()
db.close()
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(source)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], "SQLite owner did not reach its lock"
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(MigrationRefused, match="source_not_offline"):
            capture_v6(source, tmp_path / "copy.sqlite3")
        assert source.read_bytes() == before
        assert not (tmp_path / "copy.sqlite3").exists()
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    assert process.returncode == 0


def test_refusing_a_local_rollback_writer_does_not_release_its_sqlite_lock(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    # This is a real v6 DB. DELETE mode makes the owner's RESERVED lock live
    # on the main file, so closing a second descriptor exposes the regression.
    connection = sqlite3.connect(source, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        before = source.read_bytes()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationRefused, match="source_not_offline"):
            capture_v6(source, tmp_path / "refused.sqlite3")
        # Do not read_bytes/open/close source while this owner holds its lock:
        # that would itself reproduce the POSIX-lock bug under test.
        probe = subprocess.run([sys.executable, "-c", """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error), error
else:
    raise AssertionError('refusal released the existing main-file SQLite lock')
finally:
    db.close()
""", str(source)], capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert not (tmp_path / "refused.sqlite3").exists()
    finally:
        connection.rollback()
        connection.close()
    assert source.read_bytes() == before


def test_restore_refusal_keeps_the_captured_database_owner_lock(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    db = sqlite3.connect(source)
    try:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    finally:
        db.close()
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    owner = sqlite3.connect(snapshot.path, isolation_level=None)
    try:
        owner.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationRefused, match="source_not_offline"):
            restore_v6(snapshot, tmp_path / "restored.sqlite3")
        probe = subprocess.run([sys.executable, "-c", """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error), error
else:
    raise AssertionError('restore refusal released the captured database owner lock')
finally:
    db.close()
""", str(snapshot.path)], capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert not (tmp_path / "restored.sqlite3").exists()
    finally:
        owner.rollback()
        owner.close()


def test_capture_refuses_a_source_with_another_journal_pathname(tmp_path: Path, original_v6: Any) -> None:
    old_root = tmp_path / "old"
    original_v6("save", root=old_root, run_id="source", state={"status": "created"})
    source = old_root / "state.sqlite3"
    alias = tmp_path / "other-name.sqlite3"
    os.link(source, alias)
    before = source.read_bytes()
    with pytest.raises(MigrationRefused, match="source_alias"):
        capture_v6(source, tmp_path / "snapshot.sqlite3")
    assert source.read_bytes() == alias.read_bytes() == before
    assert not (tmp_path / "snapshot.sqlite3").exists()
