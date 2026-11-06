"""Pure transaction seam for Run-owned Core domain tests only.

No file admission, Task publication, public entry, capacity or
physical Host proof. The original Transaction owns accounting and rollback.
"""
from contextlib import ExitStack, closing, contextmanager
import sqlite3
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction


class ScopedKernel(Kernel):
    """Only transaction injection; every business operation is the real Kernel."""

    def __init__(self, db, run, *, path=Path("/fixture/kernel.sqlite3"), record_owners=True, enforce_readonly=False):
        self.owners = []

        @contextmanager
        def transaction(*, write=True, ordered=True, cancelled=None, deadline=None):
            assert cancelled is None and deadline is None and not db.in_transaction
            if enforce_readonly:
                # Reassigning this flag expires every prepared statement, even
                # when unchanged. Read the private connection's actual mode.
                if db.execute("PRAGMA query_only").fetchone()[0] != int(not write):
                    db.execute("PRAGMA query_only=" + ("OFF" if write else "ON"))
            owner = Transaction(db)
            if record_owners:
                self.owners.append(owner)
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield RunTransaction(owner, run)
                if owner.failed:
                    raise KernelError("transaction_aborted")
                db.commit()
            except BaseException:
                db.rollback()
                raise

        self.storage = SimpleNamespace(transaction=transaction, path=path, run_id=run)
        self.clock = lambda: 100.
        self._observation = None
        self._observation_lock = RLock()


@contextmanager
def effect_runs(monkeypatch):
    """Shared existing Effect-domain fixture, not native/public activation proof."""
    from dataclasses import asdict
    from agent_run.core import kernel as core
    from agent_run.model import Policy, wire_json
    from support.offline import fixture_inputs
    from support.repository_scope import connection, insert, seed
    SUBJECT = "parent:1"
    from agent_run.model import Commit
    BASE = Commit("a" * 40, "b" * 40)
    with closing(connection()) as db:
        db.execute("BEGIN")
        owner = Transaction(db)
        inputs = fixture_inputs()
        for run in ("A", "B"):
            seed(db, run)
            tx = RunTransaction(owner, run)
            policy = Policy(development_deadline_seconds=7., review_deadline_seconds=9.)
            db.execute("UPDATE runs SET policy=?,executor='same-owner',epoch=1 WHERE id=?",
                (wire_json(core.Kernel._policy_values(policy)), run))
            profile = tx.put(SUBJECT, "profile", wire_json(inputs.profiles))
            db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (profile, run))
            values = asdict(inputs)
            del values["profiles"]
            material = tx.put(SUBJECT, "creation_inputs", wire_json(values))
            insert(db, "run_inputs", {"run_id": run, "subject": SUBJECT, "material_id": material})
            insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": "requirements",
                "window": 1, "development_limit": 3, "review_limit": 3, "review_required": 1})
            db.execute("UPDATE subjects SET phase='review',candidate_sha=?,candidate_tree=?,workspace_sha=?,checkout=? WHERE run_id=?",
                (BASE.sha, BASE.tree, BASE.sha, "/fixture/" + run, run))
        db.commit()
        monkeypatch.setattr(core, "identity", lambda: "same-effect")
        yield db, {run: ScopedKernel(db, run) for run in ("A", "B")}


@contextmanager
def domain_kernel(native):
    """Use original SQL facts below the file-admission/save-queue protocol.

    Single-threaded business/Git consumers only. The caller must retain native
    Storage for reopening, concurrent owners, file faults, capacity, storage
    admission/contention deadlines and public entry. Creation is always the real RepositoryRoute producer.
    """
    path, run = native.storage.path, native.storage.run_id
    assert run is not None
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        selected = ScopedKernel(db, run, path=path, record_owners=False, enforce_readonly=True)
        selected.clock = native.clock
        yield selected


@pytest.fixture
def domain_scenario():
    """Explicit consumer selection; default Scenario remains fully native.

    Final's historical Ticket initialization runs natively inside Scenario;
    only subsequent business/Git work uses this transaction attachment.
    """
    from support.offline import Scenario

    with ExitStack() as resources:
        def create(*args, **kwargs):
            scenario = Scenario(*args, **kwargs)
            scenario.kernel = resources.enter_context(domain_kernel(scenario.kernel))
            return scenario
        yield create
