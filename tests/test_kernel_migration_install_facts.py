"""Canonical consumer tests; decoder equivalence remains a separate gate."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import hashlib
import io
import json
import os
import sys
import tarfile

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, ParentIssue, Requirements, RunSpec, Issue
from agent_run_kernel.attempt_continuation import AttemptContinuationBasis, ContinuationOpening
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_facts import (
    ImportedAttempt, ImportedResource, ImportedRunFacts, ImportedSubject, ImportSource, SourceRecord,
)
from agent_run_kernel.migration_records import V6RecordReader
from agent_run_kernel.migration_run_install import install_imported_facts
from agent_run_kernel.migration_run_projection import verify_imported_installation
from agent_run_kernel.model import Purpose, Role, wire_json
from agent_run_kernel.model import KernelError
from agent_run_kernel.migration_run_admission import authenticate_run_transfer
from agent_run_kernel.offline import fixture_inputs
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.model import Profile
from support.frozen_v6_runtime import old_v6_runtime
from support.v6_returned_source import returned_managed_source, continue_original_returned
from agent_run_kernel.migration_active import read_original_returned_import
from agent_run_kernel.migration_cli import _collect_original_v6
from test_kernel_migration_original_source import _git_bytes
from agent_run_kernel.migration_run_cutover import _execute_original_import, _import_origin
from agent_run_kernel.migration_run_resources import authenticate_imported_resources
from agent_run_kernel.task_location import read_task, task_path
from agent_run_kernel.legacy_admission import read_source_fence
from agent_run_kernel.migration_completed import read_original_completed_import
from test_kernel_migration_completed_history import _completed_source, _assert_original_records
from agent_run.managed_workspace import ManagedWorkspace
from support.v6_whole_run_source import whole_run_managed_source
from agent_run_kernel.migration_run import read_original_run_import
from agent_run_kernel.operation_runner import DeliveryFacts
from agent_run_kernel.local_effects import LocalEffects
from support.core_operations import ReadOnlyPorts


@pytest.fixture(scope='module')
def old_source(tmp_path_factory):
    return old_v6_runtime(tmp_path_factory.mktemp('canonical-install-old-source'))


@pytest.fixture(scope='module')
def prior_installer(tmp_path_factory):
    """Keep the last accepted installer independent after its live writer exits."""
    directory = tmp_path_factory.mktemp('migration-installer-259acd')
    repository = Path(__file__).resolve().parents[2]
    archive = subprocess.run(['git', '-C', str(repository), 'archive',
        '259acd4f94b6ce15f3a58810dbc9ee659807b880', 'project/src'],
        check=True, capture_output=True, timeout=30).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            target = directory / member.name
            assert target.resolve().is_relative_to(directory.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile()
                stream = bundle.extractfile(member)
                assert stream is not None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
    return directory / 'project/src'


@pytest.fixture
def facts(tmp_path, git_repo, old_source):
    issue = {'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
             'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}
    root = git_repo / '.agent-run'
    fixture = old_source('fixture', root=root, fixture_path=git_repo / 'github.json', issues={'2': issue})
    state = old_source('start', root=root, git=git_repo, fixture=fixture)['state']
    snapshot = capture_v6(root / 'state.sqlite3', tmp_path / 'sealed.sqlite3')
    base_sha = state['base']['sha']
    tree = subprocess.check_output(['git', 'rev-parse', base_sha + '^{tree}'], cwd=git_repo, text=True).strip()
    base = Commit(base_sha, tree)
    requirements = Requirements(ParentIssue(1, 'Parent spec', 'Deliver the ticket set.', (2,)),
        (Issue(2, issue['title'], issue['body'], 'OPEN', frozenset(issue['labels'])),))
    run_id = state['run_id']
    parent_revision = Kernel._fingerprint({'title': requirements.parent.title, 'body': requirements.parent.body})
    graph_revision = Kernel._fingerprint({'tickets': [2], 'dependencies': {'2': []}})
    owned = tmp_path / 'native-worktrees'
    source_ref, run_ref = 'refs/heads/canonical-ticket-2', 'refs/heads/' + state['run_branch']
    spec = RunSpec(run_id, 'example/project', 1, parent_revision, graph_revision, (
        Delivery('ticket:2', DeliveryKind.TICKET, 2, 'source-verified', base, source_ref, run_ref,
            checkout=str(owned / 'ticket-2'), owned_root=str(owned), common_git_dir=str(git_repo / '.git')),
        Delivery('final:' + run_id, DeliveryKind.FINAL_RUN, 1, parent_revision, base,
            run_ref, 'refs/heads/main', checkout=str(owned / 'final'), owned_root=str(owned),
            common_git_dir=str(git_repo / '.git'))), language='zh', repository_root=str(git_repo), inputs=fixture_inputs())
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        job_id = db.execute('SELECT job_id FROM work_jobs').fetchone()[0]
    record = SourceRecord('work_jobs', (run_id, job_id))
    resource = ImportedResource(record, 'ticket-workspace', 'new-unused', source_ref, None,
        owned / 'ticket-2', None, owned, git_repo / '.git', None, None, None, (), False)
    subject = ImportedSubject('ticket:2', 2, 1, record, (), 'unallocated', None, (), (), None, None, None, None, (resource,))
    run_resource = ImportedResource(SourceRecord('run_root', (run_id,)), 'run-ref', 'existing', run_ref, base.sha,
        None, None, None, git_repo / '.git', None, None, None, (SourceRecord('run_condition_base', (run_id,)),), False)
    # The captured producer supplies authentic records. The explicit native
    # policy/layout values here are synthetic consumer inputs, not a decoder
    # result or evidence of source admission.
    return ImportedRunFacts(ImportSource(snapshot, 'copied', 'selected-graph', None, None, None, None, ()),
        spec, requirements, requirements, 100.0, (subject,), run_resource, ())


def test_single_install_and_independent_prepared_leave_source_and_final_work_untouched(facts, tmp_path, monkeypatch):
    original = facts.source.snapshot.source.read_bytes(), facts.source.snapshot.path.read_bytes()
    path = tmp_path / 'native.sqlite3'
    native = install_imported_facts(facts, path)
    assert native.status('ticket:2')['phase'] == 'workspace'
    assert native.status('ticket:2')['pending_attempt'] is None
    with native.storage.transaction(write=False) as tx:
        assert [row['id'] for row in tx.rows('SELECT id FROM subjects')] == ['ticket:2']
        assert tx.one('SELECT count(*) FROM final_target')[0] == 1
        assert tx.one('SELECT count(*) FROM final_barriers')[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM threads')[0] == 0
    assert V6RecordReader(native).source['sha256'] == facts.source.snapshot.sha256

    def forbidden(*args, **kwargs):
        raise AssertionError('Expected projection called the writer')

    monkeypatch.setattr('agent_run_kernel.migration_run_install.install_imported_facts', forbidden)
    before = path.read_bytes()
    verify_imported_installation(native, facts)
    assert path.read_bytes() == before
    assert (facts.source.snapshot.source.read_bytes(), facts.source.snapshot.path.read_bytes()) == original


@pytest.mark.parametrize('family', ['human', 'uncertified-basis'])
def test_install_does_not_enable_unproved_human_or_continuation(facts, tmp_path, family):
    raw = wire_json({'result_kind': 'human_blocker', 'summary': None, 'human_blockers': ['Need a decision.']}
        if family == 'human' else {'result_kind': 'development', 'summary': 'Retained.', 'human_blockers': None})
    call = ImportedAttempt(SourceRecord('work_attempts', (facts.spec.run_id, 'unadmitted-attempt')),
        Role.DEVELOPMENT, 1, 1, Purpose.REQUIREMENTS, 1, True, False, None,
        SourceRecord('work_evidence', (facts.spec.run_id, 'unadmitted-raw')), raw, raw, None, b'{}', b'{}', None, None,
        None if family == 'human' else AttemptContinuationBasis('unproved-thread', 'unproved-context', ContinuationOpening()))
    subject = replace(facts.subjects[0], kind='returned', historical_base=facts.spec.deliveries[0].base, attempts=(call,))
    target = tmp_path / 'refused.sqlite3'
    before = facts.source.snapshot.source.read_bytes()
    expected = 'unsupported_source_human_return' if family == 'human' else 'unsupported_source_continuation_basis'
    with pytest.raises(MigrationRefused, match=expected):
        install_imported_facts(replace(facts, subjects=(subject,)), target)
    assert not target.exists()
    assert facts.source.snapshot.source.read_bytes() == before


def test_failed_prepared_comparison_never_publishes_target(facts, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise MigrationRefused('controlled_prepared_failure')

    monkeypatch.setattr('agent_run_kernel.migration_run_projection.verify_imported_installation', fail)
    target = tmp_path / 'must-not-publish.sqlite3'
    before = facts.source.snapshot.source.read_bytes()
    with pytest.raises(MigrationRefused, match='controlled_prepared_failure'):
        install_imported_facts(facts, target)
    assert not target.exists()
    assert facts.source.snapshot.source.read_bytes() == before


def test_install_rechecks_real_source_journal_created_after_decoding(facts, tmp_path):
    root = facts.source.snapshot.source.parent / 'notifications'
    root.mkdir()
    journal = root / (hashlib.sha256(facts.spec.run_id.encode()).hexdigest()[:32] + '.json')
    journal.write_text('{"seen":[]}', encoding='utf-8')
    target = tmp_path / 'must-not-import-journal.sqlite3'
    before = facts.source.snapshot.source.read_bytes()
    with pytest.raises(MigrationRefused, match='^unsupported_source_notification_journal$'):
        install_imported_facts(facts, target)
    assert not target.exists()
    assert journal.read_text() == '{"seen":[]}'
    assert facts.source.snapshot.source.read_bytes() == before


@pytest.mark.parametrize('numbers', [(2,), (2, 3)])
def test_real_idle_decoder_installs_and_advances_same_work_as_legacy(
    tmp_path, git_repo, old_source, prior_installer, monkeypatch, numbers,
):
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    root = git_repo / '.agent-run'
    fixture = old_source('fixture', root=root, fixture_path=git_repo / 'github.json', issues={str(n): {
        'number': n, 'title': f'Ticket {n}', 'body': f'Deliver {n}.', 'state': 'OPEN',
        'labels': ['ready-for-agent'], 'blocked_by': [{'number': 2, 'state': 'OPEN'}] if n == 3 else []} for n in numbers})
    state = old_source('start', root=root, git=git_repo, fixture=fixture)['state']
    snapshot = capture_v6(root / 'state.sqlite3', tmp_path / 'sealed.sqlite3')
    git = LocalGit(git_repo)
    work = tmp_path / 'work'
    work.mkdir()
    workspaces = {}
    for number in numbers:
        checkout = work / f'ticket-{number}'
        git.run('worktree', 'add', '--detach', str(checkout), state['base']['sha'])
        workspaces[number] = (f'refs/heads/canonical/{number}', checkout)
    options = dict(repository_root=git_repo, source_ref=workspaces[2][0], checkout=workspaces[2][1],
        final_checkout=work / 'final', development_profile=Profile('explicit-development', 'high'),
        review_profile=Profile('explicit-review', 'medium'), publication_profile=Profile('explicit-publication', 'low'),
        additional_workspaces={n: value for n, value in workspaces.items() if n != 2})
    before = snapshot.source.read_bytes(), snapshot.path.read_bytes(), git.run('show-ref').stdout, git.run('worktree', 'list', '--porcelain').stdout
    decoded = read_initial_import(snapshot, **options)
    canonical = install_imported_facts(decoded, tmp_path / 'canonical.sqlite3')
    script = '''
import json, sys
from dataclasses import asdict
from pathlib import Path
from agent_run_kernel.migration import SourceSnapshot
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.model import Profile, wire_json
import agent_run_kernel.migration_conversion as module
request = json.load(sys.stdin)
assert Path(module.__file__).resolve().is_relative_to(Path(request['oracle']))
snapshot = SourceSnapshot(**{**request['snapshot'], 'source': Path(request['snapshot']['source']),
    'path': Path(request['snapshot']['path']), 'table_rows': tuple(map(tuple, request['snapshot']['table_rows']))})
options = request['options']
for name in ('repository_root', 'checkout', 'final_checkout'):
    options[name] = Path(options[name])
for name in ('development_profile', 'review_profile', 'publication_profile'):
    options[name] = Profile(**options[name])
options['additional_workspaces'] = {int(number): (ref, Path(path))
    for number, (ref, path) in options['additional_workspaces'].items()}
legacy = convert_initial_v6(snapshot, Path(request['target']), **options)
print(wire_json({'states': {str(n): legacy.status(f'ticket:{n}') for n in request['numbers']},
    'deliveries': {str(n): asdict(legacy.delivery(f'ticket:{n}')) for n in request['numbers']},
    'source_status': legacy.source_status()}).decode())
'''
    request = {'oracle': str(prior_installer), 'target': str(tmp_path / 'legacy.sqlite3'),
        'numbers': numbers, 'snapshot': {**vars(snapshot), 'source': str(snapshot.source), 'path': str(snapshot.path)},
        'options': {**{key: str(value) if isinstance(value, Path) else value for key, value in options.items()},
            **{key: vars(value) for key, value in options.items() if isinstance(value, Profile)},
            'additional_workspaces': {str(n): (ref, str(path)) for n, (ref, path) in options['additional_workspaces'].items()}}}
    completed = subprocess.run([sys.executable, '-c', script], input=json.dumps(request),
        env={**os.environ, 'PYTHONPATH': str(prior_installer), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    legacy = json.loads(completed.stdout)
    for number in numbers:
        subject = f'ticket:{number}'
        expected, actual = legacy['states'][str(number)], canonical.status(subject)
        for key in ('phase', 'purpose', 'generation', 'budgets', 'pending_attempt', 'pending_write'):
            assert actual[key] == expected[key], key
        from dataclasses import asdict
        assert json.loads(wire_json(asdict(canonical.delivery(subject)))) == legacy['deliveries'][str(number)]
    assert json.loads(wire_json(canonical.source_status())) == legacy['source_status']
    if 3 in numbers:
        assert canonical.source_status()['remaining'] == {'ticket:3': 'blocked_by_incomplete_tickets'}
    verify_imported_installation(canonical, decoded)
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes(), git.run('show-ref').stdout,
        git.run('worktree', 'list', '--porcelain').stdout) == before
    assert not (work / 'final').exists()
    with canonical.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM subjects')[0] == len(numbers)
        assert tx.one('SELECT count(*) FROM final_barriers')[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    delivery = canonical.delivery('ticket:2')
    ports = ReadOnlyPorts(decoded.observed_requirements, delivery.source_ref, delivery.target_ref,
        DeliveryFacts(git.commit(delivery.target_ref), None))
    monkeypatch.setattr(ports, 'inspect', LocalEffects(git, owned_root=work).inspect_resources)
    effects = []
    class WorkspaceBoundary(Exception):
        pass
    def workspace(effect, authorize):
        authorize()
        effects.append(effect)
        raise WorkspaceBoundary
    monkeypatch.setattr(ports, 'execute', workspace)
    runner = canonical.operation_runner(canonical.claim_executor('canonical-idle'), lambda binding: ports)
    # The public runner reaches the actual authorized physical boundary. This
    # test still stops before any workspace write, as the old returned Effect did.
    with pytest.raises(WorkspaceBoundary):
        runner.run_once(agent_capacity=0)
    assert len(effects) == 1 and effects[0].operation == 'workspace'
    assert effects[0].source_ref == workspaces[2][0]
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes(), git.run('show-ref').stdout,
        git.run('worktree', 'list', '--porcelain').stdout) == before


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
@pytest.mark.parametrize('transfer_intent', [False, True])
def test_original_returned_decoder_reaches_canonical_prepared_without_retiring_source(tmp_path, git_repo, monkeypatch, role, transfer_intent):
    import os
    import shutil
    managed, paths = returned_managed_source(tmp_path, git_repo, role)
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    decoded = read_original_returned_import(snapshot, profile_snapshot=evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json', checkout_archive=evidence / 'checkout.tar')
    origin = _import_origin(decoded, managed, 'ad' * 16) if transfer_intent else None
    if origin is not None:
        authenticate_imported_resources(decoded, origin, evidence)
    native = install_imported_facts(decoded, tmp_path / 'canonical-returned.sqlite3', origin=origin)
    verify_imported_installation(native, decoded, origin=origin)
    state = native.status('ticket:2')
    assert state['phase'] == {'development': 'development', 'review': 'review', 'publication': 'publish'}[role]
    assert state['pending_return'] is True
    assert state['budgets']['requirements']['developments_used'] == 1
    assert state['budgets']['requirements']['reviews_used'] == int(role == 'publication')
    assert state['pending_attempt'] == decoded.subjects[0].attempts[-1].record.key[-1]
    with native.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM subjects')[0] == 1
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
        assert all(row['thread_id'] is None and row['opening_id'] is None for row in tx.rows('SELECT thread_id,opening_id FROM attempts'))
        if role == 'development':
            assert tx.one("SELECT count(*) FROM material WHERE purpose='v6_candidate_commit_intent'")[0] == 1
    assert (paths['database'].read_bytes(), _git_bytes(managed.repository_root)) == before
    assert not (managed.state_root / decoded.spec.run_id / 'kernel.sqlite3').exists()


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
@pytest.mark.parametrize('enter_native', [False, True], ids=['inverse', 'native'])
def test_canonical_original_executor_transfers_reenters_and_restores_real_returned_source(
    tmp_path, git_repo, monkeypatch, role, enter_native,
):
    import os
    import shutil
    managed, paths = returned_managed_source(tmp_path, git_repo, role)
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    decoded = read_original_returned_import(snapshot, profile_snapshot=evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json', checkout_archive=evidence / 'checkout.tar')
    task = decoded.source.task
    assert task is not None
    with closing(sqlite3.connect(snapshot.source)) as db:
        original_protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    resource = decoded.subjects[0].resources[0]
    assert resource.checkout is not None
    git = LocalGit(managed.repository_root)
    original_git = _git_bytes(managed.repository_root)
    head_log = Path(git.run('rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD', cwd=resource.checkout).stdout.strip())
    original_log = head_log.read_bytes()
    nonce = 'ac' * 16
    native = _execute_original_import(decoded, nonce=nonce, evidence=evidence)
    assert isinstance(native, Kernel)
    assert read_task(task_path(task)) == (decoded.spec.run_id, native.storage.path)
    assert git.run('symbolic-ref', '-q', 'HEAD', cwd=resource.checkout, check=False).returncode == 1
    with closing(sqlite3.connect(snapshot.source)) as db:
        protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    assert read_source_fence(protocol) == _import_origin(decoded, managed, nonce)
    before = native.storage.path.read_bytes(), _git_bytes(managed.repository_root)
    again = _execute_original_import(decoded, nonce=nonce, evidence=evidence)
    assert isinstance(again, Kernel) and again.storage.path == native.storage.path
    assert (native.storage.path.read_bytes(), _git_bytes(managed.repository_root)) == before
    if enter_native:
        state = native.status('ticket:2')
        # Exercise rejected proof inputs in memory, without rewriting either
        # database or its immutable materials/source rows.
        checked_bytes = snapshot.source.read_bytes(), native.storage.path.read_bytes()
        with native.storage.transaction(write=False) as tx, closing(sqlite3.connect(
                snapshot.source.as_uri() + '?mode=ro', uri=True)) as source:
            imported = tx.one("SELECT h.subject,h.kind,h.identity,h.material_id,h.generation,m.purpose "
                "FROM history h JOIN material m ON m.id=h.material_id WHERE h.kind='v6_run_import'")
            body = json.loads(tx.get(imported['material_id'], imported['subject']))
            origin = _import_origin(decoded, managed, nonce)
            for field, value in (('source_scope', 'whole-run'), ('source_scope', 'unknown'),
                    ('origin_nonce', '00' * 16), ('source_sha256', '00' * 32),
                    ('profile_sha256', False), ('run_record', {'table': 'run_root', 'key': ['other-run']})):
                with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
                    authenticate_run_transfer(tx, imported, {**body, field: value}, origin, managed, source)
            for field, value in (('expected_oid', '0' * 40),
                    ('head_evidence', [{'table': 'work_jobs', 'key': [decoded.spec.run_id, 'other-owner']}] )):
                with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
                    authenticate_run_transfer(tx, imported,
                        {**body, 'run_ref_ownership': {**body['run_ref_ownership'], field: value}}, origin, managed, source)
            authenticate_run_transfer(tx, imported, body, origin, managed, source)
        assert (snapshot.source.read_bytes(), native.storage.path.read_bytes()) == checked_bytes
        execution = native.claim_executor('canonical-returned-native')
        delivery = native.delivery('ticket:2')
        ports = ReadOnlyPorts(decoded.observed_requirements, delivery.source_ref, delivery.target_ref,
            DeliveryFacts(git.commit(delivery.target_ref), git.commit(resource.source_ref)))
        runner = native.operation_runner(execution, lambda binding: ports)
        step = runner.run_once(agent_capacity=0)
        assert step.kind == 'progress'
        with native.storage.transaction(write=False) as tx:
            pending = decoded.subjects[0].attempts[-1].record.key[-1]
            assert tx.one('SELECT status FROM attempts WHERE id=?', (pending,))[0] == 'consumed'
            assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        expected_budgets = json.loads(json.dumps(state['budgets']))
        expected_budgets['requirements']['reviews_used'] = int(role != 'development')
        assert native.status('ticket:2')['budgets'] == expected_budgets
        return

    result = _execute_original_import(decoded, nonce=nonce, evidence=evidence, inverse=True)
    assert isinstance(result, dict) and result['status'] == 'rolled_back'
    assert read_task(task_path(task)) is None
    restored_git = _git_bytes(managed.repository_root)
    log_key = str(head_log.relative_to(managed.repository_root))
    assert {key: value for key, value in restored_git.items() if key != log_key} == {
        key: value for key, value in original_git.items() if key != log_key}
    assert head_log.read_bytes().startswith(original_log)
    assert git.run('symbolic-ref', 'HEAD', cwd=resource.checkout).stdout.strip() == resource.source_ref
    with closing(sqlite3.connect(snapshot.source)) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == original_protocol
    assert _execute_original_import(decoded, nonce=nonce, evidence=evidence, inverse=True) == result
    with native.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_original_rollback_started'")[0] == 1
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_original_rollback_complete'")[0] == 1
    with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
        native.claim_executor('stale-native-after-canonical-inverse')
    resumed = continue_original_returned(managed, paths, role)
    assert resumed['calls'] == {'development': ['review', 'publication'],
        'review': ['publication'], 'publication': []}[role]
    assert resumed['budget'] == [1, 1] and resumed['state'] == 'CLOSED'
    candidate = decoded.subjects[0].candidate
    if candidate is not None:
        assert resumed['candidate_tree'] == candidate.tree


def _completed_import_source(tmp_path, git_repo, monkeypatch, profile_backend):
    import shutil
    managed = ManagedWorkspace.for_repository('example/project')
    managed.root.mkdir(parents=True)
    git_repo.rename(managed.repository_root)
    managed.state_root.mkdir()
    LocalGit(managed.repository_root).run('remote', 'add', 'origin', 'https://github.com/example/project.git')
    (managed.root / '.agent-run-workspace.json').write_text(json.dumps({
        'repository': managed.repository, 'remote_url': 'https://github.com/example/project.git'}))
    paths = _completed_source(tmp_path, managed.repository_root, 2, state_root=managed.state_root,
        controlled=True, actual_github_close=True, profile_backend=profile_backend)
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    facts = read_original_completed_import(snapshot, profile_snapshot=evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json')
    return managed, paths, snapshot, facts, before, evidence


@pytest.mark.parametrize('profile_backend', [False, True])
def test_canonical_completed_install_keeps_cleanup_and_optional_history(
    tmp_path, git_repo, monkeypatch, profile_backend,
):
    managed, paths, snapshot, facts, before, evidence = _completed_import_source(
        tmp_path, git_repo, monkeypatch, profile_backend)
    native = install_imported_facts(facts, tmp_path / 'canonical-completed.sqlite3')
    verify_imported_installation(native, facts)
    assert native.status('ticket:2')['phase'] == 'complete'
    assert native.status('ticket:2')['pending_attempt'] is None
    assert native.status('ticket:2')['budgets']['requirements']['developments_used'] == 2
    assert native.status('ticket:2')['budgets']['requirements']['reviews_used'] == 2
    with native.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM subjects')[0] == 1
        assert tx.one('SELECT count(*) FROM completions')[0] == 1
        assert tx.one("SELECT count(*) FROM effects WHERE status='confirmed'")[0] == 2
        assert tx.one("SELECT count(*) FROM resource_cleanup WHERE state='pending'")[0] == 1
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        # Publication reuses Development's real binding; no third Thread.
        assert tx.one('SELECT count(*) FROM threads')[0] == (2 if profile_backend else 0)
        assert all(row['thread_id'] is None and row['opening_id'] is None for row in tx.rows('SELECT thread_id,opening_id FROM attempts'))
    _assert_original_records(snapshot, V6RecordReader(native))
    assert (paths['database'].read_bytes(), _git_bytes(managed.repository_root)) == before
    assert not (managed.state_root / facts.spec.run_id / 'kernel.sqlite3').exists()


@pytest.mark.parametrize('second_returned', [False, True])
def test_canonical_whole_install_preserves_real_owner_and_resource_union(
    tmp_path, git_repo, monkeypatch, second_returned,
):
    import shutil
    managed, paths = whole_run_managed_source(tmp_path, git_repo, second_returned=second_returned)
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    facts = read_original_run_import(snapshot, evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json', checkouts_snapshot=evidence / 'checkouts.json')
    origin = _import_origin(facts, managed, 'bc' * 16)
    authenticate_imported_resources(facts, origin, evidence)
    native = install_imported_facts(facts, tmp_path / 'canonical-whole.sqlite3', origin=origin)
    verify_imported_installation(native, facts, origin=origin)
    assert native.status('ticket:2')['phase'] == 'complete'
    assert native.status('ticket:3')['phase'] == 'review'
    assert native.status('ticket:4')['phase'] == ('review' if second_returned else 'workspace')
    with native.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM subjects')[0] == 3
        assert tx.one('SELECT count(*) FROM completions')[0] == 1
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM resource_cleanup')[0] == 0
        proof = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_run_import'")
        body = json.loads(tx.get(proof['material_id'], proof['subject']))
        assert body['run_ref_ownership']['expected_oid'] == facts.subjects[0].completion.integrated.sha
    _assert_original_records(snapshot, V6RecordReader(native))
    assert (paths['database'].read_bytes(), _git_bytes(managed.repository_root)) == before


@pytest.mark.parametrize('profile_backend', [False, True])
@pytest.mark.parametrize('mode', ['inverse', 'native'])
def test_completed_clean_transfer_consumes_only_certified_publication_resource(
    tmp_path, git_repo, monkeypatch, profile_backend, mode,
):
    from agent_run_kernel.local_effects import LocalEffects
    from agent_run_kernel.migration_cutover import migrate_completed_original_v6, rollback_original_v6
    from collections.abc import Callable
    from agent_run_kernel.model import Effect, EffectResult, ResourceInspection, ResourceWitness
    from agent_run_kernel.operation_runner import DeliveryFacts, OperationBinding
    from support.core_operations import CoreOperations, ReadOnlyPorts
    managed, paths, snapshot, facts, before, evidence = _completed_import_source(
        tmp_path, git_repo, monkeypatch, profile_backend)
    with closing(sqlite3.connect(paths['database'].as_uri() + '?mode=ro', uri=True)) as source:
        assert source.execute('SELECT count(*) FROM work_invocations').fetchone()[0] == (5 if profile_backend else 0)
    nonce = 'ce' * 16
    options = dict(repository='example/project', parent_number=1, nonce=nonce,
        external_snapshot=evidence / 'external-readproof.json')
    native = migrate_completed_original_v6(snapshot, profile_snapshot=evidence / 'profile.json', **options)
    assert isinstance(native, Kernel)
    again = migrate_completed_original_v6(snapshot, profile_snapshot=evidence / 'profile.json', **options)
    assert isinstance(again, Kernel) and again.storage.path == native.storage.path
    resource = facts.subjects[0].resources[0]
    assert resource.checkout is not None
    git = LocalGit(managed.repository_root)
    assert git.run('symbolic-ref', 'HEAD', cwd=resource.checkout).stdout.strip() == resource.source_ref
    assert _git_bytes(managed.repository_root) == before[1]
    if mode == 'inverse':
        result = rollback_original_v6(snapshot, **options)
        assert isinstance(result, dict) and result['status'] == 'rolled_back'
        assert rollback_original_v6(snapshot, **options) == result
        assert facts.source.task is not None and read_task(task_path(facts.source.task)) is None
        assert git.run('symbolic-ref', 'HEAD', cwd=resource.checkout).stdout.strip() == resource.source_ref
        with closing(sqlite3.connect(snapshot.source)) as db:
            from agent_run_kernel.migration_cutover import _same_original_rows, _OLD_PROTOCOL
            _same_original_rows(db, snapshot.path, _OLD_PROTOCOL)
        with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
            native.claim_executor('stale-after-completed-inverse')
        return
    execution = native.claim_executor('completed-clean-native')
    state, delivery = native.status('ticket:2'), native.delivery('ticket:2')
    port = LocalEffects(git, owned_root=resource.checkout.parent)
    observed: list[Effect] = []

    class CleanupPorts(ReadOnlyPorts):
        def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
            assert effect.operation == operation and effect.payload['expected_head'] == facts.subjects[0].completion.publication.sha
            assert effect.payload['source_uuid'] == facts.source.checkout_uuid
            result = port.execute(effect, authorize)
            assert result.disposition == 'target', result
            observed.append(effect)
            return result

        def reconcile(self, effect: Effect) -> EffectResult:
            return port.reconcile(effect)

        def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
            return port.inspect_resources(resources)

    def make_ports(binding: OperationBinding) -> CleanupPorts:
        assert binding.repository_root == git.root and binding.repository == facts.spec.repository
        # These external source/ref facts stay fixed during local cleanup.
        # Actual worktree/ref inspection and writes use LocalEffects above.
        return CleanupPorts(facts.observed_requirements, delivery.source_ref, delivery.target_ref,
            DeliveryFacts(git.commit(delivery.target_ref), git.commit(resource.source_ref)))

    operations = CoreOperations(native, execution, make_ports)
    for operation in ('cleanup_worktree', 'cleanup_local_ref'):
        count = len(observed)
        step = operations.until(lambda _: len(observed) > count, agent_capacity=0)
        assert step.kind == 'progress' and len(observed) == count + 1, step
    operations.until(lambda _: native.status('ticket:2')['resource_cleanup']['stage'] == 2, agent_capacity=0)
    assert not resource.checkout.exists()
    assert git.run('show-ref', '--verify', '--quiet', resource.source_ref, check=False).returncode == 1
    assert native.status('ticket:2')['budgets'] == state['budgets']
    with native.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one("SELECT count(*) FROM effects WHERE operation='close'")[0] == 1
        for effect in observed:
            assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('confirmed', 1)
            assert tx.one("SELECT count(*) FROM history WHERE kind='effect_dispatched' AND identity=?", (effect.effect_id,))[0] == 1
