"""Original v6 query facts, without importing a writable legacy Run or engine."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Sequence

from .migration import SOURCE_PROTOCOL, source_identity
from .migration_records import V6RecordReader
from .storage import QUERY_LIMIT
from .model import wire_json


def _name(value: str) -> str:
    if re.fullmatch(r'[a-z][a-z_0-9]*', value) is None:
        raise ValueError('Invalid original record column')
    return '"' + value + '"'


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    # Refuse an already open inode before opening/closing another descriptor
    # could release this process's POSIX locks. A different live Host may write;
    # mode=ro plus a read transaction observes its WAL consistently.
    source_identity(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        yield db


def protocol(path: Path) -> bool:
    with connection(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone():
            return False
        value = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()
        if value is None:
            raise ValueError('Original protocol is missing')
        if value[0] != SOURCE_PROTOCOL:
            from .legacy_admission import read_source_fence
            read_source_fence(value[0])
        return True


class OriginalRecords(V6RecordReader):
    """Use the same evidence/history reader on one read-only original snapshot.

    Only row transport varies. Evidence digests, derived returned members and
    original history/Attempt relations are decoded by the existing reader.
    """

    def __init__(self, db: sqlite3.Connection, path: Path, run_id: str) -> None:
        self.db = db
        self.used = 0
        stored_protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
        self._manifest = {'source': {'run_id': run_id, 'protocol': stored_protocol,
                                     'path': str(path.resolve())}, 'tables': {}}

    def _table(self, table: str) -> tuple[list[str], list[str], str, tuple[str, ...]]:
        columns = list(self.db.execute('PRAGMA table_info(' + _name(table) + ')'))
        if not columns or len(columns) > 256:
            raise ValueError('Missing original record table: ' + table)
        names = [row['name'] for row in columns]
        keys = [row['name'] for row in sorted(columns, key=lambda row: row['pk']) if row['pk']]
        if not keys:
            raise ValueError('Original record has no declared key')
        where, values = (' WHERE run_id=?', (self.source['run_id'],)) if 'run_id' in names else ('', ())
        if not values and table != 'work_evidence_materials':
            raise ValueError('Original record has no Run scope')
        return names, keys, where, values

    def _rows(self, table: str, where: str, values: Sequence[Any], *, after: int = 0,
              limit: int = 128) -> list[dict[str, Any]]:
        names, keys, _, _ = self._table(table)
        sizes = '+'.join('coalesce(length(CAST(' + _name(name) + ' AS BLOB)),0)+16' for name in names)
        order = ','.join(map(_name, keys))
        # Admit bytes inside SQLite before potentially damaged text enters Python.
        size = self.db.execute('SELECT coalesce(sum(n),0) FROM (SELECT ' + sizes + ' AS n FROM '
            + _name(table) + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?)', (*values, limit, after)).fetchone()[0]
        self.used += size
        if self.used > QUERY_LIMIT:
            raise ValueError('Original record exceeds query byte limit')
        return [dict(row) for row in self.db.execute('SELECT * FROM ' + _name(table) + where
                + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', (*values, limit, after))]

    def read(self, table: str, *, after: int = 0, limit: int = 128) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Invalid original history page')
        _, keys, where, values = self._table(table)
        rows = self._rows(table, where, values, after=after, limit=limit)
        more = self.db.execute('SELECT 1 FROM ' + _name(table) + where + ' LIMIT 1 OFFSET ?',
                               (*values, after + len(rows))).fetchone()
        return {'source': self.source, 'table': table,
                'records': [{'key': [row[key] for key in keys], 'values': row} for row in rows],
                'next': after + len(rows) if more else None}

    def row(self, table: str, key: Sequence[str | int]) -> dict[str, Any]:
        _, keys, where, values = self._table(table)
        if len(key) != len(keys):
            raise ValueError('Invalid original record key')
        where += (' AND ' if where else ' WHERE ') + ' AND '.join(_name(name) + '=?' for name in keys)
        rows = self._rows(table, where, (*values, *key), limit=1)
        if len(rows) != 1:
            raise ValueError('Original record is missing: ' + table)
        return rows[0]


def _rows(reader: V6RecordReader, table: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    after = 0
    while True:
        page = reader.read(table, after=after)
        result.extend(row['values'] for row in page['records'])
        if len(result) > 4096 or len(json.dumps(serializable(result)).encode()) > QUERY_LIMIT:
            raise ValueError('Original current facts exceed bounded query')
        if page['next'] is None:
            return result
        after = page['next']


def _evidence(reader: V6RecordReader, identity: str) -> dict[str, Any]:
    record = reader.row('work_evidence', (reader.source['run_id'], identity))
    # The migration reader's consumed-result subset excludes these two original
    # display members. Resolve them from that same verified raw return here.
    if record['source_member'] not in {'human_blockers', 'thread_id'}:
        return reader.evidence(identity)
    if record['material_id'] is not None or record['source_evidence_id'] is None:
        raise ValueError('Invalid original returned member')
    parent = reader.evidence(record['source_evidence_id'])
    if parent['record']['kind'] != 'returned_result' or parent['record']['source_evidence_id'] is not None:
        raise ValueError('Invalid original returned member source')
    value = parent['value'][record['source_member']]
    body = wire_json(value)
    if len(body) != record['value_bytes'] or hashlib.sha256(body).hexdigest() != record['value_digest']:
        raise ValueError('Original returned member digest mismatch')
    return {'source': reader.source, 'record': record, 'value': value}


def timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid original timestamp')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('Original timestamp has no timezone')
    return moment.timestamp()


def header(reader: V6RecordReader, path: Path) -> dict[str, Any]:
    run = reader.row('runs', (reader.source['run_id'],))
    root = reader.row('run_root', (run['run_id'],))
    if (not isinstance(run['run_id'], str) or not isinstance(run['repository'], str)
            or type(run['parent_number']) is not int or run['parent_number'] < 1
            or not isinstance(run['status'], str) or root['value_language'] not in {'en', 'zh'}):
        raise ValueError('Invalid original Run identity/language')
    source = reader.source
    if source['protocol'] != SOURCE_PROTOCOL:
        from .legacy_admission import read_source_fence
        fence = read_source_fence(source['protocol'])
        source['target_database'] = fence['target_db']
    return {'run_id': run['run_id'], 'repository': run['repository'], 'parent': run['parent_number'],
            'repository_root': None, 'language': root['value_language'],
            'created_at': timestamp(run['created_at']), 'phases': [run['phase'] or run['status']],
            'is_terminal': run['status'] in {'completed', 'abandoned'},
            'state_dir': str(path.parent), 'database': str(path), 'original_source': source}


def headers(path: Path, *, run_id: str | None = None, repository: str | None = None,
            parent: int | None = None) -> list[dict[str, Any]]:
    with connection(path) as db:
        clauses, values = [], []
        for column, value in (('run_id', run_id), ('lower(repository)', repository), ('parent_number', parent)):
            if value is not None:
                clauses.append(column + '=?')
                values.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        identities = db.execute('SELECT run_id FROM runs' + where + ' ORDER BY run_id LIMIT 257', values).fetchall()
        if len(identities) > 256:
            raise ValueError('Too many original Runs; select a narrower state source')
        return [header(OriginalRecords(db, path, row[0]), path) for row in identities]


def document(reader: V6RecordReader, path: Path, *, history: bool, after: int = 0,
             limit: int = 128) -> dict[str, Any]:
    location = header(reader, path)
    run_id = location['run_id']
    run, root = reader.row('runs', (run_id,)), reader.row('run_root', (run_id,))
    parent = reader.row('run_parent_versions', (run_id, root['value_parent_version_id']))
    def evidence(identity: str | None) -> Any:
        return _evidence(reader, identity)['value'] if identity is not None else None
    title, body = evidence(parent['title_id']), evidence(parent['body_id'])
    slots = _rows(reader, 'work_job_slots')
    jobs = [reader.row('work_jobs', (run_id, slot['job_id'])) for slot in slots
            if slot['container'] in {'ticket_jobs', 'parent_job', 'run_acceptance', 'run_publication'}]
    gates = _rows(reader, 'work_job_gates')
    findings = []
    for job in jobs:
        if not any(gate['job_id'] == job['job_id'] and gate['gate_kind'] != 'publication' for gate in gates):
            continue
        for field in ('human_blockers', 'unresolved_acceptance_artifact', 'failure_evidence'):
            identity = job.get('value_' + field)
            if identity is not None:
                findings.append({'job_id': job['job_id'], 'kind': field, 'value': evidence(identity)})
    attempts = _rows(reader, 'work_attempts')
    invocations = _rows(reader, 'work_invocations')
    invocations.sort(key=lambda item: (timestamp(item['started_at']), item['work_subject']))
    raw = {row['raw_result_evidence_id']: _evidence(reader, row['raw_result_evidence_id'])
           for row in invocations if row['raw_result_evidence_id'] is not None}
    data: dict[str, Any] = {**location, 'parent': {'number': run['parent_number'], 'title': title, 'body': body},
        'status': run['status'], 'phase': run['phase'], 'original_source': reader.source,
        'current_liveness': 'unknown', 'current_findings': findings,
        'recorded_run': run, 'recorded_root': root, 'recorded_jobs': jobs, 'recorded_gates': gates,
        'semantic_agent_attempts': attempts, 'agent_invocations': invocations, 'raw_returns': raw,
        'elapsed_seconds': None, 'events': [], 'history_page': {'after': after, 'next_after': after, 'has_more': False}}
    if history:
        responses = []
        for job in jobs:
            for field in ('human_feedback', 'current_human_response'):
                if job.get('value_' + field) is not None:
                    responses.append({'job_id': job['job_id'], 'kind': field,
                                      'value': evidence(job['value_' + field])})
        for archived in _rows(reader, 'work_job_archives'):
            if archived['history_kind'] in {'human_blocker_history', 'human_response_history'} and archived['evidence_id'] is not None:
                responses.append({'record': archived, 'value': evidence(archived['evidence_id'])})
        data['human_feedback'] = responses
        page = reader.history(after=after, limit=limit)
        events = []
        for item in page['events']:
            stored = item['event']
            values = {key[6:]: value for key, value in stored.items() if key.startswith('value_')}
            for field in ('human_blockers', 'graph_change_summary', 'required_checks_evidence', 'external_wait_reason'):
                if values.get(field) is not None:
                    values[field] = evidence(values[field])
            events.append({'slot': item['slot'], 'record': stored, 'facts': values,
                           'created_at': timestamp(values.get('at'))})
        data['events'] = events
        data['history_page'] = {'after': after, 'next_after': page['next'] if page['next'] is not None else after + len(events),
                                'has_more': page['next'] is not None}
    return {key: serializable(value) for key, value in data.items()}


def serializable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'body': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def locator_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError('Invalid original Run locator')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {'entries'} or not isinstance(data['entries'], list) or len(data['entries']) > 32:
        raise ValueError('Invalid original Run locator')
    for entry in data['entries']:
        fields = {'run_id', 'repository_root', 'state_dir', 'updated_at'}
        if (not isinstance(entry, dict) or set(entry) not in (fields, fields | {'repository', 'parent_number'})
                or any(not isinstance(entry[key], str) or not entry[key] for key in fields)
                or not Path(entry['repository_root']).is_absolute() or not Path(entry['state_dir']).is_absolute()):
            raise ValueError('Invalid original Run locator entry')
        if 'repository' in entry and (not isinstance(entry['repository'], str)
                or type(entry['parent_number']) is not int or entry['parent_number'] < 1):
            raise ValueError('Invalid original locator route')
    return data['entries']
