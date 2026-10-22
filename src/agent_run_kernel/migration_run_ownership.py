"""Authenticate historical Run-ref ownership; never authorize a current write.

The retired source fence anchors the original owner set. Archive labels and
content-addressed native materials alone cannot authenticate that set. H0 is
the imported local ref's historical head, not an observation of its current OID
and not a receipt for a later native write.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator, Mapping

from agent_run.task_identity import TaskKey
from agent_run.managed_workspace import ManagedWorkspace

from .effects import LocalGit
from .cleanup_adapter import _read
from .legacy_admission import read_source_fence
from .migration import source_identity
from .migration_records import V6RecordReader
from .model import Blocker, Effect, Issue, KernelError, ParentIssue, Requirements, ResourceWitness, wire_json
from .storage import Storage, Transaction


def _idle_checkout_state(git: LocalGit, checkout: Path, common: Path, *, allow_dirty: bool = False
        ) -> tuple[bytes | None, tuple[str, ...] | None]:
    """Read a detached registration, or prove both path and registration absent."""
    try:
        try:
            mode = checkout.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and not stat.S_ISDIR(mode) or checkout.resolve() != checkout:
            raise KernelError('foreign_local_checkout')
        registered = []
        for entry in _read(git, 'worktree', 'list', '--porcelain', '-z').stdout.split('\0\0'):
            fields = entry.strip('\0').split('\0')
            if fields[0] == 'worktree ' + str(checkout):
                registered.append(fields)
        if mode is None and not registered:
            return None, None
        if (mode is None or len(registered) != 1 or 'detached' not in registered[0]
                or any(field.startswith(('prunable', 'locked', 'branch ')) for field in registered[0])):
            raise KernelError('foreign_local_checkout')
        if (git.run('rev-parse', '--show-toplevel', cwd=checkout).stdout.strip() != str(checkout)
                or Path(git.run('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=checkout).stdout.strip()).resolve() != common):
            raise KernelError('foreign_local_checkout')
        symbolic = git.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False)
        if symbolic.returncode == 0:
            raise KernelError('foreign_local_checkout')
        if symbolic.returncode != 1:
            raise KernelError('local_read_failed')
        head = git.run('rev-parse', '--verify', 'HEAD^{commit}', cwd=checkout).stdout.strip()
        raw = Path(git.run('rev-parse', '--path-format=absolute', '--git-path', 'HEAD', cwd=checkout).stdout.strip()).read_bytes()
        status = tuple(git.run('status', '--porcelain', cwd=checkout).stdout.splitlines())
        if raw != (head + '\n').encode('ascii') or 'HEAD ' + head not in registered[0] or status and not allow_dirty:
            raise KernelError('foreign_local_checkout')
        return raw, status
    except (OSError, UnicodeError) as error:
        raise KernelError('local_read_failed') from error
    except KernelError as error:
        if error.code == 'git_operation_failed':
            raise KernelError('local_read_failed') from error
        raise


_IDLE_ENTRY_KEYS = {'subject', 'generation', 'source_record', 'disposition', 'source_ref',
    'expected_oid', 'checkout', 'owned_root', 'workspace_oid', 'before_head', 'checkout_status'}
_IDLE_HEADER_KEYS = {'format', 'source_sha256', 'run_id', 'repository_root', 'common_git_dir',
    'copy_uuid', 'resources'}


def prebuilt_workspace_binding(storage: Storage, tx: Transaction, run: sqlite3.Row,
        job: sqlite3.Row) -> dict[str, Any] | None:
    """Authenticate immutable copied idle membership without Git or live source IO."""
    if job['kind'] != 'ticket' or job['generation'] != 1:
        return None
    rows = tx.rows("SELECT id,subject FROM material WHERE purpose='v6_import_identity'", limit=2)
    if not rows:
        return None
    try:
        if len(rows) != 1:
            raise ValueError('identity')
        imported = json.loads(tx.get(rows[0]['id'], rows[0]['subject']))
        if not isinstance(imported, dict):
            raise ValueError('identity')
        proof = imported.get('local_resources')
        if proof is None or isinstance(proof, dict) and proof.get('format') == 'copied-returned-resource/1':
            return None
        if (not isinstance(proof, dict) or set(proof) != _IDLE_HEADER_KEYS
                or proof['format'] != 'copied-idle-resources/1' or imported['source_mode'] != 'copied'
                or imported['scope'] not in ('single-ticket', 'selected-graph')
                or proof['run_id'] != run['id'] or proof['source_sha256'] != imported['source_sha256']
                or proof['repository_root'] != run['repository_root']
                or proof['common_git_dir'] != str(Path(run['repository_root']) / '.git')
                or not isinstance(proof['resources'], list) or not proof['resources']):
            raise ValueError('identity')
        expected = hashlib.sha256(wire_json({k: v for k, v in proof.items() if k != 'copy_uuid'})).hexdigest()[:32]
        if proof['copy_uuid'] != expected:
            raise ValueError('commitment')
        records = V6RecordReader(storage)
        root = records.row('run_root', (run['id'],))
        graph = records.row('run_graph_snapshots', (run['id'], root['value_graph_snapshot_id']))
        base = records.row('run_condition_base', (run['id'],))['value_sha']
        if records.source['sha256'] != proof['source_sha256'] or records.source['run_id'] != run['id']:
            raise ValueError('source')
        members: list[list[Any]] = []
        after = 0
        while True:
            page = records.read('run_graph_members', after=after)
            members.extend(item['key'] for item in page['records'] if item['key'][1] == graph['version_id'])
            if page['next'] is None:
                break
            after = page['next']
        entries = proof['resources']
        if (any(not isinstance(item, dict) or set(item) != _IDLE_ENTRY_KEYS for item in entries)
                or [item['subject'] for item in entries] != sorted({item['subject'] for item in entries})
                or {tuple(item['source_record']['key']) for item in entries} != {tuple(key) for key in members}):
            raise ValueError('members')
        for item in entries:
            key = item['source_record']['key']
            if (item['source_record'] != {'table': 'run_graph_members', 'key': key}
                    or len(key) != 3 or key[:2] != [run['id'], graph['version_id']]
                    or type(key[2]) is not int or item['subject'] != f'ticket:{key[2]}'
                    or type(item['generation']) is not int or item['generation'] != 1
                    or item['disposition'] != 'new-unused' or item['expected_oid'] is not None
                    or item['workspace_oid'] != base
                    or (item['before_head'], item['checkout_status']) not in ((None, None), (base + '\n', []))
                    or Path(item['checkout']).parent != Path(item['owned_root'])
                    or not Path(item['owned_root']).is_relative_to(storage.path.parent)
                    or not Path(proof['repository_root']).is_relative_to(storage.path.parent)):
                raise ValueError('resource')
            records.row('run_graph_members', key)
        selected = [item for item in entries if item['subject'] == job['id']]
        if not selected:
            return None  # Later native members are outside the imported graph.
        item = selected[0]
        if (item['source_record']['key'][2] != job['issue']
                or any(item[k] != job[k] for k in ('source_ref', 'checkout', 'owned_root'))
                or proof['common_git_dir'] != job['common_git_dir']):
            raise ValueError('native_resource')
        return {**{k: v for k, v in proof.items() if k != 'resources'}, **item,
            'format': 'copied-idle-workspace/1'}
    except (ValueError, KeyError, TypeError, IndexError, KernelError) as error:
        raise KernelError('prebuilt_workspace_unproven') from error


def authenticate_prebuilt_workspace(git: LocalGit, effect: Effect) -> dict[str, Any] | None:
    """Check current private physical identity; H1 and dispatch belong to the caller."""
    if 'prebuilt_workspace' not in effect.payload:
        return None
    if effect.operation != 'workspace' or effect.payload.get('create_ref') is not True:
        raise KernelError('foreign_local_checkout')
    try:
        resource = ResourceWitness(effect.subject, 1, 'development', effect.payload['checkout'],
            effect.payload['owned_root'], effect.payload['common_git_dir'], effect.source_ref,
            effect.before_sha, effect.payload['prebuilt_workspace'])
    except KeyError as error:
        raise KernelError('foreign_local_checkout') from error
    return _authenticate_prebuilt_binding(git, resource, run_id=effect.execution.run_id, require_checkout=True)


def authenticate_prebuilt_resource(git: LocalGit, resource: ResourceWitness, *,
        allow_dirty: bool = False) -> dict[str, Any] | None:
    """Authenticate an unfinished original binding carried by a Core resource.

    Core revokes this binding after workspace completion or later progress.
    A removed path is accepted only with no surviving worktree registration;
    its source ref is checked separately, so absence never proves ref cleanup.
    """
    if resource.prebuilt_workspace is None:
        return None
    return _authenticate_prebuilt_binding(git, resource, allow_dirty=allow_dirty)


def _authenticate_prebuilt_binding(git: LocalGit, resource: ResourceWitness, *,
        run_id: str | None = None, allow_dirty: bool = False, require_checkout: bool = False) -> dict[str, Any]:
    """One physical identity reader for workspace dispatch and partial cleanup."""
    try:
        value = resource.prebuilt_workspace
        keys = (_IDLE_HEADER_KEYS - {'resources'}) | _IDLE_ENTRY_KEYS
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError('shape')
        proof = dict(value)
        key = proof['source_record']
        if (resource.kind != 'development' or type(resource.generation) is not int or resource.generation != 1
                or type(allow_dirty) is not bool
                or proof['format'] != 'copied-idle-workspace/1' or type(proof['generation']) is not int
                or proof['generation'] != resource.generation
                or not isinstance(proof['run_id'], str) or not proof['run_id']
                or run_id is not None and proof['run_id'] != run_id
                or proof['subject'] != resource.subject or proof['disposition'] != 'new-unused'
                or proof['expected_oid'] is not None or proof['source_ref'] != resource.source_ref
                or not isinstance(resource.source_ref, str) or not resource.source_ref.startswith('refs/heads/')
                or not isinstance(resource.head_sha, str)
                or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', resource.head_sha) is None
                or proof['repository_root'] != str(git.root)
                or proof['common_git_dir'] != str(git.root / '.git')
                or any(proof[k] != getattr(resource, k) for k in ('checkout', 'owned_root', 'common_git_dir'))
                or not isinstance(key, dict) or set(key) != {'table', 'key'} or key['table'] != 'run_graph_members'
                or not isinstance(key['key'], list) or len(key['key']) != 3
                or key['key'][0] != proof['run_id'] or type(key['key'][2]) is not int
                or resource.subject != f"ticket:{key['key'][2]}"
                or not isinstance(proof['workspace_oid'], str) or re.fullmatch('[0-9a-f]{40,64}', proof['workspace_oid']) is None
                or not isinstance(proof['copy_uuid'], str) or re.fullmatch('[0-9a-f]{32}', proof['copy_uuid']) is None
                or not isinstance(proof['source_sha256'], str) or re.fullmatch('[0-9a-f]{64}', proof['source_sha256']) is None
                or (proof['before_head'], proof['checkout_status']) not in ((None, None), (proof['workspace_oid'] + '\n', []))):
            raise ValueError('identity')
        checkout, common = Path(proof['checkout']), Path(proof['common_git_dir'])
        if (checkout.parent != Path(proof['owned_root']) or checkout == git.root
                or common.is_symlink() or common.resolve() != common
                or git.root.resolve() != git.root
                or Path(git.run('rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()).resolve() != common):
            raise ValueError('paths')
        descriptor = os.open(common / 'agent-run-checkout-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as marker:
            physical = os.fstat(marker.fileno())
            if (not stat.S_ISREG(physical.st_mode) or physical.st_nlink != 1 or physical.st_size != 33
                    or marker.read(34) != proof['copy_uuid'].encode('ascii') + b'\n'):
                raise ValueError('marker')
        head, _ = _idle_checkout_state(git, checkout, common, allow_dirty=allow_dirty)
        allowed = {resource.head_sha + '\n'}
        if proof['before_head'] is not None:
            allowed.add(proof['before_head'])
        if (head is not None and head.decode('ascii') not in allowed
                or require_checkout and head is None and proof['before_head'] is not None):
            raise ValueError('head')
        refs = git.run('for-each-ref', '--format=%(refname) %(objectname)', resource.source_ref).stdout.splitlines()
        if refs and refs != [resource.source_ref + ' ' + resource.head_sha]:
            raise ValueError('ref')
        return proof
    except (OSError, UnicodeError) as error:
        raise KernelError('local_read_failed') from error
    except (ValueError, KeyError, TypeError, IndexError) as error:
        raise KernelError('foreign_local_checkout') from error
    except KernelError as error:
        if error.code == 'git_operation_failed':
            raise KernelError('local_read_failed') from error
        raise


def authenticate_copied_cleanup(storage: Storage, tx: Transaction, run: sqlite3.Row,
        job: sqlite3.Row, cleanup: sqlite3.Row) -> tuple[str, str] | None:
    """Read one installed private-copy witness; no original Task authority."""
    if job['kind'] != 'ticket':
        return None  # This witness never owns a Final/local Run branch.
    rows = tx.rows("SELECT id,subject FROM material WHERE purpose='v6_import_identity'", limit=2)
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('copied_resource_identity')
    imported = json.loads(tx.get(rows[0]['id'], rows[0]['subject']))
    proof = imported.get('local_resources')
    if proof is None:
        return None  # Older candidates have no witness; cleanup remains refused.
    keys = {'format', 'source_sha256', 'run_id', 'subject', 'generation', 'source_record',
        'repository_root', 'checkout', 'owned_root', 'common_git_dir', 'source_ref', 'expected_oid', 'initial_head', 'copy_uuid'}
    if (not isinstance(proof, dict) or set(proof) != keys or imported.get('source_mode') != 'copied'
            or imported.get('scope') != 'single-ticket' or proof['format'] != 'copied-returned-resource/1'
            or proof['source_sha256'] != imported['source_sha256'] or proof['run_id'] != run['id']
            or proof['subject'] != rows[0]['subject'] or proof['subject'] != job['id'] or job['kind'] != 'ticket'
            or proof['generation'] != 1 or job['generation'] != 1 or cleanup['generation'] != 1
            or cleanup['stage'] not in (0, 1) or proof['repository_root'] != run['repository_root']
            or any(proof[key] != job[key] or proof[key] != cleanup[key]
                   for key in ('source_ref', 'checkout', 'owned_root', 'common_git_dir'))
            or proof['common_git_dir'] != str(Path(run['repository_root']) / '.git')
            or Path(proof['checkout']).parent != Path(proof['owned_root'])
            or not Path(run['repository_root']).is_relative_to(storage.path.parent)
            or not Path(proof['owned_root']).is_relative_to(storage.path.parent)):
        raise ValueError('copied_resource_identity')
    records = V6RecordReader(storage)
    record: dict[str, Any] = {'table': 'work_jobs', 'key': [run['id'], f"ticket:{job['issue']}:generation:1"]}
    original = records.row(record['table'], record['key'])
    base = records.row('run_condition_base', (run['id'],))
    if (records.source['sha256'] != proof['source_sha256'] or records.source['run_id'] != run['id']
            or proof['source_record'] != record or original['owner_kind'] != 'ticket'
            or original['value_ticket_number'] != job['issue'] or original['value_ticket_branch'] != job['source_ref'].removeprefix('refs/heads/')
            or proof['expected_oid'] != base['value_sha']
            or proof['initial_head'] != (original['value_candidate_sha'] or base['value_sha'])):
        raise ValueError('copied_resource_owner')
    expected = hashlib.sha256(wire_json({key: value for key, value in proof.items() if key != 'copy_uuid'})).hexdigest()[:32]
    if proof['copy_uuid'] != expected:
        raise ValueError('copied_resource_marker')
    descriptor = os.open(Path(proof['common_git_dir']) / 'agent-run-checkout-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as marker:
        physical = os.fstat(marker.fileno())
        if (not stat.S_ISREG(physical.st_mode) or physical.st_nlink != 1 or physical.st_size != 33
                or marker.read(34) != expected.encode('ascii') + b'\n'):
            raise ValueError('copied_resource_marker')
    # The detached worktree advances natively. Its imported branch stays H0;
    # the normal local adapter still checks the exact Git identity and CAS.
    return cleanup['workspace_sha'] if cleanup['stage'] == 0 else proof['expected_oid'], expected


def _rows(db: sqlite3.Connection, sql: str, values: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = db.execute(sql, values)
    rows = cursor.fetchmany(4097)
    if len(rows) > 4096:
        raise ValueError('original_owner_limit')
    return [dict(zip((column[0] for column in cursor.description), row, strict=True)) for row in rows]


def _one(db: sqlite3.Connection, sql: str, values: tuple[Any, ...]) -> dict[str, Any]:
    rows = _rows(db, sql, values)
    if len(rows) != 1:
        raise ValueError('original_owner_identity')
    return rows[0]


@contextmanager
def _source(receipt: Mapping[str, str], opened: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    path = Path(receipt['source_db'])
    if path.resolve() != path or path != Path(receipt['target_db']).parent.parent / 'state.sqlite3':
        raise ValueError('original_owner_source')
    if opened is not None:
        if Path(opened.execute('PRAGMA database_list').fetchone()[2]) != path:
            raise ValueError('original_owner_source')
        yield opened  # Admission already holds its source read transaction.
        return
    before = source_identity(path)  # Refuse same-process inode handles before opening.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0, isolation_level=None)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        yield db
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('original_owner_source')


def _authenticate_completed(tx: Transaction, db: sqlite3.Connection, records: V6RecordReader,
        receipt: Mapping[str, str], run: Mapping[str, Any], root: Mapping[str, Any],
        job: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
    """Bind Completion and close evidence to the actual retired Single owner."""
    from .migration_completed import _completed_evidence, _external_close_proof
    from .kernel import Kernel

    run_id, number = receipt['run_id'], job['value_ticket_number']
    pending = run['status'] == 'waiting_external' and job['stage'] == 'merged'
    if (not pending and (run['status'] != 'ticket_completed' or job['stage'] != 'completed')
            or job['value_ticket_closed_by_run'] not in ((None, 0) if pending else (1,))
            or job['value_ticket_branch_initialized'] != 1):
        raise ValueError('original_completed_source')
    intent = _one(db, 'SELECT * FROM work_job_ticket_close_intent WHERE run_id=? AND job_id=?', (run_id, job['job_id']))
    if records.row('work_job_ticket_close_intent', [run_id, job['job_id']]) != intent:
        raise ValueError('original_completed_source')
    factory = db.row_factory
    try:
        db.row_factory = sqlite3.Row
        parent = _one(db, 'SELECT * FROM run_parent_versions WHERE run_id=? AND version_id=?',
            (run_id, root['value_parent_version_id']))
        title = _completed_evidence(db, run_id, parent['title_id'], owner='run:' + run_id, kind='parent.title')
        body = _completed_evidence(db, run_id, parent['body_id'], owner='run:' + run_id, kind='parent.body')
        graph = _one(db, 'SELECT evidence_id FROM run_graph_snapshots WHERE run_id=? AND snapshot_id=?',
            (run_id, root['value_graph_snapshot_id']))
        observation = _completed_evidence(db, run_id, graph['evidence_id'], owner='run:' + run_id, kind='graph.observation')
        order = [row[0] for row in db.execute('SELECT ticket_number FROM run_graph_scope_order '
            'WHERE run_id=? AND snapshot_id=? ORDER BY position', (run_id, root['value_graph_snapshot_id']))]
        if order != [number] or len(observation['tickets']) != 1:
            raise ValueError('original_completed_source')
        item = observation['tickets'][0]
        requirements = Requirements(ParentIssue(run['parent_number'], title, body, tuple(order), True),
            (Issue(number, item['title'], item['body'], item['state'], frozenset(item['labels']),
                tuple(Blocker(edge['number'], edge['state']) for edge in item['blocked_by'])),))
        runner = _completed_evidence(db, run_id, root['value_runner_github_identity'],
            owner='run:' + run_id, kind='runner_github_identity')
        close = _external_close_proof(evidence, run_id=run_id, repository=run['repository'], ticket=number,
            pr_number=job['value_pr_number'], publication_sha=job['value_publication_sha'],
            integrated_sha=job['value_integrated_sha'], base_sha=job['value_base_sha'],
            run_branch=root['value_run_branch'], ticket_branch=job['value_ticket_branch'],
            base_branch=_one(db, 'SELECT value_branch FROM run_condition_base WHERE run_id=?', (run_id,))['value_branch'],
            source_sha=receipt['source_sha256'], intent=intent, requirements=requirements, runner_identity=runner)
    finally:
        db.row_factory = factory
    native = tx.one('SELECT * FROM subjects WHERE id=?', (f'ticket:{number}',))
    completion = tx.one('SELECT * FROM completions WHERE subject=? AND generation=1', (native['id'],))
    if (native['generation'] != 1 or native['phase'] != 'complete'
            or any(completion[key] != value for key, value in {
                'source_sha': job['value_publication_sha'], 'merge_sha': job['value_integrated_sha'],
                'tree': job['value_integrated_tree'], 'pr_number': job['value_pr_number'],
                'revision': Kernel._fingerprint({'title': item['title'], 'body': item['body']}),
                'parent_revision': root['value_accepted_parent_spec_revision'],
                'graph_revision': root['value_accepted_ticket_graph_revision']}.items())):
        raise ValueError('original_completed_native')
    for field, role in (('development_attempt', 'development'), ('review_attempt', 'reviewer')):
        support = _one(db, 'SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?', (run_id, completion[field]))
        if (support['job_id'] != job['job_id'] or support['work_subject'] != native['id']
                or support['role'] != role or support['generation'] != 1
                or records.row('work_attempts', [run_id, completion[field]]) != support):
            raise ValueError('original_completed_native')
    for field, operation in (('comment_effect', 'completion_comment'), ('close_effect', 'close')):
        effect = tx.one('SELECT * FROM effects WHERE id=?', (completion[field],))
        receipt_body = json.loads(tx.get(effect['receipt_id'], native['id']))
        raw = receipt_body['raw']
        if (completion[field] != f'v6-import:{run_id}:{number}:{operation}'
                or effect['subject'] != native['id'] or effect['generation'] != 1
                or effect['operation'] != operation or effect['status'] != 'confirmed'
                or raw['event'] != close['event'] or raw['comment'] != close['comment']
                or any(raw['close_intent'].get(key) != value for key, value in close['close_intent'].items())):
            raise ValueError('original_completed_close')


def authenticate_run_ownership(tx: Transaction, body: Mapping[str, Any], receipt: Mapping[str, str],
        repository_root: Path, records: V6RecordReader, *, source: sqlite3.Connection | None = None) -> str:
    """Return authenticated H0 using one decoder and the unchanged retired source.

    Reuse admission's connection when available: opening/closing a second
    descriptor could release POSIX inode locks. No prepared-state checks or
    original business execution belongs here; native progress is permitted.
    """
    run_id = receipt['run_id']
    if (body.get('origin_nonce') != receipt['nonce'] or body.get('source_sha256') != receipt['source_sha256']
            or body.get('resource_mode') != 'existing-verified'
            or body.get('run_record') != {'table': 'run_root', 'key': [run_id]}):
        raise ValueError('original_owner_identity')
    with _source(receipt, source) as db:
        protocol = _one(db, 'SELECT value FROM schema_meta WHERE key=?', ('protocol',))['value']
        if read_source_fence(protocol) != dict(receipt):
            raise ValueError('original_owner_fence')
        run = _one(db, 'SELECT * FROM runs', ())
        task = TaskKey(repository_root, run['repository'], run['parent_number'])
        root = _one(db, 'SELECT * FROM run_root WHERE run_id=?', (run_id,))
        base = _one(db, 'SELECT * FROM run_condition_base WHERE run_id=?', (run_id,))
        if (run['run_id'] != run_id or task.fingerprint != receipt['task_fingerprint']
                or root['value_checkout_identity'] != receipt['source_uuid']
                or records.source.get('run_id') != run_id or records.source.get('sha256') != receipt['source_sha256']
                or records.row('run_root', [run_id]) != root
                or records.row('run_condition_base', [run_id]) != base):
            raise ValueError('original_owner_identity')
        graph = _one(db, 'SELECT version_id FROM run_graph_snapshots WHERE run_id=? AND snapshot_id=?',
                     (run_id, root['value_graph_snapshot_id']))
        members = {row['ticket_number'] for row in _rows(db,
            'SELECT ticket_number FROM run_graph_members WHERE run_id=? AND version_id=?', (run_id, graph['version_id']))}
        scope = body.get('source_scope', 'whole-run')
        if scope not in {'whole-run', 'single-ticket'} or scope == 'single-ticket' and len(members) != 1:
            raise ValueError('original_owner_scope')
        entries = body['subjects']
        if (not isinstance(entries, list) or len(entries) != len(members)
                or {entry['subject'] for entry in entries} != {f'ticket:{number}' for number in members}):
            raise ValueError('original_owner_scope')
        proofs = {}
        for entry in entries:
            if type(entry['generation']) is not int or entry['generation'] != 1:
                raise ValueError('original_owner_generation')
            proof = json.loads(tx.get(entry['proof_id'], entry['subject']))
            if (proof['origin_nonce'] != receipt['nonce'] or proof['source_sha256'] != receipt['source_sha256']
                    or proof['subject'] != entry['subject'] or type(proof['generation']) is not int or proof['generation'] != 1):
                raise ValueError('original_owner_identity')
            proofs[entry['subject']] = proof
        jobs = {row['job_id']: row for row in _rows(db, 'SELECT * FROM work_jobs WHERE run_id=?', (run_id,))}
        slots = _rows(db, 'SELECT container,slot,job_id FROM work_job_slots WHERE run_id=?', (run_id,))
        current: set[str] = set()
        completed = {}
        seen: set[int] = set()

        def authenticate_worked_owner(job: Mapping[str, Any], number: int) -> None:
            proof = proofs[f'ticket:{number}']
            if (job['job_id'] != f'ticket:{number}:generation:1'
                    or job['owner_kind'] != 'ticket' or job['owner_key'] != str(number)
                    or job['value_ticket_number'] != number
                    or type(job['value_ticket_branch_generation']) is not int or job['value_ticket_branch_generation'] != 1
                    or records.row('work_jobs', [run_id, job['job_id']]) != job
                    or proof['source_record'] != {'table': 'work_jobs', 'key': [run_id, job['job_id']]}
                    or proof['kind'] != ('completed' if job['stage'] == 'completed'
                        or scope == 'single-ticket' and job['stage'] == 'merged' else 'returned')):
                raise ValueError('original_owner_generation')
            current.add(job['job_id'])
            if proof['kind'] == 'completed':
                completed[job['job_id']] = job['value_integrated_sha']

        for slot in slots:
            job = jobs[slot['job_id']]
            number = job['value_ticket_number']
            if (type(number) is not int or number not in members or number in seen
                    or slot['container'] != 'ticket_jobs' or slot['slot'] != str(number)
                    or job['owner_kind'] != 'ticket' or job['owner_key'] != str(number)
                    or job['job_id'] not in {f'ticket:{number}:selection', f'ticket:{number}:generation:1'}
                    or records.row('work_jobs', [run_id, job['job_id']]) != job):
                raise ValueError('original_owner_identity')
            seen.add(number)
            proof = proofs[f'ticket:{number}']
            if job['job_id'].endswith(':selection'):
                if job['stage'] is not None or scope == 'whole-run' and proof['kind'] != 'unallocated':
                    raise ValueError('original_owner_generation')
                continue
            authenticate_worked_owner(job, number)
        if scope == 'single-ticket':
            active = _one(db, 'SELECT active_job_id FROM work_job_layout WHERE run_id=?', (run_id,))
            number = next(iter(members))
            if active['active_job_id'] not in jobs or proofs[f'ticket:{number}']['kind'] not in {'returned', 'completed'}:
                raise ValueError('original_owner_generation')
            authenticate_worked_owner(jobs[active['active_job_id']], number)
        if (current != {key for key, job in jobs.items() if job['stage'] is not None}
                or scope == 'whole-run' and any(proofs[f'ticket:{number}']['kind'] != 'unallocated' for number in members - seen)):
            raise ValueError('original_owner_scope')

        external = tx.one("SELECT h.subject,h.identity,h.material_id,m.purpose FROM history h "
                          "JOIN material m ON m.id=h.material_id WHERE h.kind='v6_external_readproof'")
        raw = tx.get(external['material_id'], external['subject'])
        evidence = json.loads(raw)
        single_completed = scope == 'single-ticket' and bool(completed)
        ref = 'refs/heads/' + root['value_run_branch']
        head = evidence.get('remote_heads', {}).get(ref)
        if (external['purpose'] != 'v6_external_readproof' or external['identity'] != body['external_readproof_sha256']
                or hashlib.sha256(raw).hexdigest() != external['identity']
                or evidence.get('source_sha256') != receipt['source_sha256']
                or evidence.get('source_kind') != (None if single_completed else
                    'returned_role' if scope == 'single-ticket' else 'original_run')
                or not isinstance(head, str) or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', head) is None
                or head not in {base['value_sha'], *completed.values()}):
            raise ValueError('original_owner_head')
        if single_completed:
            _authenticate_completed(tx, db, records, receipt, run, root, jobs[next(iter(completed))], evidence)
        git = LocalGit(repository_root)
        if any(not isinstance(sha, str) or sha != head and
               git.run('merge-base', '--is-ancestor', sha, head, check=False).returncode != 0
               for sha in completed.values()):
            raise ValueError('original_owner_completed_set')
        witnesses = [{'table': 'work_jobs', 'key': [run_id, key]} for key, sha in completed.items() if sha == head]
        if not witnesses:
            witnesses = [{'table': 'run_condition_base', 'key': [run_id]}]
        ownership = body['run_ref_ownership']
        if (not isinstance(ownership, dict) or set(ownership) != {'source_ref', 'expected_oid', 'source_record', 'head_evidence'}
                or ownership['source_ref'] != ref or ownership['expected_oid'] != head
                or ownership['source_record'] != {'table': 'run_root', 'key': [run_id]}
                or not isinstance(ownership['head_evidence'], list)
                or sorted(ownership['head_evidence'], key=wire_json) != sorted(witnesses, key=wire_json)):
            raise ValueError('original_owner_head')
        return head


def authenticate_completed_cleanup(tx: Transaction, imported: sqlite3.Row, body: Mapping[str, Any],
        receipt: Mapping[str, str], job: sqlite3.Row, cleanup: sqlite3.Row) -> str | None:
    """Read the certified clean Completion contract; returned still needs archives."""
    scope = body.get('source_scope', 'whole-run')
    if scope not in {'single-ticket', 'whole-run'}:
        raise ValueError('original_completed_scope')
    if scope != 'single-ticket':
        return None
    entries = body.get('subjects')
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError('original_completed_scope')
    capsule = json.loads(tx.get(entries[0]['proof_id'], entries[0]['subject']))
    if not isinstance(capsule, dict):
        raise ValueError('original_completed_scope')
    if capsule.get('kind') != 'completed':
        return None
    if job['kind'] == 'final_run' and job['id'] != entries[0]['subject']:
        # This source certificate covers the imported Ticket. The later
        # native Final must use the caller's separate, full Run-ref proof.
        return None
    from .migration_run_admission import authenticate_run_transfer

    managed = ManagedWorkspace.for_repository(tx.one('SELECT repository FROM runs')[0])
    with _source(receipt, None) as db:
        authenticate_run_transfer(tx, imported, body, receipt, managed, db)
        original = _one(db, 'SELECT j.* FROM work_jobs j JOIN work_job_layout l '
            'ON l.run_id=j.run_id AND l.active_job_id=j.job_id WHERE j.run_id=?', (receipt['run_id'],))
        head = original['value_publication_sha']
        checkout = Path(receipt['target_db']).parent.parent / 'worktrees' / receipt['run_id'] / f"ticket-{original['value_ticket_number']}"
        if (original['stage'] != 'merged' or job['id'] != entries[0]['subject'] or job['kind'] != 'ticket'
                or job['generation'] != cleanup['generation'] or job['generation'] != 1
                or cleanup['stage'] not in (0, 1) or job['publication_sha'] != head
                or cleanup['source_ref'] != 'refs/heads/' + original['value_ticket_branch']
                or any(cleanup[key] != head for key in ('source_sha', 'workspace_sha'))
                or any(cleanup[key] != job[key] for key in ('checkout', 'owned_root', 'common_git_dir'))
                or cleanup['checkout'] != str(checkout) or cleanup['owned_root'] != str(checkout.parent)
                or cleanup['common_git_dir'] != str(managed.repository_root / '.git')):
            raise ValueError('original_completed_resource')
        return str(head)
