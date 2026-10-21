"""A source marker grants nothing without its original physical and native proof."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Kernel, KernelError, ParentIssue, Requirements, RunSpec
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.legacy_admission import read_source_fence, source_fence
from agent_run_kernel.lifecycle_cli import creation_resources
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_cutover import migrate_completed_original_v6
from agent_run_kernel.offline import fixture_inputs
from agent_run_kernel.task_location import bind_task, task_path
from test_kernel_migration_original_source import _managed_source


def test_original_source_admission_requires_the_exact_origin_proof_and_clone(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths["database"], tmp_path / "sealed.sqlite3")
    task = TaskKey(managed.repository_root, "example/project", 1)
    kernel = migrate_completed_original_v6(snapshot, repository=task.repository, parent_number=1,
        nonce="8e" * 16, profile_snapshot=paths["profile"], github_snapshot=paths["github"],
        observation_snapshot=paths["observation"], original_report=paths["report"])
    assert Kernel.open_task(task).storage.path == kernel.storage.path
    with closing(sqlite3.connect(paths["database"])) as db:
        protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    receipt = read_source_fence(protocol)
    for field, wrong in (("nonce", "9f" * 16), ("source_sha256", "af" * 32), ("source_uuid", "bf" * 16)):
        with closing(sqlite3.connect(paths["database"])) as db:
            db.execute("UPDATE schema_meta SET value=? WHERE key='protocol'", (source_fence({**receipt, field: wrong}),))
            db.commit()
        try:
            with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
                Kernel.open_task(task)
        finally:
            with closing(sqlite3.connect(paths["database"])) as db:
                db.execute("UPDATE schema_meta SET value=? WHERE key='protocol'", (protocol,))
                db.commit()
    assert Kernel.open_task(task).storage.path == kernel.storage.path
    # The initial target is an immutable authority anchor, even after later Run replacement.
    moved = kernel.storage.path.with_name("original-proof-unavailable.sqlite3")
    kernel.storage.path.rename(moved)
    try:
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            Kernel.open_task(task)
    finally:
        moved.rename(kernel.storage.path)
    marker = managed.repository_root / ".git" / "agent-run-checkout-id"
    original_uuid = marker.read_bytes()
    marker.write_bytes(b"cf" * 16 + b"\n")
    try:
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            Kernel.open_task(task)
    finally:
        marker.write_bytes(original_uuid)
    source = Requirements(ParentIssue(1, "Unbound candidate", "Cannot assume old authority"), ())
    base = LocalGit(managed.repository_root).commit("refs/heads/main")
    spec = RunSpec("foreign-candidate", task.repository, 1, "unobserved", "unobserved",
        creation_resources("foreign-candidate", source, base, "refs/heads/main", managed.repository_root),
        language="en", repository_root=str(managed.repository_root), inputs=fixture_inputs())
    unbound = Kernel.create(managed.state_root / spec.run_id / "kernel.sqlite3", spec, requirements=source)
    bind_task(task_path(task), spec.run_id, unbound.storage.path)
    try:
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            Kernel.open_task(task)
    finally:
        bind_task(task_path(task), receipt["run_id"], kernel.storage.path)
    assert Kernel.open_task(task).storage.path == kernel.storage.path
    with kernel.storage.transaction(write=False) as tx:
        for table in ("actions", "hosts", "invocations", "leases"):
            assert tx.one(f"SELECT count(*) FROM {table}")[0] == 0
        proof = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_original_cutover'")
        assert json.loads(tx.get(proof["material_id"], proof["subject"])) == receipt
