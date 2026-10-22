"""Inspect trusted Core schema on synthetic databases, without altered DDL."""
from pathlib import Path
import sqlite3

from agent_run_kernel import Kernel
from agent_run_kernel.migration_run_projection import _Projection
from agent_run_kernel.model import DeliveryKind
from agent_run_kernel.offline import OfflinePort


def test_prepared_schema_accepts_core_creation_and_physical_vacuum(tmp_path: Path) -> None:
    port = OfflinePort.create(tmp_path / 'native')
    spec, _ = port.prepare(DeliveryKind.TICKET)
    native = Kernel.create(port.root / 'kernel.sqlite3', spec)
    contract = _Projection()
    with native.storage.transaction(write=False) as tx:
        contract.verify_schema(tx)
        # table_xinfo exposes the hidden-column classification explicitly.
        assert len(contract.schema) == 38
        assert sum(len(columns) for columns in contract.schema.values()) == 354
        assert all(column['hidden'] == 0 for columns in contract.schema.values() for column in columns)
        assert tx.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    # Physical page allocation is outside the schema contract. This is normal
    # SQLite maintenance on a test-owned database, with no DDL alteration.
    with sqlite3.connect(native.storage.path) as db:
        db.execute('VACUUM')
    with native.storage.transaction(write=False) as tx:
        contract.verify_schema(tx)
        assert tx.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
