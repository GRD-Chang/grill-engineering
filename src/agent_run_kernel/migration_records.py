"""Immutable v6 records and a bounded reader; these records grant no execution.

Only an explicitly accepted Run's relation closure is copied. Native Attempts,
budgets and Completion are installed separately by the completed-state mapper.
Historical Invocations never become native Executors, leases or new calls.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Sequence

from .kernel import Kernel
from .migration import MigrationRefused, SourceSnapshot, capture_v6
from .migration_quiescence import CONTROL_TABLES
from .model import wire_json


RECORD_TABLES = CONTROL_TABLES | frozenset({
    'schema_meta', 'runs', 'run_root', 'run_condition_base', 'run_parent_versions',
    'run_graph_versions', 'run_graph_snapshots', 'run_graph_scope_order',
    'run_graph_ticket_slots', 'run_graph_members', 'run_frontier', 'run_query_roots',
    'run_resume_roots', 'run_timeline_events', 'run_timeline_slots',
    'run_closeout_headers', 'run_cleanup_items', 'work_jobs', 'work_job_slots',
    'work_job_layout', 'work_job_archives', 'work_job_archive_roots',
    'work_job_attempt_roots', 'work_job_attempt_history', 'work_job_acceptance_record',
    'work_job_review_artifacts', 'work_job_review_budget', 'work_job_ci_recovery',
    'work_job_integration_resync', 'work_job_merge_intent', 'work_job_record_strings',
    'work_job_ticket_close_intent', 'work_job_ticket_close_ownership',
    'work_invocation_roots', 'work_invocation_current', 'work_invocation_history',
    'work_invocations', 'work_invocation_segments', 'work_invocation_wait_intervals',
    'work_evidence', 'work_evidence_materials', 'work_attempts', 'work_attempt_versions',
    'work_attempt_current', 'work_attempt_returns', 'work_attempt_result_members',
    'work_job_budget_events',
})
_ROW_LIMIT = 8 * 1024 * 1024
_PAGE_LIMIT = 512 * 1024
_FORMAT = 'v6-records/1'


def _name(value: str) -> str:
    if re.fullmatch(r'[a-z][a-z_0-9]*', value) is None:
        raise MigrationRefused('source_record_schema')
    return '"' + value + '"'


def _scope(table: str, columns: Sequence[str], run_id: str) -> tuple[str, tuple[str, ...]]:
    if table == 'schema_meta':
        return '', ()
    if table == 'work_evidence_materials':
        return (' WHERE material_id IN (SELECT material_id FROM work_evidence '
                'WHERE run_id=? AND material_id IS NOT NULL)', (run_id,))
    if table in CONTROL_TABLES:
        return (' WHERE task_id IN (SELECT task_id FROM control_tasks WHERE run_id=?)', (run_id,))
    if 'run_id' in columns:
        return ' WHERE run_id=?', (run_id,)
    raise MigrationRefused('source_record_scope')


def preserve_v6_records(snapshot: SourceSnapshot, kernel: Kernel, *, run_id: str, subject: str) -> None:
    """Preserve immutable records in an unpublished native candidate, without authority.

    Capture again at this public seam so an open source descriptor is refused
    before any source connection can release its owner's POSIX locks.
    """
    with tempfile.TemporaryDirectory(prefix='v6-records-') as directory:
        copied = capture_v6(snapshot.path, Path(directory) / 'source.sqlite3')
        if copied.sha256 != snapshot.sha256 or copied.schema_sha256 != snapshot.schema_sha256:
            raise MigrationRefused('snapshot_changed')
        _preserve_copy(copied, kernel, run_id=run_id, subject=subject)


def _preserve_copy(snapshot: SourceSnapshot, kernel: Kernel, *, run_id: str, subject: str,
                   profile_document: bytes | None = None) -> None:
    """The mapper passes its already captured, privately owned source copy."""
    with kernel.storage.transaction(write=False) as tx:
        if (tx.one('SELECT id,executor FROM runs')['id'] != run_id
                or tx.one('SELECT executor FROM runs')[0] is not None
                or tx.rows("SELECT position FROM history WHERE kind='v6_record_import'")):
            raise MigrationRefused('record_import_identity')
        tx.one('SELECT id FROM subjects WHERE id=?', (subject,))
    source = {'run_id': run_id, 'protocol': snapshot.source_protocol,
              'sha256': snapshot.sha256, 'schema_sha256': snapshot.schema_sha256}
    manifest: dict[str, Any] = {'format': _FORMAT, 'source': source, 'tables': {}, 'documents': {}}

    def put(purpose: str, body: bytes) -> str:
        with kernel.storage.transaction() as tx:
            return tx.put(subject, purpose, body)

    def cell(value: Any) -> dict[str, Any]:
        kind = {type(None): 'null', int: 'integer', float: 'real', str: 'text', bytes: 'blob'}.get(type(value))
        if kind is None:
            raise MigrationRefused('source_record_type')
        if isinstance(value, (str, bytes)):
            raw = value.encode('utf-8') if isinstance(value, str) else value
            if len(raw) > 4 * 1024 * 1024:
                raise MigrationRefused('source_record_size')
            if len(raw) > (1024 if isinstance(value, str) else 64):
                return {'type': kind, 'material': put('v6_record_value', raw), 'size': len(raw)}
            if isinstance(value, bytes):
                return {'type': kind, 'hex': value.hex(), 'size': len(value)}
        return {'type': kind, 'value': value}

    with closing(sqlite3.connect(snapshot.path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        parameters: tuple[str, ...]
        if db.execute('SELECT count(*) FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] != 1:
            raise MigrationRefused('source_record_identity')
        if profile_document is not None:
            if len(profile_document) > 4 * 1024 * 1024:
                raise MigrationRefused('source_record_size')
            profile = json.loads(profile_document)
            if not isinstance(profile, dict) or profile.get('run_id') != run_id:
                raise MigrationRefused('source_profile')
            manifest['documents']['profile'] = {
                'material': put('v6_profile_document', profile_document), 'size': len(profile_document),
                'sha256': hashlib.sha256(profile_document).hexdigest()}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            info = [dict(row) for row in db.execute(f'PRAGMA table_info({_name(table)})')]
            names = [entry['name'] for entry in info]
            if table not in RECORD_TABLES:
                where, parameters = (' WHERE run_id=?', (run_id,)) if 'run_id' in names else ('', ())
                if db.execute(f'SELECT 1 FROM {_name(table)}{where} LIMIT 1', parameters).fetchone():
                    raise MigrationRefused('unsupported_source_relations')
                continue
            primary = [entry['name'] for entry in sorted(info, key=lambda entry: entry['pk']) if entry['pk']]
            if not primary or len(names) > 256:
                raise MigrationRefused('source_record_schema')
            where, parameters = _scope(table, names, run_id)
            sizes = '+'.join(f'CASE WHEN typeof({_name(name)}) IN (\'text\',\'blob\') '
                             f'THEN length(CAST({_name(name)} AS BLOB)) ELSE 16 END' for name in names)
            largest = db.execute(f'SELECT coalesce(max({sizes}),0) FROM {_name(table)}{where}', parameters).fetchone()[0]
            if largest > _ROW_LIMIT:
                raise MigrationRefused('source_record_size')
            pages: list[dict[str, Any]] = []
            batch: list[dict[str, Any]] = []
            count = batch_size = body_size = 0

            def flush() -> None:
                nonlocal count, batch, batch_size, body_size
                if batch:
                    pages.append({'start': count, 'count': len(batch), 'first': batch[0]['key'],
                                  'last': batch[-1]['key'],
                                  'material': put('v6_record_page', wire_json(batch))})
                    count += len(batch)
                    batch, batch_size, body_size = [], 0, 0

            order = ','.join(_name(name) for name in primary)
            for row in db.execute(f'SELECT * FROM {_name(table)}{where} ORDER BY {order}', parameters):
                key = [row[name] for name in primary]
                if any(type(value) not in {int, str} or isinstance(value, str) and len(value.encode()) > 1024 for value in key):
                    raise MigrationRefused('source_record_key')
                size = sum(len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 16 for value in row)
                record = {'key': key, 'cells': [cell(value) for value in row], 'size': size}
                encoded_size = len(wire_json(record))
                if batch and (len(batch) == 128 or batch_size + size > 4 * 1024 * 1024 or body_size + encoded_size > _PAGE_LIMIT):
                    flush()
                batch.append(record)
                batch_size += size
                body_size += encoded_size
            flush()
            manifest['tables'][table] = {'columns': info, 'primary_key': primary, 'count': count, 'pages': pages}
    with kernel.storage.transaction() as tx:
        material = tx.put(subject, 'v6_record_manifest', wire_json(manifest))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,?,?)',
                   (subject, 'v6_record_import', run_id, material, 1, kernel.clock()))


class V6RecordReader:
    """Read original identities, values and chronology with source provenance."""

    def __init__(self, kernel: Kernel) -> None:
        self.storage = kernel.storage
        with self.storage.transaction(write=False) as tx:
            record = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_record_import'")
            self.subject = str(record['subject'])
            manifest = json.loads(tx.get(record['material_id'], self.subject))
            if (manifest.get('format') != _FORMAT or manifest['source']['run_id'] != record['identity']
                    or record['identity'] != tx.one('SELECT id FROM runs')[0]):
                raise MigrationRefused('record_import_identity')
            self._manifest: dict[str, Any] = manifest

    @property
    def source(self) -> dict[str, str]:
        return dict(self._manifest['source'])

    def tables(self) -> dict[str, Any]:
        return deepcopy({name: {key: value for key, value in table.items() if key != 'pages'}
                         for name, table in self._manifest['tables'].items()})

    def document(self, name: str) -> dict[str, Any]:
        metadata = self._manifest.get('documents', {}).get(name)
        if metadata is None:
            raise MigrationRefused('source_document_missing')
        with self.storage.transaction(write=False) as tx:
            body = tx.get(metadata['material'], self.subject)
        if len(body) != metadata['size'] or hashlib.sha256(body).hexdigest() != metadata['sha256']:
            raise MigrationRefused('source_document_changed')
        return {'source': self.source, 'name': name, 'body': body, 'sha256': metadata['sha256']}

    def _page(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        with self.storage.transaction(write=False) as tx:
            rows: list[dict[str, Any]] = json.loads(tx.get(page['material'], self.subject))
        if (len(rows) != page['count'] or rows[0]['key'] != page['first'] or rows[-1]['key'] != page['last']):
            raise MigrationRefused('source_record_page')
        return rows

    def _decode(self, table: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        values = []
        with self.storage.transaction(write=False) as tx:
            for cell in record['cells']:
                if 'material' in cell:
                    raw = tx.get(cell['material'], self.subject)
                    if len(raw) != cell['size']:
                        raise MigrationRefused('source_record_size')
                    value = raw.decode('utf-8') if cell['type'] == 'text' else raw
                elif cell['type'] == 'blob':
                    value = bytes.fromhex(cell['hex'])
                    if len(value) != cell['size']:
                        raise MigrationRefused('source_record_size')
                else:
                    value = cell['value']
                values.append(value)
        if len(values) != len(table['columns']):
            raise MigrationRefused('source_record_schema')
        result = dict(zip((column['name'] for column in table['columns']), values, strict=True))
        if ([result[name] for name in table['primary_key']] != record['key']
                or 'run_id' in result and result['run_id'] != self.source['run_id']):
            raise MigrationRefused('source_record_scope')
        return result

    def read(self, table: str, *, after: int = 0, limit: int = 128) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 128:
            raise MigrationRefused('source_record_page')
        metadata = self._manifest['tables'].get(table)
        if metadata is None or after > metadata['count']:
            raise MigrationRefused('source_record_table')
        result: list[dict[str, Any]] = []
        used, position = 0, after
        for page in metadata['pages']:
            if page['start'] + page['count'] <= after:
                continue
            for record in self._page(page)[max(0, after - page['start']):]:
                if len(result) == limit or used + record['size'] > _ROW_LIMIT:
                    return {'source': self.source, 'table': table, 'records': result, 'next': position}
                result.append({'key': record['key'], 'values': self._decode(metadata, record)})
                position += 1
                used += record['size']
        return {'source': self.source, 'table': table, 'records': result, 'next': None}

    def row(self, table: str, key: Sequence[str | int]) -> dict[str, Any]:
        metadata = self._manifest['tables'].get(table)
        if metadata is None or len(key) != len(metadata['primary_key']):
            raise MigrationRefused('source_record_key')
        for page in metadata['pages']:
            if tuple(page['first']) <= tuple(key) <= tuple(page['last']):
                for record in self._page(page):
                    if record['key'] == list(key):
                        return self._decode(metadata, record)
        raise MigrationRefused('source_record_missing')

    def evidence(self, evidence_id: str) -> dict[str, Any]:
        """Resolve the original evidence relation, including derived raw members.

        No current candidate, model profile or native authorization is supplied
        here. The value must match its original digest and original byte count.
        """
        record = self.row('work_evidence', (self.source['run_id'], evidence_id))
        return {'source': self.source, 'record': record,
                'value': self._evidence_value(record, derived=True)}

    def _evidence_value(self, record: dict[str, Any], *, derived: bool) -> Any:
        if (type(record['value_bytes']) is not int
                or not 0 <= record['value_bytes'] <= 4 * 1024 * 1024):
            raise MigrationRefused('source_evidence')
        if record['source_evidence_id'] is not None:
            original = self.row('work_evidence', (self.source['run_id'], record['source_evidence_id']))
            if (not derived or record['material_id'] is not None
                    or original['kind'] != 'returned_result'
                    or original['source_evidence_id'] is not None):
                raise MigrationRefused('source_evidence')
            envelope = self._evidence_value(original, derived=False)
            member = record['source_member']
            if not isinstance(envelope, dict):
                raise MigrationRefused('source_evidence')
            if member in {'summary', 'artifact'} and member in envelope:
                value = envelope[member]
            elif member == 'publication_fields' and isinstance(envelope.get('artifact'), dict):
                fields = ('commit_message', 'pr_title', 'pr_body_markdown')
                if any(key not in envelope['artifact'] for key in fields):
                    raise MigrationRefused('source_evidence')
                value = {key: envelope['artifact'][key] for key in fields}
            else:
                raise MigrationRefused('source_evidence')
            body = wire_json(value)
        else:
            if record['source_member'] is not None or record['material_id'] != record['value_digest']:
                raise MigrationRefused('source_evidence')
            material = self.row('work_evidence_materials', (record['material_id'],))
            if not isinstance(material['body'], str) or material['byte_length'] != record['value_bytes']:
                raise MigrationRefused('source_evidence')
            body = material['body'].encode('utf-8')
            try:
                value = json.loads(body)
            except ValueError as error:
                raise MigrationRefused('source_evidence') from error
        if len(body) != record['value_bytes'] or hashlib.sha256(body).hexdigest() != record['value_digest']:
            raise MigrationRefused('source_evidence')
        return value

    def history(self, *, after: int = 0, limit: int = 128) -> dict[str, Any]:
        """Original slot order and event text; presence/NULL remain in records."""
        page = self.read('run_timeline_slots', after=after, limit=limit)
        events: list[dict[str, Any]] = []
        used = 0
        for item in page['records']:
            slot = item['values']
            event = self.row('run_timeline_events', (self.source['run_id'], slot['event_id']))
            size = sum(len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, bytes)
                       else 16 for record in (slot, event) for value in record.values())
            if used + size > _ROW_LIMIT:
                if not events:
                    raise MigrationRefused('source_record_size')
                return {'source': self.source, 'events': events, 'next': after + len(events)}
            events.append({'slot': slot, 'event': event})
            used += size
        return {'source': self.source, 'events': events, 'next': page['next']}

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        """Read a semantic Attempt's own identity, current facts and budget.

        Older versions and physical Invocation bindings are separately readable
        relations; neither is rebound to this version or a native candidate.
        """
        key = (self.source['run_id'], attempt_id)
        attempt = self.row('work_attempts', key)
        current = self.row('work_attempt_current', key)
        version = self.row('work_attempt_versions', (*key, current['version_id']))
        evidence = {field: self.evidence(version[field]) for field in (
            'budget_snapshot_evidence_id', 'review_context_evidence_id',
            'returned_result_evidence_id') if version[field] is not None}
        return {'source': self.source, 'attempt': attempt, 'current': current,
                'version': version, 'evidence': evidence}
