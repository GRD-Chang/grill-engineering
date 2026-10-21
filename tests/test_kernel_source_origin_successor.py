"""Task origin survives the Core's ordinary legal Run replacement."""
from pathlib import Path
import json

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Execution, Kernel, ParentIssue, Requirements, RunSpec
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.lifecycle_cli import creation_resources
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_cutover import migrate_completed_original_v6
from agent_run_kernel.offline import fixture_inputs
from agent_run_kernel.task_location import read_task, task_path
from test_kernel_migration_completed_history import _continue_final
from test_kernel_migration_original_cutover import _ManagedSourcePort
from test_kernel_migration_original_source import _managed_source


@pytest.mark.parametrize("entry", ["create_task", "admit_host"])
def test_legal_native_successor_keeps_the_fixed_original_task_origin(
    tmp_path: Path, git_repo: Path, entry: str,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths["database"], tmp_path / "sealed.sqlite3")
    task = TaskKey(managed.repository_root, "example/project", 1)
    kernel = migrate_completed_original_v6(snapshot, repository=task.repository, parent_number=1,
        nonce="7d" * 16, profile_snapshot=paths["profile"], github_snapshot=paths["github"],
        observation_snapshot=paths["observation"], original_report=paths["report"])
    assert Kernel.open_task(task).storage.path == kernel.storage.path
    port = _ManagedSourcePort.create(tmp_path / "remote")
    port.local_root = managed.root / "worktrees" / kernel.storage.path.parent.name
    port.git = LocalGit(managed.repository_root)
    _continue_final(kernel, port, paths)
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs")
        execution = Execution(run["id"], run["executor"], run["epoch"])
        proof = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_original_cutover'")
        original_proof = tx.get(proof["material_id"], proof["subject"])
    kernel.control("stop", "completed-native-exit")
    kernel.confirm_executor_exit(execution)
    assert Kernel.open_task(task) is None  # Existing _replaceable owns this decision.
    retired_source = paths["database"].read_bytes()
    base = port.git.commit("refs/heads/main")
    requirements = Requirements(ParentIssue(1, "New delivery", "Fresh Parent outcome"), ())
    run_id = "native-successor"
    spec = RunSpec(run_id, task.repository, 1, "unobserved", "unobserved",
        creation_resources(run_id, requirements, base, "refs/heads/main", managed.repository_root),
        language="en", repository_root=str(managed.repository_root), inputs=fixture_inputs())
    target = managed.state_root / run_id / "kernel.sqlite3"
    if entry == "create_task":
        successor = Kernel.create_task(target, spec, requirements=requirements)
    else:
        successor = Kernel.create(target, spec, requirements=requirements)
        request = successor.admit_host("new-native-host", kind="run", command=("native-host",),
            environment_carrier=tmp_path / "carrier", preflight=lambda: None)
        assert request.run_id == run_id
    assert read_task(task_path(task)) == (run_id, target)
    # A valid new binding must continue to pass the exact original-source gate.
    assert Kernel.open_task(task).storage.path == target
    with successor.storage.transaction(write=False) as tx:
        row = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_source_origin'")
        assert row["identity"] == "7d" * 16
        assert json.loads(tx.get(row["material_id"], row["subject"])) == {
            "origin_db": str(kernel.storage.path), "nonce": "7d" * 16}
        assert not tx.rows("SELECT identity FROM history WHERE kind='v6_original_cutover'")
    with kernel.storage.transaction(write=False) as tx:
        assert tx.get(proof["material_id"], proof["subject"]) == original_proof
    assert paths["database"].read_bytes() == retired_source

    # Retiring a sole old Task cannot block an unrelated Parent in this clone.
    other_requirements = Requirements(ParentIssue(2, "Other Parent", "Independent Task"), ())
    other_id = "independent-parent"
    other_spec = RunSpec(other_id, task.repository, 2, "unobserved", "unobserved",
        creation_resources(other_id, other_requirements, base, "refs/heads/main", managed.repository_root),
        language="en", repository_root=str(managed.repository_root), inputs=fixture_inputs())
    other = Kernel.create_task(managed.state_root / other_id / "kernel.sqlite3", other_spec, requirements=other_requirements)
    assert Kernel.open_task(TaskKey(managed.repository_root, task.repository, 2)).storage.path == other.storage.path
