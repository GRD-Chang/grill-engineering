"""Transaction seam for staged Run-owned Core domain tests only.

No file admission, Task publication, public protocol activation, capacity or
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
