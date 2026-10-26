"""Derived artifact locations for one explicitly selected Run, never authority.

The shared journal already hashes Run identity and keeps its existing root.
Only repository-layout per-Run artifacts/Host carrier partitions need a new
directory. No locator, mkdir, protocol probing, guessed directory name or state
is stored here. Public protocol selection remains the Storage caller's duty.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from agent_run.errors import KernelError
from agent_run.storage.run_scope import selected_statement, transaction_scope
from agent_run.storage.sqlite import Transaction


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    artifacts: Path
    journal_root: Path
    host_partition: str | None


def run_paths(owner: Transaction, database: Path, *, expected_run: str | None = None) -> RunPaths:
    selected = transaction_scope(owner)
    row = owner.one(*selected_statement(owner, "SELECT id FROM runs",
        "SELECT id FROM runs WHERE id=?", (), run_last=True))
    run_id = row["id"]
    if type(run_id) is not str or not run_id or expected_run is not None and run_id != expected_run:
        raise KernelError("run_path_binding")
    root = database.absolute().parent
    if selected is None:
        # Preserve the current per-Run artifact layout after reading its exact
        # single header; the directory name is never interpreted as an identity.
        return RunPaths(run_id, root, root, None)
    partition = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return RunPaths(run_id, root / "runs" / partition, root, partition)
