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

from agent_run.managed_workspace import normalize_repository
from agent_run.git_errors import GitError

from .migration import SOURCE_PROTOCOL, source_identity
from .migration_records import V6RecordReader
from .storage import QUERY_LIMIT, Storage
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


    def count(self, table: str) -> int:
        _, _, where, values = self._table(table)
        return int(self.db.execute('SELECT count(*) FROM ' + _name(table) + where, values).fetchone()[0])


class ImportedRecords(V6RecordReader):
    """Read immutable archive rows through Storage, without a writable Kernel."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        with storage.transaction(write=False) as tx:
            record = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_record_import'")
            self.subject = str(record['subject'])
            manifest = json.loads(tx.get(record['material_id'], self.subject))
            if (manifest.get('format') != 'v6-records/1'
                    or manifest['source']['run_id'] != record['identity']
                    or record['identity'] != tx.one('SELECT id FROM runs')[0]):
                raise ValueError('record_import_identity')
            self._manifest = manifest

    def count(self, table: str) -> int:
        return int(self._manifest['tables'][table]['count'])


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
        if page['next'] <= after:
            raise ValueError('Original record page did not advance')
        after = page['next']


class Evidence:
    """Resolve only the selected references and their verified parent closure."""

    def __init__(self, reader: V6RecordReader) -> None:
        self.reader = reader
        self.cache: dict[str, dict[str, Any]] = {}
        self.used = 0

    def get(self, identity: str, *, owner: str | None = None, kind: str | None = None) -> dict[str, Any]:
        cached = self.cache.get(identity)
        record = cached['record'] if cached is not None else self.reader.row('work_evidence', (self.reader.source['run_id'], identity))
        if (owner is not None and record['owner_id'] != owner or kind is not None and record['kind'] != kind):
            raise ValueError('source_evidence_owner')
        if cached is not None:
            return cached
        if type(record['value_bytes']) is not int or not 0 <= record['value_bytes'] <= 4 * 1024 * 1024:
            raise ValueError('source_evidence')
        if record['source_evidence_id'] is not None:
            parent_record = self.reader.row('work_evidence', (self.reader.source['run_id'], record['source_evidence_id']))
            member = record['source_member']
            if (record['material_id'] is not None or parent_record['source_evidence_id'] is not None
                    or parent_record['kind'] != ('human_response_history' if member == 'response' else 'returned_result')):
                raise ValueError('source_evidence')
            # Attempt members belong to that same Attempt's raw return, even
            # when first reached without an owner constraint or through cache.
            # Job projections may legitimately reference another role's Attempt.
            parent_owner = record['owner_id'] if record['owner_id'].startswith('attempt:') else None
            parent = self.get(record['source_evidence_id'], owner=parent_owner)['value']
            if not isinstance(parent, dict):
                raise ValueError('source_evidence')
            if member == 'response' and isinstance(parent.get('response'), str) and parent['response'].strip():
                value = parent['response']
            elif member in {'summary', 'artifact', 'human_blockers', 'thread_id'} and member in parent:
                value = parent[member]
            elif member == 'publication_fields' and isinstance(parent.get('artifact'), dict):
                value = {key: parent['artifact'][key] for key in ('commit_message', 'pr_title', 'pr_body_markdown')}
            else:
                raise ValueError('source_evidence')
            body = wire_json(value)
        else:
            if record['source_member'] is not None or record['material_id'] != record['value_digest']:
                raise ValueError('source_evidence')
            material = self.reader.row('work_evidence_materials', (record['material_id'],))
            if not isinstance(material['body'], str) or material['byte_length'] != record['value_bytes']:
                raise ValueError('source_evidence')
            body = material['body'].encode('utf-8')
            value = json.loads(body)
        if len(body) != record['value_bytes'] or hashlib.sha256(body).hexdigest() != record['value_digest']:
            raise ValueError('source_evidence')
        self.used += len(body)
        if self.used > QUERY_LIMIT:
            raise ValueError('Original evidence closure exceeds query byte limit')
        result = {'source': self.reader.source, 'record': record, 'value': value}
        self.cache[identity] = result
        return result


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
    closure = Evidence(reader)
    def evidence(identity: str | None) -> Any:
        return closure.get(identity)['value'] if identity is not None else None
    title, body = evidence(parent['title_id']), evidence(parent['body_id'])
    slots = _rows(reader, 'work_job_slots')
    jobs = [reader.row('work_jobs', (run_id, slot['job_id'])) for slot in slots
            if slot['container'] in {'ticket_jobs', 'parent_job', 'run_acceptance', 'run_publication'}]
    gates = _rows(reader, 'work_job_gates')
    events = []
    continuation = None
    if history:
        page = reader.history(after=after, limit=limit)
        continuation = page['next']
        for item in page['events']:
            stored = item['event']
            columns = {key[6:]: value for key, value in stored.items() if key.startswith('value_')}
            names, mask = sorted(columns), stored['presence']
            if not isinstance(mask, bytes) or len(mask) != (len(names) + 7) // 8:
                raise ValueError('Invalid original event presence')
            bits = int.from_bytes(mask, 'little')
            if bits >> len(names):
                raise ValueError('Invalid original event presence')
            values = {name: columns[name] for index, name in enumerate(names) if bits & (1 << index)}
            if any(columns[name] is not None for name in columns.keys() - values.keys()):
                raise ValueError('Unexpected original event column')
            for field in ('human_blockers', 'graph_change_summary', 'required_checks_evidence', 'external_wait_reason'):
                if values.get(field) is not None:
                    values[field] = evidence(values[field])
            if hashlib.sha256(wire_json(values)).hexdigest() != stored['event_id']:
                raise ValueError('Original event digest mismatch')
            events.append({'slot': item['slot'], 'record': stored, 'facts': values,
                           'created_at': timestamp(values.get('at'))})
        # References, not timestamps or the latest Invocation, select this page.
        attempt_ids = {event['facts'][field] for event in events
                       for field in ('semantic_attempt_id', 'explicit_resume_attempt_id')
                       if event['facts'].get(field) is not None}
        attempts = [reader.row('work_attempts', (run_id, identity)) for identity in sorted(attempt_ids)]
        owners = {attempt['attempt_id']: attempt['work_subject'] for attempt in attempts}
        references = {(event['facts']['semantic_attempt_id'], event['facts']['agent_invocation_started_at'])
                      for event in events if event['facts'].get('agent_invocation_started_at') is not None}
        invocations = []
        for identity, started in sorted(references):
            invocation = reader.row('work_invocations', (run_id, owners[identity], started))
            if invocation['attempt_id'] != identity:
                raise ValueError('Original Invocation and event Attempt differ')
            invocations.append(invocation)
    else:
        refs = _rows(reader, 'work_invocation_current')
        invocations = [reader.row('work_invocations', key) for key in sorted({
            (run_id, ref['work_subject'], ref['started_at']) for ref in refs})]
        attempt_ids = {row['attempt_id'] for row in invocations if row['attempt_id'] is not None}
        attempt_ids.update(row['pending_attempt_id'] for row in _rows(reader, 'work_job_attempt_roots')
                           if row['pending_attempt_id'] is not None)
        attempts = [reader.row('work_attempts', (run_id, identity)) for identity in sorted(attempt_ids)]
    version_keys = {(row['attempt_id'], row['attempt_version_id']) for row in invocations
                    if row['attempt_id'] is not None and row['attempt_version_id'] is not None}
    historical = ([row for row in _rows(reader, 'work_job_attempt_history') if row['attempt_id'] in attempt_ids]
                  if attempt_ids else [])
    version_keys.update((row['attempt_id'], row['version_id']) for row in historical)
    for attempt in attempts:
        if not history:
            current = reader.row('work_attempt_current', (run_id, attempt['attempt_id']))
            version_keys.add((attempt['attempt_id'], current['version_id']))
    versions = []
    for identity, version_id in sorted(version_keys):
        version = reader.row('work_attempt_versions', (run_id, identity, version_id))
        facts = {field: closure.get(value, owner='attempt:' + identity, kind=field[:-12])['value']
                 for field, value in version.items() if field.endswith('_evidence_id') and value is not None}
        versions.append({'record': version, 'evidence': facts})
    raw = {row['raw_result_evidence_id']: closure.get(row['raw_result_evidence_id'],
               owner='attempt:' + row['attempt_id'], kind='returned_result')
           for row in invocations if row['raw_result_evidence_id'] is not None}
    findings = []
    if not history:
        for job in jobs:
            if not any(gate['job_id'] == job['job_id'] and gate['gate_kind'] != 'publication' for gate in gates):
                continue
            for field in ('human_blockers', 'unresolved_acceptance_artifact', 'failure_evidence'):
                if job.get('value_' + field) is not None:
                    findings.append({'job_id': job['job_id'], 'kind': field, 'value': evidence(job['value_' + field])})
    data: dict[str, Any] = {**location, 'parent': {'number': run['parent_number'], 'title': title, 'body': body},
        'status': run['status'], 'phase': run['phase'], 'original_source': reader.source,
        'current_liveness': 'unknown', 'current_findings': findings,
        'recorded_run': run, 'recorded_root': root, 'recorded_jobs': jobs, 'recorded_gates': gates,
        'semantic_agent_attempts': attempts, 'attempt_versions': versions, 'agent_invocations': invocations, 'raw_returns': raw,
        'elapsed_seconds': None, 'events': events,
        'history_page': {'after': after, 'next_after': continuation if continuation is not None else after + len(events),
                         'has_more': continuation is not None}}
    if history:
        responses = []
        sequences = {event['facts']['explicit_resume_sequence'] for event in events
                     if event['facts'].get('explicit_resume_sequence') is not None}
        resume_ids = {row['resume_id'] for row in invocations if row['resume_id'] is not None}
        for resume in _rows(reader, 'run_resume_events') if sequences or resume_ids else []:
            if (resume['sequence'] in sequences or resume['resume_id'] in resume_ids) and resume['human_response_supplied']:
                response = reader.row('run_human_responses', (run_id, resume['resume_id']))
                responses.append({'record': resume, 'value': evidence(response['evidence_id'])})
        data['human_feedback'] = responses

    raw.update({identity: item for identity, item in closure.cache.items() if item['record']['kind'] == 'returned_result'})
    return {key: serializable(value) for key, value in data.items()}


def serializable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'body': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def locator_entries(path: Path, *, run_id: str | None = None, repository: str | None = None,
                    parent: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError('Invalid original Run locator')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {'entries'} or not isinstance(data['entries'], list) or len(data['entries']) > 32:
        raise ValueError('Invalid original Run locator')
    selected = []
    for entry in data['entries']:
        if not isinstance(entry, dict):
            raise ValueError('Invalid original Run locator entry')
        # Only a valid route discriminator proves this entry unrelated. Paths
        # and other payload fields are validated after that scope decision.
        if (run_id is not None and isinstance(entry.get('run_id'), str)
                and entry['run_id'] and entry['run_id'] != run_id):
            continue
        try:
            routed_repository = normalize_repository(entry['repository']) if isinstance(entry.get('repository'), str) else None
        except GitError:
            routed_repository = None
        if repository is not None and routed_repository is not None and routed_repository != repository:
            continue
        if parent is not None and type(entry.get('parent_number')) is int and entry['parent_number'] > 0 and entry['parent_number'] != parent:
            continue
        fields = {'run_id', 'repository_root', 'state_dir', 'updated_at'}
        if (set(entry) not in (fields, fields | {'repository', 'parent_number'})
                or any(not isinstance(entry[key], str) or not entry[key] for key in fields)
                or not Path(entry['repository_root']).is_absolute() or not Path(entry['state_dir']).is_absolute()):
            raise ValueError('Invalid original Run locator entry')
        if 'repository' in entry and (not isinstance(entry['repository'], str)
                or routed_repository is None or type(entry['parent_number']) is not int or entry['parent_number'] < 1):
            raise ValueError('Invalid original locator route')
        selected.append(entry)
    return selected
