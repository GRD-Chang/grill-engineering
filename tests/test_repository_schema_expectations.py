"""Cache only immutable expected DDL; every real database stays untrusted."""
from contextlib import closing
import sqlite3

import pytest

from agent_run.storage import repository_schema as schema
from agent_run.storage.preflight import UnsupportedState, require_supported_state
from agent_run.storage.sqlite import Storage


def test_expected_schema_parse_is_reused_without_sharing_a_mutable_result(monkeypatch):
    # Distinct complete input gives this test a cold key without depending on
    # private cache APIs or other tests' order.
    monkeypatch.setattr(schema, 'REPOSITORY_ROUTING_SCHEMA', schema.REPOSITORY_ROUTING_SCHEMA + '\n')
    complete = sqlite3.complete_statement
    parsed = []
    def record(statement):
        parsed.append(True)
        return complete(statement)
    monkeypatch.setattr(sqlite3, 'complete_statement', record)
    first = schema.repository_schema_objects()
    expected = dict(first)
    initial = len(parsed)
    assert initial > 0
    first.pop(('table', 'runs'))
    first[('table', 'meta')] = 'foreign SQL'
    first[('view', 'foreign')] = 'untrusted'
    second = schema.repository_schema_objects()
    assert second == expected and second is not first
    assert len(parsed) == initial


@pytest.mark.parametrize('name', ['REPOSITORY_RUN_SCHEMA', 'REPOSITORY_TASK_SCHEMA', 'REPOSITORY_ROUTING_SCHEMA'])
def test_each_complete_schema_input_is_part_of_the_cache_identity(monkeypatch, name):
    original = getattr(schema, name)
    before = schema.repository_schema_objects()
    with monkeypatch.context() as changed:
        changed.setattr(schema, name, original + '\nCREATE TABLE explicit_new_contract(value TEXT);\n')
        observed = schema.repository_schema_objects()
        assert observed == before | {('table', 'explicit_new_contract'): 'CREATE TABLE explicit_new_contract(value TEXT)'}
    assert schema.repository_schema_objects() == before


@pytest.mark.parametrize('suffix,message', [('SELECT 1;', 'Unrecognized schema declaration'),
    ('/* incomplete schema declaration', 'Incomplete schema declaration')])
@pytest.mark.parametrize('name', ['REPOSITORY_RUN_SCHEMA', 'REPOSITORY_TASK_SCHEMA', 'REPOSITORY_ROUTING_SCHEMA'])
def test_changed_invalid_schema_is_not_hidden_by_warm_expected_entries(monkeypatch, name, suffix, message):
    before = schema.repository_schema_objects()
    with monkeypatch.context() as changed:
        changed.setattr(schema, name, getattr(schema, name) + '\n' + suffix)
        with pytest.raises(RuntimeError, match=message):
            schema.repository_schema_objects()
    assert schema.repository_schema_objects() == before


@pytest.mark.parametrize('fault,reason', [
    ('extra-view', 'uncertain_state'), ('missing-index', 'uncertain_state'),
    ('protocol', 'unknown_protocol'), ('source-history', 'source_origin'),
    ('import-history', 'imported_state'), ('source-material', 'source_origin'),
    ('import-material', 'imported_state'),
])
@pytest.mark.parametrize('same_database', [True, False])
def test_warm_expected_ddl_never_caches_actual_database_admission(tmp_path, fault, reason, same_database):
    good = Storage.create(tmp_path / 'good.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    assert require_supported_state(good.path) == schema.PROTOCOL
    damaged = good if same_database else Storage.create(tmp_path / 'other.sqlite3',
        namespace=('example/other', str(tmp_path.resolve())))
    # Deliberate invalid-state inputs at the physical SQL boundary. No Run,
    # successful work or authority is fabricated for a positive scenario.
    with closing(sqlite3.connect(damaged.path)) as db:
        if fault == 'extra-view':
            db.execute('CREATE VIEW foreign_contract AS SELECT 1')
        elif fault == 'missing-index':
            db.execute('DROP INDEX history_run_page')
        elif fault == 'protocol':
            db.execute("UPDATE meta SET protocol='unrecognized-protocol'")
        elif fault.endswith('history'):
            marker = 'v6_source_origin' if fault.startswith('source-') else 'v6_imported'
            db.execute('INSERT INTO history(run_id,subject,kind,identity,generation,created_at) VALUES (?,?,?,?,?,?)',
                ('invalid-run', 'invalid-subject', marker, 'negative-fixture', 1, 1.0))
        else:
            marker = 'v6_source_origin' if fault.startswith('source-') else 'v6_imported'
            db.execute('INSERT INTO material(run_id,id,subject,purpose,digest,size,body) VALUES (?,?,?,?,?,?,?)',
                ('invalid-run', 'negative-fixture', 'invalid-subject', marker, 'invalid-digest', 1, b'x'))
        db.commit()
    before = damaged.path.read_bytes()
    with pytest.raises(UnsupportedState) as rejected:
        require_supported_state(damaged.path)
    assert rejected.value.reason == reason and rejected.value.path == damaged.path
    assert damaged.path.read_bytes() == before
    if not same_database:
        assert require_supported_state(good.path) == schema.PROTOCOL
