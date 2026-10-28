"""Pure transaction seam for Run-owned Core domain tests only.

No file admission, Task publication, public entry, capacity or
physical Host proof. The original Transaction owns accounting and rollback.
"""
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction


class ScopedKernel(Kernel):
    """Only transaction injection; every business operation is the real Kernel."""

    def __init__(self, db, run):
        self.owners = []

        @contextmanager
        def transaction(*, write=True, ordered=True, cancelled=None):
            assert cancelled is None and not db.in_transaction
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            owner = Transaction(db)
            self.owners.append(owner)
            try:
                yield RunTransaction(owner, run)
                if owner.failed:
                    raise KernelError("transaction_aborted")
                db.commit()
            except BaseException:
                db.rollback()
                raise

        self.storage = SimpleNamespace(transaction=transaction, path=Path("/fixture/kernel.sqlite3"))
        self.clock = lambda: 100.
        self._observation = None
        self._observation_lock = RLock()


@contextmanager
def effect_runs(monkeypatch):
    """Shared existing Effect-domain fixture, not native/public activation proof."""
    from contextlib import closing
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
