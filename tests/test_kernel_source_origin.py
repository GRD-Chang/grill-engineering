"""Prepared original-source proof belongs to the unpublished native creation."""
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.task_identity import TaskKey
from agent_run_kernel import Kernel, KernelError
from agent_run_kernel.model import wire_json
from agent_run_kernel.task_location import locked_task, read_task
from test_kernel_legacy_admission import native_spec, old_owner, unchanged_v6_source


@pytest.mark.parametrize("original_source", ["present", "absent"])
def test_unpublished_creation_retains_exact_origin_without_granting_admission(
    tmp_path: Path, unchanged_v6_source: Path, original_source: str,
) -> None:
    managed = ManagedWorkspace.for_repository("example/project")
    managed.repository_root.mkdir(parents=True)
    spec, requirements = native_spec(managed.repository_root)
    task = TaskKey(managed.repository_root, spec.repository, spec.parent_issue)
    target = managed.state_root / spec.run_id / "kernel.sqlite3"
    candidate = tmp_path / "unpublished.sqlite3"
    receipt = {
        "nonce": "1" * 32, "source_db": str(managed.state_root / "state.sqlite3"),
        "source_sha256": "a" * 64, "run_id": spec.run_id, "task_fingerprint": task.fingerprint,
        "source_uuid": "2" * 32, "target_db": str(target),
    }
    authority = (old_owner(unchanged_v6_source, managed.repository_root, managed.state_root, "unknown")
                 if original_source == "present" else nullcontext())
    with authority:
        kernel = Kernel.create(candidate, spec, requirements=requirements, source_origin=receipt)
        with kernel.storage.transaction(write=False) as tx:
            rows = tx.rows("SELECT * FROM history WHERE kind='v6_original_cutover'")
            assert len(rows) == 1 and rows[0]["identity"] == receipt["nonce"]
            body = tx.get(rows[0]["material_id"], rows[0]["subject"])
            assert body == wire_json(receipt) and json.loads(body) == receipt
            assert tx.one("SELECT purpose FROM material WHERE id=?", (rows[0]["material_id"],))[0] == "v6_original_cutover"
        # This is an immutable publication intent, including its final path;
        # an unrelated temporary inode has received no Task or Host authority.
        assert kernel.storage.path == candidate and candidate != target
        assert not target.exists()
        with locked_task(task) as location:
            assert read_task(location) is None
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            kernel.admit_host("cannot-start-prepared-origin", kind="run", command=("native-host",),
                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            kernel.claim_executor("cannot-start-unpublished-origin")
        assert kernel.active_host_action() is None
