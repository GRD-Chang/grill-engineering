"""Latest-only storage refusal, using actual producers rather than edited proofs."""
from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from agent_run_kernel.state_preflight import PROTOCOL, UnsupportedState, require_supported_state
from agent_run_kernel.storage import Storage
from test_kernel_legacy_admission import old_owner, unchanged_v6_source


def _durable_bytes(path: Path) -> dict[str, bytes]:
    # SQLite SHM contains transient reader marks, not a state/history record.
    # Keep the main database and the entire committed WAL in this assertion.
    return {item.name: item.read_bytes() for item in path.parent.iterdir()
            if item.is_file() and not item.name.endswith('-shm')}


def test_latest_only_refusal_preserves_normal_producer_state_and_wal(
    tmp_path: Path, unchanged_v6_source: Path,
) -> None:
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

    # Execute the exact old native producer, without creating a compatibility
    # runtime or rewriting an old protocol/material to construct a fixture.
    source = tmp_path / 'previous-source'
    archive = subprocess.run(['git', 'archive', '807e0ada56294bf9efc4adbabe940da53a881ce6', 'project/src'],
        cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, timeout=20).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        for member in files:
            if member.isfile():
                target = source / member.name
                assert target.resolve().is_relative_to(source.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = files.extractfile(member)
                assert stream is not None
                target.write_bytes(stream.read())
    previous = tmp_path / 'previous' / 'kernel.sqlite3'
    producer = subprocess.run([sys.executable, '-c', '''
from pathlib import Path
import sys
from agent_run_kernel import Commit,Delivery,DeliveryKind,Kernel,Policy,RunSpec
spec=RunSpec('old-native','example/project',1,'parent','graph',(
    Delivery('parent:1',DeliveryKind.PARENT_ONLY,1,'revision',Commit('base','tree'),
             'refs/heads/parent','refs/heads/main'),),Policy(),language='en')
Kernel.create(Path(sys.argv[1]),spec)
''', str(previous)], env={**os.environ, 'PYTHONPATH': str(source / 'project/src'),
    'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=20)
    assert producer.returncode == 0, producer.stdout + producer.stderr
    with closing(sqlite3.connect(previous.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.execute('SELECT protocol FROM meta').fetchone()
        before = _durable_bytes(previous)
        for operation in (require_supported_state, Storage):
            with pytest.raises(UnsupportedState) as refused:
                operation(previous)
            assert refused.value.code == 'unsupported_state'
            assert refused.value.protocol == 'independent-delivery-kernel-v18-exploration'
            assert refused.value.path == previous
        assert _durable_bytes(previous) == before

    workspace = tmp_path / 'legacy-checkout'
    workspace.mkdir()
    state = tmp_path / 'legacy-state'
    with old_owner(unchanged_v6_source, workspace, state, 'live'):
        database = state / 'state.sqlite3'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.execute('SELECT value FROM schema_meta').fetchall()
            before = _durable_bytes(database)
            with pytest.raises(UnsupportedState) as refused:
                require_supported_state(database)
            assert refused.value.reason == 'legacy_v6'
            assert refused.value.protocol == 'sqlite-work-records-v6'
            assert refused.value.diagnostic()['path'] == str(database)
            assert _durable_bytes(database) == before


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
