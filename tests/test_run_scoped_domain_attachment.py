"""The explicit domain attachment keeps original SQL ownership and rollback."""
import sqlite3

import pytest

from agent_run.errors import KernelError
from support.worker_facts import worker_fixture
from support.run_scoped_kernel import domain_kernel


@pytest.mark.parametrize("write", [False, True])
def test_domain_attachment_rolls_back_a_caught_sql_failure_and_closes_its_connection(tmp_path, write):
    native, _ = worker_fixture(tmp_path)
    with native.storage.transaction(write=False) as tx:
        original = tx.one("SELECT executor FROM runs")[0]
    with domain_kernel(native) as kernel:
        assert kernel.storage.path == native.storage.path and kernel.storage.run_id == native.storage.run_id
        with pytest.raises(KernelError, match="transaction_aborted"):
            with kernel.storage.transaction(write=write) as tx:
                with pytest.raises(sqlite3.OperationalError):
                    tx.execute("UPDATE runs SET executor='rolled-back'")
                    tx.execute("SELECT * FROM absent_table")
        assert kernel.owners == []
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT executor FROM runs")[0] == original
        assert kernel.control("stop", "original-domain-control") == "applied"
    # The same native owner sees committed facts, not a copied business store.
    assert native.source_status()["stopped"] is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        with kernel.storage.transaction(write=False):
            pass
