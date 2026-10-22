"""Canonical consumer tests; decoder equivalence remains a separate gate."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess

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
from support.frozen_v6_runtime import old_v6_runtime


@pytest.fixture(scope='module')
def old_source(tmp_path_factory):
    return old_v6_runtime(tmp_path_factory.mktemp('canonical-install-old-source'))


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
