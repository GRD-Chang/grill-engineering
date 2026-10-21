"""Unchanged 02 histories, rather than fixed cardinalities, drive completion import."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from agent_run_kernel.migration import MigrationRefused, SourceSnapshot, capture_v6
from agent_run_kernel.migration_completed import convert_completed_ticket_v6
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration_records import V6RecordReader, preserve_v6_records
from agent_run_kernel.model import CreationInputs, DeliveryKind, Policy, PullRequest, RunSpec, World, wire_json
from agent_run_kernel.offline import OfflinePort, Scenario
from support.frozen_v6_runtime import BASELINE, old_v6_runtime


_COMPLETE = r'''
import hashlib, json, sys
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.state import StateStore
from support.cli_payloads import passing_acceptance, publication, repair_acceptance
import agent_run.controller

request = json.load(sys.stdin)
frozen, root, output = map(Path, (request['frozen'], request['repository'], request['output']))
assert Path(agent_run.controller.__file__).resolve().is_relative_to(frozen / 'project/src')
rounds = request['rounds']
git = GitRepository(root)
states = StateStore(root / '.agent-run')
fixture = output / 'github.json'
fixture.write_text(json.dumps({
    'repository': 'example/project', 'default_branch': 'main', 'default_head_sha': git.resolve('main'),
    'parent': {'number': 1, 'title': 'Parent spec', 'body': 'Deliver the ticket set.',
               'sub_issues': [2], 'sub_issue_order_reliable': True},
    'issues': {'2': {'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
                      'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}},
}))
state, reused = Controller(FixtureGitHubReader(fixture), git, states).start(1)
assert not reused
run_id = state['run_id']
profiles = AgentProfileStore(states.root)
assert profiles.load(run_id) is not None
agents_file = output / 'agents.json'
agents_file.write_text(json.dumps({
    'developments': [
        {'thread_id': 'history-development',
         'expected_thread_id': None if index == 0 else 'history-development',
         'summary': 'Repair round ' + str(index + 1),
         'write_files': {'history-feature.txt': 'implemented round ' + str(index + 1) + '\n'}}
        for index in range(rounds)],
    'reviews': [repair_acceptance('history-review-' + str(index + 1))
                if index + 1 < rounds else passing_acceptance('history-review-' + str(index + 1), 'Complete repair verified.')
                for index in range(rounds)],
    'publications': [{**publication(), 'thread_id': 'history-development'}],
}))
calls = []
class Agents(FixtureAgentBackend):
    def develop(self, request):
        calls.append('development'); return super().develop(request)
    def review(self, request):
        calls.append('review'); return super().review(request)
    def publication(self, request):
        calls.append('publication'); return super().publication(request)
backend = ProfiledAgentBackend(Agents(agents_file), profiles, run_id=run_id)
TicketDeliveryEngine(git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
                     agents=backend).deliver(run_id)
after = states.load_current_run(run_id)
job = after['ticket_jobs']['2']
assert after['status'] == 'ticket_completed' and job['phase'] == 'completed'
assert job['review_budget']['development_attempts'] == rounds
assert job['review_budget']['reviewer_invocations'] == rounds
assert calls == ['development', 'review'] * rounds + ['publication']
assert git.resolve(after['run_branch']) == job['integrated_sha']
external = json.loads(fixture.read_text())
assert external['issues']['2']['state'] == 'CLOSED'
assert len(external['delivery']['pull_requests']) == 1
pull = external['delivery']['pull_requests'][0]
assert pull['state'] == 'MERGED' and pull['integrated_sha'] == job['integrated_sha']
report = {'status': after['status'], 'rounds': rounds,
          'raw_backend_calls': {role: calls.count(role) for role in ('development', 'review', 'publication')},
          'source_identity': {'run_id': run_id, 'pull_request': pull},
          'mutations': external['delivery']['mutations'],
          'candidate_sha': job['candidate_sha'], 'integrated_sha': job['integrated_sha'],
          'calls': calls}
(output / 'report.json').write_text(json.dumps(report, ensure_ascii=False))
print(json.dumps({'run_id': run_id, 'status': after['status']}))
'''


def _completed_source(tmp_path: Path, git_repo: Path, rounds: int) -> dict[str, Path]:
    frozen, source = tmp_path / 'unchanged-02', tmp_path / 'source'
    old_v6_runtime(frozen)  # Only materializes verified, unchanged source files.
    source.mkdir()
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join(str(frozen / name) for name in ('project/src', 'project/tests'))}
    result = subprocess.run([sys.executable, '-c', _COMPLETE], env=environment,
                            input=json.dumps({'frozen': str(frozen), 'repository': str(git_repo),
                                              'output': str(source), 'rounds': rounds}),
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    identity = json.loads(result.stdout)
    assert identity['status'] == 'ticket_completed'
    run_id = identity['run_id']
    paths = {'database': git_repo / '.agent-run/state.sqlite3',
             'github': source / 'github.json', 'report': source / 'report.json',
             'profile': git_repo / '.agent-run/profiles' / (run_id + '.json'),
             'git': source / 'copied.git', 'observation': source / 'observation.json'}
    assert paths['profile'].is_file()
    shutil.copytree(git_repo / '.git', paths['git'], symlinks=True)
    report = json.loads(paths['report'].read_text())
    registrations = subprocess.run(['git', '-C', str(git_repo), 'worktree', 'list', '--porcelain'],
                                    check=True, capture_output=True, text=True, timeout=10).stdout
    report['post_close_worktrees'] = {'registrations': [
        {'path': block.splitlines()[0].removeprefix('worktree '),
         'repository': block.splitlines()[0] == 'worktree ' + str(git_repo)}
        for block in registrations.strip().split('\n\n')]}
    paths['report'].write_text(json.dumps(report, ensure_ascii=False))
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    refs = subprocess.run(['git', '--git-dir', str(paths['git']), 'for-each-ref',
                           '--format=%(refname)%09%(objectname)'], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
    paths['observation'].write_text(json.dumps({
        'source_runtime_revision': BASELINE,
        'backend': 'unchanged 02 FixtureAgentBackend with ProfiledAgentBackend, terminated runtime child',
        'run_id': run_id, 'source_database_sha256': digest(paths['database']),
        'copied_database_sha256': digest(paths['database']),
        'original_report_sha256': digest(paths['report']), 'profile_present': True,
        'profile_sha256': digest(paths['profile']), 'github_sha256': digest(paths['github']),
        'candidate_sha': report['candidate_sha'], 'integrated_sha': report['integrated_sha'],
        'git_refs': refs, 'external_effects': report['mutations'],
        'pull_request': report['source_identity']['pull_request'],
        'old_runtime_exit': {'returncode': result.returncode, 'reaped': True},
    }, ensure_ascii=False))
    return paths


def _assert_original_records(snapshot: SourceSnapshot, reader: V6RecordReader) -> None:
    run_id = reader.source['run_id']
    with closing(sqlite3.connect(snapshot.path)) as db:
        db.row_factory = sqlite3.Row
        populated = set()
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            names = [row['name'] for row in db.execute('PRAGMA table_info("' + table + '")')]
            scope = ' WHERE run_id=?' if 'run_id' in names else ''
            parameters = (run_id,) if scope else ()
            if db.execute('SELECT 1 FROM "' + table + '"' + scope + ' LIMIT 1', parameters).fetchone():
                populated.add(table)
        assert populated <= reader.tables().keys()
        for table, metadata in reader.tables().items():
            schema = [dict(row) for row in db.execute('PRAGMA table_info("' + table + '")')]
            assert metadata['columns'] == schema
            assert metadata['primary_key'] == [row['name'] for row in sorted(schema, key=lambda row: row['pk']) if row['pk']]
            if table == 'schema_meta':
                where, parameters = '', ()
            elif table == 'work_evidence_materials':
                where, parameters = (' WHERE material_id IN (SELECT material_id FROM work_evidence '
                                     'WHERE run_id=? AND material_id IS NOT NULL)', (run_id,))
            else:
                where, parameters = ' WHERE run_id=?', (run_id,)
            order = ','.join('"' + name + '"' for name in metadata['primary_key'])
            expected = [dict(row) for row in db.execute('SELECT * FROM "' + table + '"' + where + ' ORDER BY ' + order, parameters)]
            actual, after = [], 0
            while True:
                page = reader.read(table, after=after, limit=3)
                assert page['source'] == reader.source
                actual.extend(item['values'] for item in page['records'])
                if page['next'] is None:
                    break
                assert page['next'] > after
                after = page['next']
            assert actual == expected, table
            assert metadata['count'] == len(expected)
            for record in expected:
                assert reader.row(table, [record[name] for name in metadata['primary_key']]) == record
        slots = db.execute('SELECT * FROM run_timeline_slots WHERE run_id=? ORDER BY stream,position', (run_id,)).fetchall()
        expected_history = [{'slot': dict(slot), 'event': dict(db.execute(
            'SELECT * FROM run_timeline_events WHERE run_id=? AND event_id=?',
            (run_id, slot['event_id'])).fetchone())} for slot in slots]
        actual_history, after = [], 0
        while True:
            page = reader.history(after=after, limit=3)
            actual_history.extend(page['events'])
            if page['next'] is None:
                break
            assert page['next'] > after
            after = page['next']
        assert actual_history == expected_history  # Every original event identity/body and stream slot.


class _HistoricalPublisherScenario(Scenario):
    original_actor: str

    def world(self) -> World:
        return replace(super().world(), publisher_actor=self.original_actor)


def _continue_final(kernel: Kernel, port: OfflinePort, paths: dict[str, Path]) -> None:
    """Reuse the existing native driver, with copied old Git/external facts."""
    report = json.loads(paths['report'].read_text())
    run_id = report['source_identity']['run_id']
    run_ref = f'refs/heads/agent-run/{run_id}/run'
    run_head, default = port.git.commit(run_ref), port.git.commit('refs/heads/main')
    assert run_head.sha == report['integrated_sha']
    with closing(sqlite3.connect(paths['database'])) as old:
        old.row_factory = sqlite3.Row
        close = dict(old.execute('SELECT * FROM work_job_ticket_close_ownership').fetchone())
    assert json.loads(paths['github'].read_text())['delivery']['ticket_close_ownership']['2']['actor'] == close['value_actor']
    prior = report['source_identity']['pull_request']
    prior_pr = PullRequest(prior['number'], 'example/project', 'refs/heads/' + prior['branch'],
                           'refs/heads/' + prior['base_branch'], port.git.commit(prior['head_sha']),
                           port.git.commit(prior['base_sha']), prior['title'], prior['body'],
                           state=prior['state'], merged=port.git.commit(prior['integrated_sha']))
    with closing(sqlite3.connect(port.database)) as remote:
        remote.execute('DELETE FROM refs')
        remote.executemany('INSERT INTO refs VALUES (?,?)', [('refs/heads/main', default.sha), (run_ref, run_head.sha)])
        remote.execute('INSERT INTO closed VALUES (?,?,?,?)',
                       (2, close['value_intent_binding'], close['value_actor'], run_head.sha))
        remote.execute('INSERT INTO prs VALUES (?,?,?)', (prior_pr.number, 'ticket:2', wire_json(asdict(prior_pr))))
        remote.commit()
    state = kernel.status('ticket:2')
    with kernel.storage.transaction(write=False) as tx:
        frozen = tx.one('SELECT subject,material_id FROM run_inputs')
        inputs = json.loads(tx.get(frozen['material_id'], frozen['subject']))
        profile = tx.one('SELECT subject,material_id FROM profile_revisions WHERE id=1')
        profiles = json.loads(tx.get(profile['material_id'], profile['subject']))
        old_effects = [tuple(row) for row in tx.rows("SELECT id,status,payload_id,receipt_id FROM effects WHERE id LIKE 'v6-import:%' ORDER BY id")]
    execution = kernel.claim_executor('completed-history-native-executor')
    final = kernel.activate_final(execution, run_head, default,
                                  ancestry=((default.sha, run_head.sha),))
    scenario = _HistoricalPublisherScenario.__new__(_HistoricalPublisherScenario)
    scenario.original_actor = close['value_actor']
    scenario.script, scenario.kernel, scenario.port, scenario.execution = None, kernel, port, execution
    scenario.delivery = kernel.delivery(final)
    scenario.spec = RunSpec(run_id, 'example/project', 1, state['parent_revision'], state['graph_revision'],
                            (scenario.delivery,), Policy(), inputs=CreationInputs(
                                profiles, inputs['prompt_resources'], inputs['worker_identity'],
                                inputs['runner_identity'], inputs['notifications'], inputs['provenance']))
    scenario.initial = default
    scenario.reviews, scenario.checks = ['pass'], ['none']
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads, scenario.role_calls, scenario.checkpoints, scenario.reviewed_heads = [], [], [], []
    scenario.edits = None
    assert scenario.run(approve=True).kind == 'complete'
    assert kernel.status(final)['phase'] == 'complete'
    assert [call['role'] for call in scenario.role_calls] == ['review', 'publication']
    assert all(write['subject'] == final for write in port.writes())
    assert port.pull_request('ticket:2') == prior_pr
    new_pr = port.pull_request(final)
    assert new_pr is not None and new_pr.number > prior_pr.number
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.rows("SELECT id,status,payload_id,receipt_id FROM effects WHERE id LIKE 'v6-import:%' ORDER BY id")] == old_effects
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements' AND current=1")) == (report['rounds'], report['rounds'])
    assert json.loads(paths['report'].read_text())['raw_backend_calls'] == {'development': report['rounds'], 'review': report['rounds'], 'publication': 1}


def test_record_reader_recovers_original_relations_without_execution_authority(
    tmp_path: Path, git_repo: Path,
) -> None:
    paths = _completed_source(tmp_path, git_repo, 2)
    original_sha = hashlib.sha256(paths['database'].read_bytes()).hexdigest()
    copied = tmp_path / 'records-source.sqlite3'
    copied.write_bytes(paths['database'].read_bytes())
    # Synthetic unrelated rows test the export boundary on a private copy.
    # They never alter the unchanged runtime's source or its observation.
    with closing(sqlite3.connect(copied)) as db:
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
        columns = [row[1] for row in db.execute('PRAGMA table_info(runs)')]
        values = list(db.execute('SELECT * FROM runs').fetchone())
        values[columns.index('run_id')] = 'unrelated-run'
        db.execute('INSERT INTO runs VALUES (' + ','.join('?' for _ in values) + ')', values)
        secret = 'unrelated credential must stay outside the record closure'
        db.execute('INSERT INTO work_evidence_materials VALUES (?,?,?)',
                   (hashlib.sha256(secret.encode()).hexdigest(), secret, len(secret.encode())))
        db.commit()
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    snapshot = capture_v6(copied, tmp_path / 'records-captured.sqlite3')
    port = OfflinePort.create(tmp_path / 'records-native')
    spec, delivery = port.prepare(DeliveryKind.TICKET)
    kernel = Kernel.create(port.root / 'kernel.sqlite3', replace(spec, run_id=run_id))
    with kernel.storage.transaction(write=False) as tx:
        before_subject = dict(tx.one('SELECT * FROM subjects'))
        before_budget = [dict(row) for row in tx.rows('SELECT * FROM budgets ORDER BY subject,purpose,window')]
    preserve_v6_records(snapshot, kernel, run_id=run_id, subject=delivery.key)
    reader = V6RecordReader(kernel)
    assert reader.source == {'run_id': run_id, 'protocol': snapshot.source_protocol,
                             'sha256': snapshot.sha256, 'schema_sha256': snapshot.schema_sha256}
    _assert_original_records(snapshot, reader)
    with closing(sqlite3.connect(snapshot.path)) as db:
        db.row_factory = sqlite3.Row
        evidence = db.execute('SELECT evidence_id,value_digest,value_bytes FROM work_evidence WHERE run_id=?', (run_id,)).fetchall()
        for record in evidence:
            resolved = reader.evidence(record['evidence_id'])
            body = wire_json(resolved['value'])
            assert len(body) == record['value_bytes']
            assert hashlib.sha256(body).hexdigest() == record['value_digest']
        attempt_ids = [row[0] for row in db.execute('SELECT attempt_id FROM work_job_attempt_history WHERE run_id=? ORDER BY position', (run_id,))]
        readable = [reader.attempt(identity) for identity in attempt_ids]
        assert [entry['attempt']['role'] for entry in readable] == ['development', 'reviewer'] * 2 + ['publication']
        assert all(entry['evidence']['budget_snapshot_evidence_id']['value'] is not None for entry in readable[:-1])
        assert readable[-1]['version']['budget_snapshot_evidence_id'] is None
        assert 'budget_snapshot_evidence_id' not in readable[-1]['evidence']
        timeline_count = db.execute('SELECT count(*) FROM run_timeline_slots WHERE run_id=?', (run_id,)).fetchone()[0]
    history = reader.history()
    assert history['next'] is None and len(history['events']) == timeline_count
    assert [entry['slot']['position'] for entry in history['events']] == list(range(timeline_count))
    assert history['events'][-1]['event']['value_phase'] == 'completed'
    assert b'unrelated credential' not in kernel.storage.path.read_bytes()
    with kernel.storage.transaction(write=False) as tx:
        assert dict(tx.one('SELECT * FROM subjects')) == before_subject
        assert [dict(row) for row in tx.rows('SELECT * FROM budgets ORDER BY subject,purpose,window')] == before_budget
        assert tx.one('SELECT count(*) FROM attempts')[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM effects')[0] == 0
    assert hashlib.sha256(paths['database'].read_bytes()).hexdigest() == original_sha


@pytest.mark.parametrize('boundary', ['missing-run', 'expanded-history'])
def test_record_reader_checks_source_identity_and_expanded_page_budget(
    tmp_path: Path, boundary: str,
) -> None:
    original = (Path(__file__).resolve().parents[3] / 'results/kernel-completed-ticket-observation/'
                'completed-ticket-v6.sqlite3')
    assert hashlib.sha256(original.read_bytes()).hexdigest() == '8acb67257401b081626734ee26c25c50e590847a701c5defd1127df7f3aed3be'
    copied = tmp_path / 'source.sqlite3'
    copied.write_bytes(original.read_bytes())
    with closing(sqlite3.connect(copied)) as db:
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
        if boundary == 'expanded-history':
            keys = db.execute("SELECT event_id FROM run_timeline_slots WHERE stream='timeline' ORDER BY position LIMIT 3").fetchall()
            assert len(keys) == 3
            db.executemany('UPDATE run_timeline_events SET value_worker=? WHERE event_id=?',
                           [('original large display text ' + 'x' * (3 * 1024 * 1024), row[0]) for row in keys])
            db.commit()
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    snapshot = capture_v6(copied, tmp_path / 'captured.sqlite3')
    port = OfflinePort.create(tmp_path / 'native')
    spec, delivery = port.prepare(DeliveryKind.TICKET)
    selected = 'missing-source-run' if boundary == 'missing-run' else run_id
    kernel = Kernel.create(port.root / 'kernel.sqlite3', replace(spec, run_id=selected))
    if boundary == 'missing-run':
        with kernel.storage.transaction(write=False) as tx:
            before = tx.one('SELECT count(*) FROM material')[0]
        with pytest.raises(MigrationRefused, match='source_record_identity'):
            preserve_v6_records(snapshot, kernel, run_id=selected, subject=delivery.key)
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT count(*) FROM material')[0] == before
            assert tx.one("SELECT count(*) FROM history WHERE kind='v6_record_import'")[0] == 0
    else:
        preserve_v6_records(snapshot, kernel, run_id=run_id, subject=delivery.key)
        reader = V6RecordReader(kernel)
        first = reader.history()
        assert len(first['events']) == 2 and first['next'] == 2
        second = reader.history(after=first['next'])
        assert first['events'][0]['event']['value_worker'].startswith('original large display text ')
        assert second['events'][0]['slot']['position'] == 2
        assert len(first['events']) + len(second['events']) == reader.tables()['run_timeline_slots']['count']


@pytest.mark.parametrize('rounds', [2, 3])
def test_completed_ticket_import_preserves_real_multiround_history(
    tmp_path: Path, git_repo: Path, rounds: int,
) -> None:
    paths = _completed_source(tmp_path, git_repo, rounds)
    snapshot = capture_v6(paths['database'], tmp_path / 'copied.sqlite3')
    with closing(sqlite3.connect(snapshot.path)) as db:
        assert db.execute('SELECT count(*) FROM work_attempts').fetchone()[0] == 2 * rounds + 1
        assert db.execute('SELECT count(*) FROM work_invocations').fetchone()[0] == 2 * rounds + 1
        # Original 02 retains last-completed active/ticket references. Their
        # presence does not prove a live invocation; check the referenced fact.
        assert db.execute("SELECT count(*) FROM work_invocations WHERE status!='completed' OR ended_at IS NULL OR resume_pending!=0").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM work_invocation_current c LEFT JOIN work_invocations v USING(run_id,work_subject,started_at) WHERE v.started_at IS NULL OR v.status!='completed' OR v.ended_at IS NULL").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM control_workers').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM control_executors').fetchone()[0] == 0
    target = tmp_path / 'native'
    port = OfflinePort.create(target)
    shutil.rmtree(port.git.root)  # Only this helper's newly created synthetic seed.
    kernel = convert_completed_ticket_v6(
        snapshot, target / 'kernel.sqlite3', git_store=paths['git'], repository_root=port.git.root,
        final_checkout=target / 'work/final', profile_snapshot=paths['profile'],
        github_snapshot=paths['github'], observation_snapshot=paths['observation'],
        original_report=paths['report'])
    assert kernel.status('ticket:2')['phase'] == 'complete'
    with kernel.storage.transaction(write=False) as tx:
        used = tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements' AND current=1")
        assert tuple(used) == (rounds, rounds)
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM attempts')[0] == 3  # Only Completion support; not all history.
    reader = V6RecordReader(kernel)
    _assert_original_records(snapshot, reader)
    assert reader.document('profile')['body'] == paths['profile'].read_bytes()
    assert reader.tables()['work_attempts']['count'] == 2 * rounds + 1
    assert reader.tables()['work_invocations']['count'] == 2 * rounds + 1
    before = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items() if path.is_file()}
    _continue_final(kernel, port, paths)
    _assert_original_records(snapshot, V6RecordReader(kernel))
    assert {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items() if path.is_file()} == before


@pytest.mark.parametrize('boundary', ['unknown-write', 'resume-pending', 'bound-context-drift'])
def test_completed_history_refuses_unsettled_authority_without_publishing(
    tmp_path: Path, git_repo: Path, boundary: str,
) -> None:
    paths = _completed_source(tmp_path, git_repo, 2)
    original_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                       for name, path in paths.items() if path.is_file()}
    copied = tmp_path / 'counterfactual.sqlite3'
    copied.write_bytes(paths['database'].read_bytes())
    with closing(sqlite3.connect(copied)) as db:
        db.row_factory = sqlite3.Row
        if boundary == 'unknown-write':
            current = db.execute("SELECT value_linked_branch_display FROM work_jobs WHERE stage='completed'").fetchone()[0]
            unknown = [(identity, json.loads(body)) for identity, body in db.execute(
                "SELECT e.evidence_id,m.body FROM work_evidence e JOIN work_evidence_materials m ON m.material_id=e.material_id "
                "WHERE e.kind='linked_branch_display'") if json.loads(body).get('status') == 'indeterminate']
            assert len(unknown) == 1 and unknown[0][0] != current
            db.execute("UPDATE work_jobs SET value_linked_branch_display=? WHERE stage='completed'", (unknown[0][0],))
        elif boundary == 'resume-pending':
            changed = db.execute("UPDATE work_invocations SET resume_pending=1 WHERE started_at=(SELECT max(started_at) FROM work_invocations)")
            assert changed.rowcount == 1
        else:
            invocation = dict(db.execute("SELECT * FROM work_invocations WHERE invocation_role='review' ORDER BY started_at DESC LIMIT 1").fetchone())
            attempt_id = invocation['attempt_id']
            current = dict(db.execute('SELECT v.* FROM work_attempt_current c JOIN work_attempt_versions v USING(run_id,attempt_id,version_id) WHERE c.attempt_id=?', (attempt_id,)).fetchone())
            bound = dict(db.execute('SELECT * FROM work_attempt_versions WHERE attempt_id=? AND version_id=?',
                                   (attempt_id, invocation['attempt_version_id'])).fetchone())
            evidence = dict(db.execute('SELECT * FROM work_evidence WHERE evidence_id=?',
                                      (current['review_context_evidence_id'],)).fetchone())
            old_body = db.execute('SELECT body FROM work_evidence_materials WHERE material_id=?', (evidence['material_id'],)).fetchone()[0]
            context = json.loads(old_body)
            context['facts']['human_response_history'] = [{'message': 'Later clarification; unchanged candidate/base/tree.'}]
            body = wire_json(context)
            digest = hashlib.sha256(body).hexdigest()
            evidence_id = hashlib.sha256(wire_json((evidence['owner_id'], 'review_context', digest, None, None))).hexdigest()
            db.execute('INSERT INTO work_evidence_materials VALUES (?,?,?)', (digest, body.decode(), len(body)))
            db.execute('INSERT INTO work_evidence VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (evidence['run_id'], evidence_id, evidence['owner_id'], 'review_context',
                        evidence['version'] + 1, digest, len(body), digest, None, None))
            current['review_context_evidence_id'] = evidence_id
            current['logical_bytes'] += len(body) - len(old_body.encode())
            fields = ('budget_snapshot', 'review_context', 'history_facts', 'review_identity',
                      'development_summary', 'publication', 'publication_operation_retry',
                      'publication_commit_observation', 'returned_result')
            identity = (attempt_id, current['presence'].hex(),
                        tuple(current[key] for key in ('status', 'outcome', 'budget_consumed')),
                        tuple(current[key + '_evidence_id'] for key in fields))
            current['version_id'] = hashlib.sha256(wire_json(identity)).hexdigest()
            db.execute('INSERT INTO work_attempt_versions (' + ','.join(current) + ') VALUES (' +
                       ','.join('?' for _ in current) + ')', tuple(current.values()))
            db.execute('UPDATE work_attempt_current SET version_id=? WHERE attempt_id=?', (current['version_id'], attempt_id))
            db.execute('UPDATE work_job_attempt_history SET version_id=? WHERE attempt_id=?', (current['version_id'], attempt_id))
            assert dict(db.execute('SELECT * FROM work_invocations WHERE attempt_id=?', (attempt_id,)).fetchone()) == invocation
            assert dict(db.execute('SELECT * FROM work_attempt_versions WHERE attempt_id=? AND version_id=?',
                                   (attempt_id, invocation['attempt_version_id'])).fetchone()) == bound
        db.commit()
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    snapshot = capture_v6(copied, tmp_path / 'captured.sqlite3')
    observation = json.loads(paths['observation'].read_text())
    observation['copied_database_sha256'] = observation['source_database_sha256'] = snapshot.sha256
    observed = tmp_path / 'counterfactual-observation.json'
    observed.write_text(json.dumps(observation, ensure_ascii=False))
    target = tmp_path / 'native'
    target.mkdir()
    expected = 'unsupported_source_effect_outcome' if boundary == 'unknown-write' else 'unsupported_source_invocation'
    with pytest.raises(MigrationRefused, match=expected):
        convert_completed_ticket_v6(
            snapshot, target / 'kernel.sqlite3', git_store=paths['git'], repository_root=target / 'repo',
            final_checkout=target / 'work/final', profile_snapshot=paths['profile'],
            github_snapshot=paths['github'], observation_snapshot=observed, original_report=paths['report'])
    assert not (target / 'kernel.sqlite3').exists() and not (target / 'repo').exists()
    assert copied.read_bytes() == snapshot.path.read_bytes()
    assert {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items() if path.is_file()} == original_hashes
