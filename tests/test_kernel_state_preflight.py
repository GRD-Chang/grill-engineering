"""Current storage preflight preserves committed data and rejects missing state."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.storage.preflight import PROTOCOL, UnsupportedState, require_supported_state
from agent_run.storage.sqlite import Storage


def _durable_bytes(path: Path) -> dict[str, bytes]:
    # SQLite SHM contains transient reader marks, not a state/history record.
    # Keep the main database and the entire committed WAL in this assertion.
    return {item.name: item.read_bytes() for item in path.parent.iterdir()
            if item.is_file() and not item.name.endswith('-shm')}


def test_latest_producer_state_and_wal_are_not_modified_by_preflight(tmp_path: Path) -> None:
    native = Storage.create(tmp_path / 'latest' / 'kernel.sqlite3')
    with native.transaction(write=False):
        # Normal SQLite maintenance while a reader holds its earlier snapshot
        # leaves the latest producer's actual schema/data in a committed WAL.
        # No old protocol, proof or material is fabricated or altered.
        with closing(sqlite3.connect(native.path)) as maintenance:
            maintenance.execute('VACUUM')
        assert Path(str(native.path) + '-wal').stat().st_size > 0
        before = _durable_bytes(native.path)
        assert require_supported_state(native.path) == PROTOCOL
        assert _durable_bytes(native.path) == before


def test_missing_state_and_orphaned_sidecars_are_not_new_storage(tmp_path: Path) -> None:
    missing = tmp_path / 'not-created' / 'kernel.sqlite3'
    with pytest.raises(UnsupportedState, match='unreadable_state'):
        require_supported_state(missing)
    assert not missing.parent.exists()
    # No fabricated WAL content: this is an empty, abandoned location marker.
    orphan = tmp_path / 'kernel.sqlite3-shm'
    orphan.touch()
    with pytest.raises(UnsupportedState, match='uncertain_state'):
        Storage.create(tmp_path / 'kernel.sqlite3')
    assert orphan.read_bytes() == b''
    assert not (tmp_path / 'kernel.sqlite3').exists()
