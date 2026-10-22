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
from agent_run_kernel.offline import fixture_inputs
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.model import Profile, World
from support.frozen_v6_runtime import old_v6_runtime


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
    current = canonical.status('ticket:2')
    world = World(current['parent_revision'], current['graph_revision'], current['revision'],
        decoded.spec.deliveries[0].base, completion_revision=current['completion_revision'])
    step = canonical.advance('ticket:2', canonical.claim_executor('canonical-idle'), world)
    assert step.kind == 'effect' and step.effect is not None and step.effect.operation == 'workspace'
    assert step.effect.source_ref == workspaces[2][0]
