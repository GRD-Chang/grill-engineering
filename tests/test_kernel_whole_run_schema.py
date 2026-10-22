"""Inspect trusted Core schema on synthetic databases, without altered DDL."""
from pathlib import Path
import json
import sqlite3

import pytest

from agent_run_kernel import Kernel
from agent_run_kernel.migration_run_projection import _Projection
from agent_run_kernel.model import DeliveryKind
from agent_run_kernel.offline import OfflinePort
from agent_run_kernel.storage import PROTOCOL
from test_kernel_continuation_upgrade import native_v17_source, _produce


@pytest.mark.parametrize('creation', ['fresh', 'upgraded'])
def test_prepared_schema_accepts_core_creation_and_physical_vacuum(
    tmp_path: Path, native_v17_source: Path, creation: str,
) -> None:
    if creation == 'upgraded':
        path = tmp_path / 'upgraded.sqlite3'
        original = _produce(native_v17_source, path, 'returned')
        native = Kernel(path)  # Only the Core's official upgrade touches v17.
        with native.storage.transaction(write=False) as tx:
            attempt = tx.one('SELECT * FROM attempts WHERE id=?', (original['attempt'],))
            assert attempt['thread_id'] == original['thread']
            assert json.loads(tx.get(attempt['opening_id'], 'ticket:2')) == {
                'provenance_id': None, 'ordinary_used': 0, 'capacity_count': 0, 'prior': None}
        before = path.read_bytes()
        assert Kernel(path).storage.path.read_bytes() == before
    else:
        port = OfflinePort.create(tmp_path / 'native')
        spec, _ = port.prepare(DeliveryKind.TICKET)
        native = Kernel.create(port.root / 'kernel.sqlite3', spec)
    contract = _Projection()
    with native.storage.transaction(write=False) as tx:
        contract.verify_schema(tx)
        assert tx.one('SELECT protocol FROM meta')[0] == PROTOCOL == 'independent-delivery-kernel-v18-exploration'
        # table_xinfo exposes the hidden-column classification explicitly.
        assert len(contract.schema) == 38
        assert sum(len(columns) for columns in contract.schema.values()) == 356
        assert all(column['hidden'] == 0 for columns in contract.schema.values() for column in columns)
        references = {row['from']: (row['table'], row['to'])
                      for row in tx.execute('PRAGMA foreign_key_list(attempts)')}
        assert references['thread_id'] == ('threads', 'id')
        assert references['opening_id'] == ('material', 'id')
        assert tx.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    # Physical page allocation is outside the schema contract. This is normal
    # SQLite maintenance on a test-owned database, with no DDL alteration.
    with sqlite3.connect(native.storage.path) as db:
        db.execute('VACUUM')
    with native.storage.transaction(write=False) as tx:
        contract.verify_schema(tx)
        assert tx.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
