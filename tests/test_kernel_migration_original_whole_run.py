"""Canonical mixed original Run producer, before native mapping is attempted."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from support.v6_whole_run_source import whole_run_managed_source, continue_original_whole_run
from test_kernel_continuation_upgrade import native_v17_source


def test_original_whole_run_producer_retains_completed_returned_and_unallocated_tickets(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = whole_run_managed_source(tmp_path, git_repo)
    report = json.loads(paths['report'].read_bytes())
    with closing(sqlite3.connect(paths['database'])) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM control_tasks').fetchone()[0] == 1
        host = db.execute('SELECT status,exited_at FROM control_executors').fetchone()
        assert host['status'] == 'exited' and host['exited_at']
        assert db.execute('SELECT status,application_observed FROM control_actions').fetchone()[:] == ('completed', 1)
        jobs = {row['value_ticket_number']: row for row in db.execute(
            "SELECT * FROM work_jobs WHERE owner_kind='ticket' AND stage IS NOT NULL")}
        assert jobs[2]['stage'] == 'completed' and jobs[3]['stage'] == 'reviewing'
        assert 4 not in jobs
        owner = db.execute('SELECT * FROM work_job_ticket_close_ownership WHERE job_id=?',
                           (jobs[2]['job_id'],)).fetchone()
        assert owner['value_event_id'] == 4102 and owner['value_actor'] == 'fixture-publisher'
        budgets = {row['job_id']: row for row in db.execute('SELECT * FROM work_job_review_budget')}
        assert [(budgets[jobs[n]['job_id']]['value_development_attempts'],
                 budgets[jobs[n]['job_id']]['value_reviewer_invocations']) for n in (2, 3)] == [(1, 1), (1, 0)]
        roots = db.execute('SELECT * FROM work_job_attempt_roots WHERE job_id=?', (jobs[3]['job_id'],)).fetchone()
        assert roots['pending_attempt_id'] == report['b_pending_attempt']
        pending = db.execute('SELECT status,raw_result_evidence_id,attempt_version_id FROM work_invocations WHERE attempt_id=?',
                             (roots['pending_attempt_id'],)).fetchone()
        assert pending['status'] == 'completed' and pending['raw_result_evidence_id']
        assert pending['attempt_version_id'] != db.execute('SELECT version_id FROM work_attempt_current WHERE attempt_id=?',
                                                          (roots['pending_attempt_id'],)).fetchone()[0]
    assert report['b_base'] == report['a_integrated'] != report['default_base']
    assert (managed.state_root / 'worktrees' / report['run_id'] / 'ticket-3' / 'feature-2.txt').read_text() == 'done 2\n'


def test_original_whole_run_private_install_uses_one_core_and_exact_subject_proofs(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, *, canonical_drift: str | None = None,
) -> None:
    from agent_run_kernel import Kernel
    from agent_run_kernel.migration import capture_v6
    from agent_run_kernel.migration_records import V6RecordReader
    from agent_run_kernel.migration_run import read_original_run, WorkedTicket
    from agent_run_kernel.migration_run_external import collect_run_external, verify_run_external
    from agent_run_kernel.migration_run_facts import SourceKey
    from agent_run_kernel.migration_run import read_original_run_import
    from agent_run_kernel.migration_run_install import install_imported_facts
    from agent_run_kernel.migration_run_projection import verify_imported_installation
    from agent_run_kernel.migration import MigrationRefused
    from agent_run_kernel.migration_run_resources import seal_run_checkouts, authenticate_run_resources
    from agent_run_kernel.model import wire_json
    from test_kernel_migration_completed_history import _assert_original_records
    from test_kernel_migration_original_source import _git_bytes

    managed, paths = whole_run_managed_source(tmp_path, git_repo)
    report = json.loads(paths['report'].read_bytes())
    evidence = tmp_path / 'whole-evidence'
    evidence.mkdir()
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    snapshot = capture_v6(paths['database'], evidence / 'source.sqlite3')
    facts = read_original_run(snapshot, paths['profile'])
    assert [(ticket.owner.subject, getattr(ticket, 'kind', 'unallocated')) for ticket in facts.tickets] == [
        ('ticket:2', 'completed'), ('ticket:3', 'returned'), ('ticket:4', 'unallocated')]
    assert [(ticket.base_sha, ticket.budget.developments_used, ticket.budget.reviews_used)
            for ticket in facts.tickets if isinstance(ticket, WorkedTicket)] == [
        (report['default_base'], 1, 1), (report['a_integrated'], 1, 0)]
    assert [edge.number for issue in facts.header.requirements.issues if issue.number == 4
            for edge in issue.blocked_by] == [3]
    real_git = shutil.which('git')
    assert real_git is not None
    monkeypatch.setenv('PATH', str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('V6_CLOSE_SERVICE', str(tmp_path / 'close-service.json'))
    monkeypatch.setenv('V6_CLOSE_CALLS', str(tmp_path / 'close-service-calls.jsonl'))
    monkeypatch.setenv('V6_CLOSE_REAL_GIT', real_git)
    proof = collect_run_external(facts, managed.repository_root)
    run_head, closes = verify_run_external(facts, proof, managed.repository_root)
    with closing(sqlite3.connect(snapshot.path)) as db:
        uuid = db.execute('SELECT value_checkout_identity FROM run_root').fetchone()[0]
    origin = {'nonce': '6' * 32, 'source_db': str(paths['database']), 'source_sha256': snapshot.sha256,
        'run_id': report['run_id'], 'task_fingerprint': facts.header.task.fingerprint,
        'source_uuid': uuid, 'target_db': str(managed.state_root / report['run_id'] / 'kernel.sqlite3')}
    sealed = seal_run_checkouts(facts, evidence)
    resources = authenticate_run_resources(snapshot, facts, origin, evidence, sealed, run_head)
    assert {subject: len(entries) for subject, entries in resources.items()} == {'ticket:2': 0, 'ticket:3': 1, 'ticket:4': 0}
    destination = tmp_path / 'private-whole.sqlite3'
    private_origin = {**origin, 'target_db': str(destination)}
    (evidence / 'external-readproof.json').write_bytes(wire_json(proof))
    (evidence / 'checkouts.json').write_bytes(wire_json(sealed))
    canonical = read_original_run_import(snapshot, paths['profile'],
        external_snapshot=evidence / 'external-readproof.json', checkouts_snapshot=evidence / 'checkouts.json')
    kernel = install_imported_facts(canonical, destination, origin=private_origin)
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.rows('SELECT id,phase,base_sha FROM subjects ORDER BY id')] == [
            ('ticket:2', 'complete', report['default_base']), ('ticket:3', 'review', report['b_base']),
            ('ticket:4', 'workspace', report['default_base'])]
        assert [tuple(row) for row in tx.rows('SELECT subject,predecessor FROM dependencies ORDER BY subject')] == [
            ('ticket:3', 'ticket:2'), ('ticket:4', 'ticket:3')]
        assert not tx.rows('SELECT id FROM invocations') and not tx.rows('SELECT subject FROM leases')
        assert [tuple(row) for row in tx.rows('SELECT operation,status FROM effects ORDER BY operation')] == [
            ('close', 'confirmed'), ('completion_comment', 'confirmed')]
        manifest = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_run_import'")
        body = json.loads(tx.get(manifest['material_id'], manifest['subject']))
        assert [entry['subject'] for entry in body['subjects']] == ['ticket:2', 'ticket:3', 'ticket:4']
        for entry in body['subjects']:
            subject_proof = json.loads(tx.get(entry['proof_id'], entry['subject']))
            assert subject_proof['subject'] == entry['subject'] and subject_proof['origin_nonce'] == origin['nonce']
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_original_cutover'")[0] == 1
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_record_import'")[0] == 1
        assert tx.one("SELECT count(*) FROM runs")[0] == 1
        assert tx.one("SELECT backend_id FROM threads WHERE id=(SELECT development_thread FROM subjects WHERE id='ticket:3')")[0] == 'whole-development-3'
    _assert_original_records(snapshot, V6RecordReader(kernel))
    assert kernel.source_status()['frontier'] == ['ticket:3']
    assert kernel.source_status()['remaining']['ticket:4'] == 'blocked_by_incomplete_tickets'
    proof_sha = hashlib.sha256(wire_json(proof)).hexdigest()
    verify_imported_installation(kernel, canonical, origin=private_origin)
    if canonical_drift is not None:
        with kernel.storage.transaction() as tx:
            if canonical_drift == 'dependencies':
                tx.execute("DELETE FROM dependencies WHERE subject='ticket:4' AND predecessor='ticket:3'")
            else:
                revision = tx.one('SELECT subject,material_id FROM profile_revisions WHERE id=1')
                document = json.loads(tx.get(revision['material_id'], revision['subject']))
                document['profiles']['review']['model'] = 'gpt-6-sol'
                replacement = tx.put(revision['subject'], 'profile_revision', wire_json(document))
                tx.execute('UPDATE profile_revisions SET material_id=? WHERE id=1', (replacement,))
        candidate_bytes = destination.read_bytes()
        with pytest.raises(MigrationRefused) as refused:
            verify_imported_installation(kernel, canonical, origin=private_origin)
        assert refused.value.code == 'native_original_cutover_advanced'
        assert destination.read_bytes() == candidate_bytes
        return
    with kernel.storage.transaction(write=False) as tx:
        other_context = tx.one("SELECT context_id FROM attempts WHERE subject='ticket:3' AND role='development'")[0]
        review_thread = tx.one("SELECT id FROM threads WHERE subject='ticket:3' AND role='review'")[0]
    # A prepared Run is an exact conversion, including the other owners and
    # historical Attempt authority. Read-only validation must catch drift
    # before either source retirement or inverse can use it.
    for table, column, predicate, identity, drift in (
        ('subjects', 'source_sha', 'id=?', 'ticket:4', '0' * 40),
        ('subjects', 'integrated_sha', 'id=?', 'ticket:2', '0' * 40),
        ('attempts', 'base_sha', 'id=?', report['b_pending_attempt'], '0' * 40),
        ('attempts', 'context_id', 'id=?', report['b_pending_attempt'], other_context),
        ('attempts', 'outcome', 'id=?', report['b_pending_attempt'], 'pass'),
        ('budgets', 'development_limit', "subject=? AND purpose='requirements' AND window=1", 'ticket:4', 99),
        ('resync_allowances', 'used', 'subject=? AND generation=1', 'ticket:3', 1),
        ('runs', 'source_sequence', 'id=?', report['run_id'], 2),
        ('issue_facts', 'state', 'issue=?', 4, 'CLOSED'),
        ('dependency_facts', 'state', 'issue=? AND blocker=3', 4, 'CLOSED'),
        ('requirement_versions', 'title', 'issue=?', 4, 'Changed requirement'),
        ('source_observations', 'body', 'sequence=?', 1, wire_json({'foreign': 'requirements'})),
        ('final_target', 'target_ref', 'id=?', 'final:' + report['run_id'], 'refs/heads/foreign'),
        ('threads', 'backend_id', 'id=?', review_thread, 'foreign-review-thread'),
        ('effects', 'actor', 'id=?', f"v6-import:{report['run_id']}:2:close", 'foreign-actor'),
    ):
        with kernel.storage.transaction() as tx:
            original = tx.one(f'SELECT {column} FROM {table} WHERE {predicate}', (identity,))[0]
            tx.execute(f'UPDATE {table} SET {column}=? WHERE {predicate}', (drift, identity))
        candidate_bytes = destination.read_bytes()
        try:
            with pytest.raises(MigrationRefused) as refused:
                verify_imported_installation(kernel, canonical, origin=private_origin)
            assert refused.value.code == 'native_original_cutover_advanced'
            assert destination.read_bytes() == candidate_bytes
        finally:
            with kernel.storage.transaction() as tx:
                tx.execute(f'UPDATE {table} SET {column}=? WHERE {predicate}', (original, identity))
        verify_imported_installation(kernel, canonical, origin=private_origin)
    # These audit values do not authorize execution. No broad timestamp or
    # material exclusion is used: only declared mechanical clocks and valid,
    # unreferenced immutable material may vary.
    with kernel.storage.transaction() as tx:
        tx.execute('UPDATE profile_revisions SET created_at=created_at+0.5')
        tx.execute("UPDATE history SET created_at=created_at+0.5 WHERE kind IN ('source_refreshed','v6_original_cutover','v6_record_import')")
        tx.put('ticket:3', 'inspection_note', b'Unreferenced read-only diagnostic')
    candidate_bytes = destination.read_bytes()
    verify_imported_installation(kernel, canonical, origin=private_origin)
    assert destination.read_bytes() == candidate_bytes
    assert (paths['database'].read_bytes(), _git_bytes(managed.repository_root)) == before


@pytest.mark.parametrize('drift', ['dependencies', 'profile'])
def test_prepared_whole_run_refuses_changed_canonical_installation(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, drift: str,
) -> None:
    test_original_whole_run_private_install_uses_one_core_and_exact_subject_proofs(
        tmp_path, git_repo, monkeypatch, canonical_drift=drift)


def test_original_whole_run_formal_cutover_continues_c_and_final_against_original(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, *,
    inverse: bool = False, foreign_marker: bool = False, initial_native_source: Path | None = None,
) -> None:
    from dataclasses import replace
    from agent_run_kernel import Kernel
    from agent_run_kernel.effects import LocalGit
    from agent_run_kernel.migration import capture_v6
    from agent_run_kernel.migration_records import V6RecordReader
    from agent_run_kernel.model import CreationInputs, Policy, RunSpec, wire_json
    from agent_run_kernel.storage import PREVIOUS_PROTOCOL, PROTOCOL, Storage
    from agent_run_kernel.offline import Scenario, review_result
    from support.cli_payloads import publication, final_run_publication
    from support.returned_remote import ReturnedRemote
    from test_kernel_migration_completed_history import _assert_original_records
    from test_kernel_migration_original_cutover import _OLD_WRITES
    from test_kernel_migration_original_source import _git_bytes

    oracle_git = tmp_path / 'oracle-git'
    shutil.copytree(git_repo, oracle_git, symlinks=True)
    with monkeypatch.context() as old_environment:
        old_environment.setenv('XDG_DATA_HOME', str(tmp_path / 'oracle-data'))
        old_managed, old_paths = whole_run_managed_source(tmp_path / 'oracle', oracle_git)
        original = continue_original_whole_run(old_managed, old_paths)
    managed, paths = whole_run_managed_source(tmp_path, git_repo)
    report = json.loads(paths['report'].read_bytes())
    run_id = report['run_id']
    original_bytes = paths['database'].read_bytes()
    git_before = _git_bytes(managed.repository_root)
    real_git = shutil.which('git')
    assert real_git is not None
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'), 'V6_CLOSE_REAL_GIT': real_git}
    evidence = tmp_path / 'formal-whole'
    command = [sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--repo', 'example/project',
               '--parent', '1', '--evidence-dir', str(evidence)]
    initial_environment = environment if initial_native_source is None else {
        **environment, 'PYTHONPATH': str(initial_native_source)}
    applied = subprocess.run(command, env=initial_environment, capture_output=True, text=True, timeout=30)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    result = json.loads(applied.stdout)
    assert result['status'] == 'migrated' and result['supported_source']['kind'] == 'original_run'
    target = Path(result['target']['database'])
    if initial_native_source is not None:
        with Storage(target).transaction(write=False) as tx:
            assert tx.one('SELECT protocol FROM meta')[0] == PREVIOUS_PROTOCOL
            assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    kernel = Kernel(target)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT protocol FROM meta')[0] == PROTOCOL
        assert all(tuple(row) == (None, None, None) for row in tx.all(
            'SELECT current_invocation,thread_id,opening_id FROM attempts'))
        assert tx.one("SELECT count(*) FROM material WHERE purpose='attempt_continuation_opening'")[0] == 0
    assert (evidence / 'source.sqlite3').read_bytes() == original_bytes
    retained = paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)
    resumed = subprocess.run(command + ['--resume'], env=environment, capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)['nonce'] == result['nonce']
    assert (paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)) == retained
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'records-copy.sqlite3'), V6RecordReader(kernel))
    old_environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
        str(paths['frozen'] / name) for name in ('project/src', 'project/tests'))}
    refused = subprocess.run([sys.executable, '-c', _OLD_WRITES], env=old_environment,
        input=json.dumps({'root': str(managed.repository_root), 'state': str(managed.state_root), 'github': str(paths['github'])}),
        capture_output=True, text=True, timeout=15)
    assert refused.returncode == 0, refused.stdout + refused.stderr
    if inverse:
        if foreign_marker:
            local = LocalGit(managed.repository_root)
            branch = 'refs/heads/' + report['b_branch']
            prior = local.commit(report['b_candidate'])
            foreign = local.make_commit(prior.tree, [prior.sha], 'foreign marker\n')
            local.run('update-ref', branch, foreign.sha, prior.sha)
            before = paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)
            rollback = subprocess.run(command + ['--rollback'], env=environment, capture_output=True, text=True, timeout=30)
            assert rollback.returncode == 1 and json.loads(rollback.stdout)['status'] == 'refused', rollback.stdout + rollback.stderr
            assert (paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)) == before
            return
        rollback = subprocess.run(command + ['--rollback'], env=environment, capture_output=True, text=True, timeout=30)
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert json.loads(rollback.stdout)['status'] == 'rolled_back'
        assert kernel.storage.path.is_file()
        with closing(sqlite3.connect(paths['database'])) as db:
            assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == 'sqlite-work-records-v6'
        checkout = managed.state_root / 'worktrees' / run_id / 'ticket-3'
        assert LocalGit(managed.repository_root).run('symbolic-ref', 'HEAD', cwd=checkout).stdout.strip() == 'refs/heads/' + report['b_branch']
        assert all(_git_bytes(managed.repository_root)[name] == digest for name, digest in git_before.items() if '/logs/HEAD' not in name)
        restored = continue_original_whole_run(managed, paths)
        assert restored == original
        return
    class WholeRemote(ReturnedRemote):
        def delivery(self, *arguments, **options):
            return replace(super().delivery(*arguments, **options), publisher_actor='fixture-publisher')
    port = WholeRemote.create(tmp_path / 'native-whole-remote')
    port.git = LocalGit(managed.repository_root)
    port.local_root = managed.state_root / 'worktrees' / run_id
    external = json.loads(paths['github'].read_bytes())
    with closing(sqlite3.connect(port.database)) as db:
        db.execute('DELETE FROM refs')
        heads = {**external['delivery']['published_branches'], external['default_branch']: external['default_head_sha']}
        db.executemany('INSERT INTO refs VALUES (?,?)', [('refs/heads/' + ref, sha) for ref, sha in heads.items()])
        db.commit()
    with kernel.storage.transaction(write=False) as tx:
        frozen = tx.one('SELECT subject,material_id FROM run_inputs')
        inputs = json.loads(tx.get(frozen['material_id'], frozen['subject']))
        profile = tx.one('SELECT subject,material_id FROM profile_revisions WHERE id=1')
        profiles = json.loads(tx.get(profile['material_id'], profile['subject']))
        assert not tx.rows('SELECT id FROM invocations')
    execution = kernel.claim_executor('whole-native-executor')
    role_calls = []
    b_merge = None
    class WholeScenario(Scenario):
        def agent(self, call):
            subject = self.delivery.key
            role_calls.append(call.role.value)
            self.role_calls.append({'role': call.role.value, 'purpose': call.purpose.value, 'owner_kind': self.delivery.kind.value,
                                    'attempt': call.attempt_id, 'repair_source': call.context.get('repair_source')})
            if subject == 'ticket:3':
                assert call.role.value == 'publication', 'B durable D/R must be consumed without replay'
                assert call.reported_thread == 'whole-development-3' and call.mode == 'resume'
            if call.role.value == 'development':
                assert subject == 'ticket:4' and call.mode == 'fresh' and call.reported_thread is None
                assert call.context['base_sha'] == b_merge
                checkout = Path(call.context['checkout'])
                assert (checkout / 'feature-2.txt').read_text() == 'done 2\n'
                assert (checkout / 'feature-3.txt').read_text() == 'done 3\n'
                kernel.bind_thread(call, 'whole-development-4')
                (checkout / 'feature-4.txt').write_text('done 4\n')
                self.development_count += 1
                return wire_json({'result_kind': 'development', 'summary': 'Deliver feature 4', 'human_blockers': None})
            if call.role.value == 'review':
                assert call.mode == 'fresh' and call.reported_thread is None
                kernel.bind_thread(call, 'whole-review-4' if subject == 'ticket:4' else 'whole-final-review')
                self.review_count += 1
                return wire_json(review_result('pass'))
            if subject == 'ticket:4':
                assert call.reported_thread == 'whole-development-4' and call.mode == 'resume'
            if subject.startswith('final:'):
                assert call.mode == 'fresh' and call.reported_thread is None
            kernel.bind_thread(call, call.reported_thread or 'fixture-run-publication')
            self.publication_count += 1
            return wire_json({'result_kind': 'publication', **(final_run_publication() if subject.startswith('final:') else publication()), 'human_blockers': None})
    from support.migration_operations import configure_source, admit_final
    def scenario(subject: str) -> WholeScenario:
        item = WholeScenario.__new__(WholeScenario)
        item.script, item.kernel, item.port = None, kernel, port
        item.delivery, item.execution = kernel.delivery(subject), execution
        status = kernel.status(subject)
        item.spec = RunSpec(run_id, 'example/project', 1, status['parent_revision'], status['graph_revision'],
            (item.delivery,), Policy(), inputs=CreationInputs(profiles, inputs['prompt_resources'], inputs['worker_identity'],
                inputs['runner_identity'], inputs['notifications'], inputs['provenance']))
        item.initial = port.git.commit('refs/heads/main')
        item.reviews, item.checks = ['pass'], ['none']
        item.review_count = item.development_count = item.publication_count = 0
        item.heads, item.role_calls, item.checkpoints, item.reviewed_heads = [], [], [], []
        item.edits = None
        configure_source(item, external)
        return item
    assert scenario('ticket:3').run().kind == 'complete'
    b_cleanup = kernel.status('ticket:3')['resource_cleanup']
    assert b_cleanup['state'] == 'complete', b_cleanup
    with kernel.storage.transaction(write=False) as tx:
        b_merge = tx.one("SELECT merge_sha FROM completions WHERE subject='ticket:3'")[0]
    assert kernel.source_status()['frontier'] == ['ticket:4']
    assert scenario('ticket:4').run().kind == 'complete'
    run_ref = 'refs/heads/' + report['run_branch']
    run_head, default = port.remote_commit(run_ref), port.remote_commit('refs/heads/main')
    assert run_head is not None and default is not None
    with kernel.storage.transaction(write=False) as tx:
        completion_heads = tuple(row[0] for row in tx.rows(
            'SELECT c.merge_sha FROM completions c JOIN subjects s '
            'ON s.id=c.subject AND s.generation=c.generation ORDER BY c.subject'))
    # Match the production Final observer: every current Completion must be
    # proved into the observed Run head by the real local Git object graph.
    assert all(port.git.is_ancestor(head, run_head.sha) for head in completion_heads)
    final = admit_final(scenario('ticket:4'))
    port.local_root = managed.root / 'worktrees' / run_id
    assert scenario(final).run(approve=True).kind == 'complete'
    assert role_calls == original['calls']
    assert port.remote_commit('refs/heads/main').tree == original['tree']
    with kernel.storage.transaction(write=False) as tx:
        for number in (2, 3, 4):
            assert list(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject=? AND purpose='requirements'", (f'ticket:{number}',))) == original['tickets'][str(number)]['budget']
            assert tx.one('SELECT tree FROM completions WHERE subject=?', (f'ticket:{number}',))[0] == original['tickets'][str(number)]['tree']
        assert tx.one('SELECT count(*) FROM invocations')[0] == 6
    assert not any(write['subject'] == 'ticket:2' for write in port.writes())
    assert all(kernel.status(subject)['resource_cleanup']['state'] == 'complete' for subject in ('ticket:3', 'ticket:4', final))
    assert set(port.remote_refs()) == {'refs/heads/main'}
    assert all(not Path(kernel.delivery(subject).checkout).exists() for subject in ('ticket:2', 'ticket:3', 'ticket:4', final))
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'final-record-copy.sqlite3'), V6RecordReader(kernel))
    advanced = subprocess.run(command + ['--rollback'], env=environment, capture_output=True, text=True, timeout=30)
    assert advanced.returncode == 1 and json.loads(advanced.stdout)['code'] == 'native_original_cutover_advanced', advanced.stdout + advanced.stderr
    assert json.loads(advanced.stdout)['next_action'] == 'rollforward'


@pytest.mark.parametrize('inverse', [False, True], ids=['forward', 'inverse'])
def test_original_whole_run_v17_prepared_upgrade_keeps_source_contract(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    native_v17_source: Path, inverse: bool,
) -> None:
    test_original_whole_run_formal_cutover_continues_c_and_final_against_original(
        tmp_path, git_repo, monkeypatch, inverse=inverse, initial_native_source=native_v17_source)


def test_original_whole_run_formal_inverse_returns_all_owners_to_original_continuation(tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_original_whole_run_formal_cutover_continues_c_and_final_against_original(tmp_path, git_repo, monkeypatch, inverse=True)


def test_original_whole_run_inverse_refuses_foreign_original_marker(tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_original_whole_run_formal_cutover_continues_c_and_final_against_original(tmp_path, git_repo, monkeypatch, inverse=True, foreign_marker=True)


def test_original_whole_run_two_layout_exit_reentry_has_one_authority_and_set_inverse(
    tmp_path: Path, git_repo: Path,
) -> None:
    from agent_run.task_identity import TaskKey
    from agent_run_kernel import Kernel
    from agent_run_kernel.effects import LocalGit
    from agent_run_kernel.migration import capture_v6
    from agent_run_kernel.migration_records import V6RecordReader
    from agent_run_kernel.task_location import read_task, task_path
    from agent_run_kernel.model import KernelError
    from test_kernel_migration_completed_history import _assert_original_records
    from test_kernel_migration_original_cutover import _OLD_WRITES
    from test_kernel_migration_original_source import _git_bytes

    managed, paths = whole_run_managed_source(tmp_path, git_repo, second_returned=True)
    report = json.loads(paths['report'].read_bytes())
    run_id = report['run_id']
    task = TaskKey(managed.repository_root, 'example/project', 1)
    real_git = shutil.which('git')
    assert real_git is not None
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'), 'V6_CLOSE_REAL_GIT': real_git}
    evidence = tmp_path / 'union-evidence'
    arguments = ['--repo', 'example/project', '--parent', '1', '--evidence-dir', str(evidence)]
    command = [sys.executable, '-m', 'agent_run_kernel.migration_cutover', *arguments]
    crash = r'''
import os,sys
from agent_run_kernel import migration_layout as unit
from agent_run_kernel.migration_cli import main
operation=sys.argv[1]; original=getattr(unit,operation); count=0
def exit_after_first(layout):
 global count
 original(layout); count+=1
 if count==1: os._exit(81)
setattr(unit,operation,exit_after_first)
raise SystemExit(main(sys.argv[2:]))
'''
    first = subprocess.run([sys.executable, '-c', crash, 'detach_layout', *arguments],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert first.returncode == 81, first.stdout + first.stderr
    operation_bytes = (evidence / 'operation.json').read_bytes()
    operation = json.loads(operation_bytes)
    target = managed.state_root / run_id / 'kernel.sqlite3'
    kernel = Kernel(target)
    local = LocalGit(managed.repository_root)
    checkouts = [managed.state_root / 'worktrees' / run_id / ('ticket-' + str(number)) for number in (3, 4)]
    assert [local.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False).returncode for checkout in checkouts] == [1, 0]
    assert read_task(task_path(task)) is None
    before = paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)
    with pytest.raises(KernelError) as blocked:
        kernel.claim_executor('must-not-start-before-full-pointer')
    assert blocked.value.code == 'legacy_requires_offline_migration'
    assert (paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)) == before
    wrong = {**operation, 'nonce': '0' * 32}
    (evidence / 'operation.json').write_text(json.dumps(wrong))
    refused = subprocess.run(command + ['--resume'], env=environment, capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1 and json.loads(refused.stdout)['status'] == 'refused'
    assert (paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)) == before
    (evidence / 'operation.json').write_bytes(operation_bytes)
    resumed = subprocess.run(command + ['--resume'], env=environment, capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)['nonce'] == operation['nonce']
    assert read_task(task_path(task)) == (run_id, target)
    assert all(local.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False).returncode == 1 for checkout in checkouts)
    inverse = subprocess.run([sys.executable, '-c', crash, 'restore_layout', *arguments, '--rollback'],
                              env=environment, capture_output=True, text=True, timeout=30)
    assert inverse.returncode == 81, inverse.stdout + inverse.stderr
    assert [local.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False).returncode for checkout in checkouts] == [0, 1]
    assert read_task(task_path(task)) == (run_id, target)
    before = paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)
    with pytest.raises(KernelError) as blocked:
        kernel.claim_executor('must-not-start-during-whole-inverse')
    assert blocked.value.code == 'legacy_requires_offline_migration'
    assert (paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)) == before
    old_environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
        str(paths['frozen'] / name) for name in ('project/src', 'project/tests'))}
    old_input = json.dumps({'root': str(managed.repository_root), 'state': str(managed.state_root), 'github': str(paths['github'])})
    old_refused = subprocess.run([sys.executable, '-c', _OLD_WRITES], env=old_environment, input=old_input,
                                 capture_output=True, text=True, timeout=15)
    assert old_refused.returncode == 0, old_refused.stdout + old_refused.stderr
    restored = subprocess.run(command + ['--rollback'], env=environment, capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert read_task(task_path(task)) is None
    assert [local.run('symbolic-ref', 'HEAD', cwd=checkout).stdout.strip() for checkout in checkouts] == [
        f'refs/heads/agent-run/{run_id}/ticket-3', f'refs/heads/agent-run/{run_id}/ticket-4']
    with closing(sqlite3.connect(paths['database'])) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == 'sqlite-work-records-v6'
        assert db.execute('SELECT count(*) FROM work_attempt_returns').fetchone()[0] == 7
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'union-records.sqlite3'), V6RecordReader(kernel))
