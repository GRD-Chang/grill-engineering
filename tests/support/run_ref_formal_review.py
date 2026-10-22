"""Formal admission followed by a corrupted grant with the existing Executor."""
import json
from pathlib import Path

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Kernel, KernelError
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import wire_json
from test_kernel_migration_original_whole_run import (
    test_original_whole_run_formal_cutover_continues_c_and_final_against_original as original_forward,
)


class ProofChecked(Exception):
    pass


def test_formal_executor_cannot_turn_changed_run_ref_material_into_a_cleanup_grant(tmp_path, git_repo, monkeypatch):
    advance = Kernel.advance_cleanup

    def inspect(kernel, subject, execution, world=None):
        if not subject.startswith("final:"):
            return advance(kernel, subject, execution, world)
        with kernel.storage.transaction(write=False) as tx:
            cleanup = tx.one("SELECT * FROM resource_cleanup WHERE subject=?", (subject,))
            run = tx.one("SELECT * FROM runs")
        if cleanup["stage"] != 1 or cleanup["effect_id"] is not None:
            return advance(kernel, subject, execution, world)
        task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
        admitted = Kernel.open_task(task)
        assert admitted is not None and admitted.storage.path == kernel.storage.path
        git = LocalGit(task.workspace)
        prior = git.commit(cleanup["source_ref"])
        with kernel.storage.transaction() as tx:
            row = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
            manifest = json.loads(tx.get(row["material_id"], row["subject"]))
            manifest["run_ref_ownership"]["head_evidence"] = [None]
            manifest["run_ref_ownership"]["expected_oid"] = "0" * 40
            material = tx.put(row["subject"], "v6_run_import", wire_json(manifest))
            tx.execute("UPDATE history SET material_id=? WHERE kind='v6_run_import'", (material,))
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            Kernel.open_task(task)
        step = advance(kernel, subject, execution, world)
        assert (step.kind, step.reason, step.effect) == ("paused", "cleanup_marker_unproven", None)
        assert git.commit(cleanup["source_ref"]) == prior
        assert kernel.status(subject)["resource_cleanup"]["effect_id"] is None
        raise ProofChecked

    monkeypatch.setattr(Kernel, "advance_cleanup", inspect)
    with pytest.raises(ProofChecked):
        original_forward(tmp_path, git_repo, monkeypatch)
