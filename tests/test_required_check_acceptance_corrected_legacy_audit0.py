"""Independent old/current consumers; normal API-created state, local fake-gh."""
from __future__ import annotations

import pytest

import test_required_check_absence_contract_legacy_audit0 as original
from test_required_check_absence_contract_legacy_audit0 import unchanged_old_source

OLD_PREFIX = original.OLD_CALLER.split(
    'from agent_run.external_supervision import ExternalSupervisor, supervision_scope')[0]
NATIVE_PREFIX = original.NATIVE_CALLER.split('for _ in range(80):')[0].replace(
    'checks = normalize_required_checks(read_required_check_facts(reader, pr.number, expected_head_sha=request.check_head))',
    'checks = _physical_read(lambda: normalize_required_checks(read_required_check_facts(reader, pr.number, expected_head_sha=request.check_head)))',
)

OLD_TRANSITIONS = OLD_PREFIX + r'''
from copy import deepcopy
from agent_run.external_supervision import ExternalSupervisor, supervision_scope
from agent_run import external_supervision as query_clock
now = [100.25]
query_clock.monotonic = lambda: now[0]
supervisor = ExternalSupervisor(now=lambda: now[0], sleeper=lambda _: None)
later_payload = payload.pop('_later')

def snapshot():
    current = states.load_run(rid)
    job = current['ticket_jobs']['2']
    window = current.get('supervision_window')
    return {'status': current['status'], 'phase': job['phase'],
            'window': None if window is None else {k: window.get(k) for k in ('kind', 'started_at', 'deadline', 'budget_seconds', 'retry_count')},
            'ci': job.get('ci_recovery'), 'diagnostics': current.get('diagnostics'),
            'checks': job.get('required_checks_evidence')}
with supervision_scope(supervisor):
    engine.deliver(rid)
before = snapshot()
payload.clear()
payload.update(later_payload)
now[0] = 105.25
with supervision_scope(supervisor):
    engine.deliver(rid)
after = snapshot()
# Reopen the actual store. The public Driver's supervisor probes detached
# copies of product-created state, never persisted fabricated state.
reloaded = StateStore(states.root).load_run(rid)
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    code = cli.main(['status', rid, '--state-dir', str(states.root), '--json'])
probes = []
for point in sorted(set([700.25, 2800.25, after['window']['deadline']])):
    now[0] = point
    detached = deepcopy(reloaded)
    allowed = supervisor.before_retry(detached)
    probes.append({'at': point, 'allows_retry': allowed, 'status': detached['status'],
                   'window': {k: detached['supervision_window'].get(k) for k in ('kind', 'started_at', 'deadline')}})
print(json.dumps({'before': before, 'after': after, 'probes': probes,
                  'query_code': code, 'query_stdout': stream.getvalue()}))
'''

NATIVE_TRANSITIONS = NATIVE_PREFIX + """
from support.offline import _physical_read
later_payload = payload.pop('_later')

def snapshot():
    status = s.kernel.status(s.delivery.key)
    with s.kernel.storage.transaction(write=False) as tx:
        windows = [dict(r) for r in tx.all('SELECT * FROM waits')]
    return {'status': status, 'windows': windows, 'checks': observations[-1] if observations else None}

def boundary(*, initial):
    steps = []
    for _ in range(80 if initial else 8):
        step = s.runner().run_once()
        steps.append({'kind': step.kind, 'reason': step.reason})
        if step.call is not None:
            assert initial
            s.runner().authorize_agent(step.call)
            s.kernel.receive(step.call, s.agent(step.call))
        if step.kind in {'waiting', 'paused'}:
            return steps
    raise AssertionError('No normal check wait')
initial_steps = boundary(initial=True)
before = snapshot()
payload.clear()
payload.update(later_payload)
now[0] = 105.25
later_steps = boundary(initial=False)
after = snapshot()
import subprocess
restart_program = \"\"\"import json, sys
from pathlib import Path
from agent_run.core.kernel import Kernel
request = json.load(sys.stdin)
kernel = Kernel(Path(request['path']))
status = kernel.status(request['subject'])
with kernel.storage.transaction(write=False) as tx:
    windows = [dict(row) for row in tx.all('SELECT * FROM waits')]
print(json.dumps({'status': status, 'windows': windows}))
\"\"\"
restarted_process = subprocess.run([sys.executable, '-B', '-c', restart_program],
    input=json.dumps({'path': str(s.kernel.storage.path), 'subject': s.delivery.key}),
    text=True, capture_output=True, timeout=10)
assert restarted_process.returncode == 0, restarted_process.stdout + restarted_process.stderr
restarted = json.loads(restarted_process.stdout)
import agent_run.queries.run as query_clock
query_clock.time.time = lambda: now[0]
qio = io.StringIO()
with contextlib.redirect_stdout(qio):
    code = main(['status', s.spec.run_id, '--state-dir', str(s.kernel.storage.path), '--json'])
s.kernel = Kernel(s.kernel.storage.path)
s.kernel.clock = lambda: now[0]
probes = []
for point in sorted(set([700.25, 2800.25, after['windows'][0]['deadline']])):
    now[0] = point
    step = s.runner().run_once()
    probes.append({'at': point, 'kind': step.kind, 'reason': step.reason,
                   'status': s.kernel.status(s.delivery.key)})
print(json.dumps({'before': before, 'after': after, 'probes': probes,
                  'initial_steps': initial_steps, 'later_steps': later_steps,
                  'restarted': restarted,
                  'writes': s.port.writes(), 'query_code': code, 'query': json.loads(qio.getvalue())}))
"""


def check_input(value):
    if value == 'read_error':
        return {'ruleset': ['A'], 'checks': [], 'checks_exit': 2,
                'checks_stderr': 'temporary gh pr checks failure'}
    return {'ruleset': ['A'], 'checks': [{'name': 'A', 'bucket': value}]}


@pytest.mark.parametrize('initial,later', [
    ('pending', 'mystery'), ('pending', 'read_error'),
    ('mystery', 'pending'), ('read_error', 'pending'),
], ids=['pending-to-server-unknown', 'pending-to-read-error',
        'server-unknown-to-pending', 'read-error-to-pending'])
def test_normal_check_window_transition_matches_fixed_old_consumer(
    tmp_path, unchanged_old_source, monkeypatch, initial, later,
):
    monkeypatch.setattr(original, 'OLD_CALLER', OLD_TRANSITIONS)
    monkeypatch.setattr(original, 'NATIVE_CALLER', NATIVE_TRANSITIONS)
    old, native = original.observations(tmp_path, unchanged_old_source, {
        **check_input(initial), '_later': check_input(later),
    })
    assert old['before']['window']['started_at'] == native['before']['windows'][0]['started_at'] == 100.25
    assert old['before']['window']['deadline'] == native['before']['windows'][0]['deadline']
    assert len(native['after']['windows']) == 1
    assert native['after']['status']['holds_integration'] is True
    assert native['after']['status']['pending_attempt'] is None
    assert native['after']['status']['budgets'] == native['before']['status']['budgets']
    assert native['restarted']['windows'] == native['after']['windows']
    assert native['restarted']['status']['holds_integration'] is True
    assert native['restarted']['status']['budgets'] == native['after']['status']['budgets']
    assert old['after']['window']['started_at'] == native['after']['windows'][0]['started_at']
    assert old['after']['window']['deadline'] == native['after']['windows'][0]['deadline']
    for old_probe, new_probe in zip(old['probes'], native['probes'], strict=True):
        assert old_probe['at'] == new_probe['at']
        assert old_probe['allows_retry'] is (new_probe['reason'] != 'supervision_timeout')
        assert new_probe['status']['holds_integration'] is True


def test_mixed_fail_pending_public_query_keeps_old_waiting_external(
    tmp_path, unchanged_old_source,
):
    old, native = original.observations(tmp_path, unchanged_old_source, {
        'ruleset': ['A', 'B'], 'checks': [
            {'name': 'A', 'bucket': 'fail', 'link': 'https://external.example.invalid/check/A'},
            {'name': 'B', 'bucket': 'pending'},
        ],
    })
    assert old['query']['status'] == 'waiting_external'
    assert old['query']['supervision']['kind'] == native['query']['supervision']['kind'] == 'github_convergence'
    assert native['status']['phase'] == 'checks'
    assert native['status']['holds_integration'] is True
    assert native['query']['status'] == old['query']['status']


def test_server_unknown_with_unreported_context_fixes_old_consumer_contradiction(
    tmp_path, unchanged_old_source, monkeypatch,
):
    monkeypatch.setattr(original, 'OLD_CALLER', OLD_AGGREGATION_DIAGNOSTIC)
    monkeypatch.setattr(original, 'NATIVE_CALLER', NATIVE_AGGREGATION_ACCEPTANCE)
    old, native = original.observations(tmp_path, unchanged_old_source, {
        'ruleset': ['A', 'B'], 'checks': [{'name': 'A', 'bucket': 'mystery'}],
    })
    assert old['status'] == 'blocked' and old['checks_evidence'] is None
    assert old['diagnostics'][0]['code'] == 'published_head_mismatch'
    assert old['validation_error'] == 'Required Checks snapshot result conflicts with check buckets'
    assert old['consumer_derived'] == 'unknown'
    assert old['reads']
    for read in old['reads']:
        assert read['snapshot']['result'] == 'pending'
        assert read['expected'] == read['before']['head_sha'] == read['after']['head_sha']
        assert read['expected'] == old['publication_head'] == old['source_head']
        assert read['before']['head_branch'] == read['after']['head_branch'] == old['source_ref']
        assert read['before']['head_repository'] == read['after']['head_repository'] == 'example/project'
    assert native['checks']['result'] == 'pending'
    assert dict(native['checks']['details']) == {'A': 'unknown', 'B': 'pending'}
    assert native['head_identity']['expected'] == native['head_identity']['before']
    assert native['head_identity']['expected'] == native['head_identity']['after'] == native['remote_source_head']
    assert native['query']['status'] == 'waiting_checks'
    assert native['waits'][0]['kind'] == 'required_checks'
    assert native['waits'][0]['started_at'] == 100.25 and native['waits'][0]['deadline'] == 2800.25
    assert native['at_ten_minutes'] == {'kind': 'waiting', 'reason': 'required_checks'}
    assert native['status']['phase'] == 'checks' and native['status']['purpose'] != 'ci'
    assert native['status']['holds_integration'] is True and native['status']['pending_attempt'] is None
    assert native['status']['budgets'] == native['after_poll']['budgets']
    assert native['after_poll']['holds_integration'] is True
    assert native['status']['budgets']['ci']['developments_used'] == 0
    assert not [call for call in native['role_calls'] if call['purpose'] == 'ci']
    assert not [write for write in native['writes'] if write['operation'] in {'merge', 'close', 'completion_comment'}]
    assert native['guard_error'] == 'change_pr_head_drift'
    assert native['guard_restored_result'] == 'pending'
    assert native['status_after_guard']['budgets'] == native['after_poll']['budgets']
    assert native['status_after_guard']['holds_integration'] is True


OLD_FAMILY = OLD_PREFIX + r'''
from agent_run.external_supervision import ExternalSupervisor, supervision_scope
from agent_run import external_supervision as query_clock
query_clock.monotonic = lambda: 100.25
supervisor = ExternalSupervisor(now=lambda: 100.25, sleeper=lambda _: None)
requested = dict(payload)
after_approval = requested.pop('_after_approval', None)

def actual_evidence(self, pr_number, *, expected_head_sha=None):
    bind({**self.live_pull_request(pr_number), 'number': pr_number})
    return GhGitHubPublisher(self.data['repository'], self.git).required_check_evidence(
        pr_number, expected_head_sha=expected_head_sha)
Publisher.required_check_evidence = actual_evidence

def update_api(value):
    payload.clear()
    payload.update(value)

if kind == 'final':
    # The Ticket is genuinely delivered/merged/closed before Final acceptance.
    update_api({'checks': []})
    with supervision_scope(supervisor):
        completed = engine.deliver(rid)
    assert completed['ticket_jobs']['2']['phase'] == 'completed'
    with supervision_scope(supervisor):
        Controller(FixtureGitHubReader(fixture), git, states).resume(rid)
    from agent_run.agents import ReviewResult
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.run_publication import RunPublicationEngine
    from support.run_artifacts import _passing_artifact
    from support.run_publication import RunPublicationAgents
    class Reviewer:
        def review(self, request):
            return ReviewResult('normal-final-review', _passing_artifact())
    with supervision_scope(supervisor):
        accepted = RunAcceptanceEngine(git=git, states=states, agents=Reviewer(),
            github=publisher, default_head_sha=git.resolve('main')).accept(rid)
    assert accepted['run_acceptance']['phase'] == 'accepted'
    engine = RunPublicationEngine(git=git, states=states, agents=RunPublicationAgents(),
        github=publisher, default_branch='main', default_head_sha=git.resolve('main'))
update_api(requested)
with supervision_scope(supervisor):
    first = engine.publish(rid) if kind == 'final' else engine.deliver(rid)
initial_status = first['status']
if after_approval is not None:
    # Public hook changes only external API responses after the real grant;
    # it never modifies the state argument supplied by the product.
    with supervision_scope(supervisor):
        engine.approve(rid, prepare_state=lambda _state: update_api(after_approval))
current = states.load_run(rid)
owner = current['run_publication'] if kind == 'final' else current['parent_job']
ci_owner = current['run_acceptance'] if kind == 'final' else owner
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    code = cli.main(['status', rid, '--state-dir', str(states.root), '--json'])
fixture_state = json.loads(fixture.read_text())
pr = publisher.live_pull_request(owner['pr_number'])
print(json.dumps({'initial_status': initial_status, 'status': current['status'],
    'phase': owner['phase'], 'grant': owner.get('approval_grant'),
    'ci_phase': ci_owner['phase'], 'repair_source': ci_owner.get('repair_request', {}).get('repair_source', owner.get('repair_source')),
    'ci': ci_owner.get('ci_recovery'), 'window': current.get('supervision_window'),
    'checks': owner.get('required_checks_evidence'), 'pr_state': pr['state'],
    'target_sha': git.resolve('main'), 'initial_base': base,
    'closed_issues': fixture_state.get('delivery', {}).get('closed_issues', []),
    'query_code': code, 'query_stdout': stream.getvalue()}))
'''

NATIVE_FAMILY = NATIVE_PREFIX.replace(
    "kind = req.get('kind', 'ticket')",
    "requested_kind = req.get('kind', 'ticket')\nkind = 'ticket' if requested_kind == 'final' else requested_kind",
).replace('base_make = s.make_ports', '''if requested_kind == 'final':
    assert s.run().kind == 'complete'
    for _ in range(20):
        final_subject = s.kernel.source_status()['final_subject']
        if final_subject is not None:
            break
        s.step()
    else:
        raise AssertionError('Normal Final activation did not occur')
    s.select(final_subject)
base_make = s.make_ports''').replace(
    'if request.check_head is not None and facts.pr is not None:',
    'if facts.pr is not None and facts.pr.state == "OPEN":',
).replace('expected_head_sha=request.check_head',
          'expected_head_sha=request.check_head or facts.source.sha') + """
from support.offline import _physical_read
from agent_run.core.operation_runner import observe_control
after_approval = payload.pop('_after_approval', None)

def boundary():
    for _ in range(160):
        step = s.runner().run_once()
        status = s.kernel.status(s.delivery.key)
        if step.call is not None:
            s.runner().authorize_agent(step.call)
            s.kernel.receive(step.call, s.agent(step.call))
        if observations and (step.kind in {'waiting', 'paused', 'approval', 'complete'} or
                             status['purpose'] == 'ci' or status['phase'] == 'approval'):
            return step
    raise AssertionError('No normal delivery boundary')
boundary()
initial_status = s.kernel.status(s.delivery.key)
grant_before_external_change = None
if after_approval is not None:
    assert initial_status['phase'] == 'approval'
    approval_world = observe_control(s.kernel, s.delivery.key, s.make_ports)
    with s.kernel.storage.transaction(write=False) as tx:
        original_checks = tx.one('SELECT checks_id FROM subjects WHERE id=?', (s.delivery.key,))[0]
        original_material = tx.one('SELECT material_id FROM checks WHERE id=?', (original_checks,))[0]
        prior_checks = json.loads(tx.get(original_material, s.delivery.key))
    (root / 'normal_approval_observations.json').write_text(json.dumps({
        'prior': prior_checks, 'current': asdict(approval_world.checks)}, indent=2))
    s.kernel.control('approve', 'normal-audit-approval', subject=s.delivery.key, world=approval_world)
    with s.kernel.storage.transaction(write=False) as tx:
        grant_before_external_change = tx.one('SELECT grant_id FROM subjects WHERE id=?', (s.delivery.key,))[0]
    assert grant_before_external_change is not None
    payload.clear()
    payload.update(after_approval)
    boundary()
status = s.kernel.status(s.delivery.key)
with s.kernel.storage.transaction(write=False) as tx:
    waits = [dict(r) for r in tx.all('SELECT * FROM waits')]
    grant = tx.one('SELECT grant_id FROM subjects WHERE id=?', (s.delivery.key,))[0]
import agent_run.queries.run as query_clock
query_clock.time.time = lambda: now[0]
qio = io.StringIO()
with contextlib.redirect_stdout(qio):
    code = main(['status', s.spec.run_id, '--state-dir', str(s.kernel.storage.path), '--json'])
print(json.dumps({'initial_status': initial_status, 'status': status,
    'grant_before_external_change': grant_before_external_change, 'grant': grant,
    'checks': observations[-1], 'waits': waits, 'writes': s.writes(),
    'pr_state': s.port.pr_by_number(status['pr_number']).state,
    'target_sha': s.port.physical.refs()[s.delivery.target_ref], 'initial_base': s.initial.sha,
    'query_code': code, 'query': json.loads(qio.getvalue())}))
"""


@pytest.mark.parametrize('kind', ['parent', 'final'])
def test_non_ticket_mixed_pending_waits_for_terminal_failure_evidence(
    tmp_path, unchanged_old_source, monkeypatch, kind,
):
    monkeypatch.setattr(original, 'OLD_CALLER', OLD_FAMILY)
    monkeypatch.setattr(original, 'NATIVE_CALLER', NATIVE_FAMILY)
    old, native = original.observations(tmp_path, unchanged_old_source, {
        'ruleset': ['A', 'B'], 'checks': [
            {'name': 'A', 'bucket': 'fail', 'workflow': 'External CI', 'link': 'https://external.example.invalid/check/A'},
            {'name': 'B', 'bucket': 'pending'},
        ],
    }, kind=kind)
    assert old['status'] == 'waiting_external'
    assert old['query']['diagnostics'][0]['code'] == 'github_check_evidence_observation_pending'
    assert native['status']['purpose'] != 'ci'
    assert native['waits'][0]['kind'] == 'github_convergence'
    assert old['ci']['developments_used'] == native['status']['budgets']['ci']['developments_used'] == 0
    assert old['target_sha'] == old['initial_base']
    assert native['target_sha'] == native['initial_base']


@pytest.mark.parametrize('kind,after,budget', [
    ('parent', {'ruleset': ['A'], 'checks': [{'name': 'A', 'bucket': 'mystery'}]}, 600),
    ('final', {'ruleset': ['A'], 'checks': []}, 2700),
], ids=['parent-server-unknown-after-grant', 'final-unreported-after-grant'])
def test_normal_approval_then_changed_checks_waits_without_merge_or_losing_grant(
    tmp_path, unchanged_old_source, monkeypatch, kind, after, budget,
):
    monkeypatch.setattr(original, 'OLD_CALLER', OLD_FAMILY)
    monkeypatch.setattr(original, 'NATIVE_CALLER', NATIVE_FAMILY)
    old, native = original.observations(tmp_path, unchanged_old_source, {
        'ruleset': ['A'], 'checks': [{'name': 'A', 'bucket': 'pass'}],
        '_after_approval': after,
    }, kind=kind)
    assert old['grant'] is not None
    assert native['grant'] == native['grant_before_external_change'] is not None
    assert old['window']['deadline'] == native['waits'][0]['deadline'] == 100.25 + budget
    assert old['pr_state'] == native['pr_state'] == 'OPEN'
    assert old['target_sha'] == old['initial_base']
    assert native['target_sha'] == native['initial_base']
    assert 1 not in old['closed_issues']
    assert not [w for w in native['writes'] if w['operation'] in {'merge', 'close'} and w['subject'] == native['status']['subject']]
    assert native['query']['status'] == old['query']['status']


# V2 replaces only the invalid old-formal-pending assertion. Production source,
# the fixed02 child and every other V1 contract remain unchanged.
OLD_AGGREGATION_DIAGNOSTIC = OLD_PREFIX + r'''
from agent_run.external_supervision import ExternalSupervisor, supervision_scope
from agent_run.required_checks_observation import read_required_checks_observation, derive_required_checks_result
reads = []
def recorded_snapshot(self, pr_number, *, expected_head_sha):
    live = self.live_pull_request(pr_number)
    bind({**live, 'number': pr_number})
    official = GhGitHubPublisher(self.data['repository'], self.git)
    before = official.live_pull_request(pr_number)
    snapshot = official.required_checks_snapshot(pr_number, expected_head_sha=expected_head_sha)
    after = official.live_pull_request(pr_number)
    reads.append({'expected': expected_head_sha, 'before': before, 'after': after, 'snapshot': snapshot})
    return snapshot
Publisher.required_checks_snapshot = recorded_snapshot
with supervision_scope(ExternalSupervisor(now=lambda: 100.25, sleeper=lambda _: None)):
    engine.deliver(rid)
durable = states.load_run(rid)
job = durable['ticket_jobs']['2']
validation_error = None
try:
    read_required_checks_observation(publisher, job['pr_number'], expected_head_sha=job['publication_sha'])
except ValueError as error:
    validation_error = str(error)
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    code = cli.main(['status', rid, '--state-dir', str(states.root), '--json'])
print(json.dumps({'status': durable['status'], 'checks_evidence': job.get('required_checks_evidence'),
 'diagnostics': durable['diagnostics'], 'publication_head': job['publication_sha'],
 'source_ref': job['ticket_branch'], 'source_head': git.resolve(job['ticket_branch']),
 'reads': reads, 'validation_error': validation_error,
 'consumer_derived': derive_required_checks_result(reads[0]['snapshot']['checks']),
 'query_code': code, 'query_stdout': stream.getvalue()}))
'''

NATIVE_AGGREGATION_ACCEPTANCE = NATIVE_PREFIX.replace(
    'observations.append(asdict(checks))',
    'observations.append(asdict(checks))\n            captured_readers.append((reader, pr.number, request.check_head))',
) + """
from support.offline import _physical_read
from agent_run.errors import GitHubReadError
captured_readers = []
for _ in range(80):
    step = s.runner().run_once()
    if step.call is not None:
        s.runner().authorize_agent(step.call)
        s.kernel.receive(step.call, s.agent(step.call))
    if observations and step.kind in {'waiting', 'paused'}:
        break
else:
    raise AssertionError('No normal check boundary')
status = s.kernel.status(s.delivery.key)
with s.kernel.storage.transaction(write=False) as tx:
    waits = [dict(row) for row in tx.all('SELECT * FROM waits')]
import agent_run.queries.run as query_clock
query_clock.time.time = lambda: now[0]
qio = io.StringIO()
with contextlib.redirect_stdout(qio):
    code = main(['status', s.spec.run_id, '--state-dir', str(s.kernel.storage.path), '--json'])
now[0] = 700.25
later = s.runner().run_once()
after_poll = s.kernel.status(s.delivery.key)
reader, pr_number, expected = captured_readers[-1]
original_api = api.read_bytes()
stale = json.loads(original_api)
stale['pr']['head']['sha'] = stale['pr']['base']['sha']
assert stale['pr']['head']['sha'] != expected
api.write_text(json.dumps(stale))
guard_error = None
try:
    read_required_check_facts(reader, pr_number, expected_head_sha=expected)
except GitHubReadError as error:
    guard_error = error.code
finally:
    api.write_bytes(original_api)
restored = normalize_required_checks(read_required_check_facts(reader, pr_number, expected_head_sha=expected))
status_after_guard = s.kernel.status(s.delivery.key)
observation = json.loads(observations[-1]['logs'])['observation']
print(json.dumps({'status': status, 'after_poll': after_poll, 'status_after_guard': status_after_guard,
 'checks': observations[-1], 'waits': waits,
 'head_identity': {'expected': expected, 'before': observation['pr_before']['head']['sha'],
                   'after': observation['pr_after']['head']['sha']},
 'remote_source_head': s.port.remote_commit(s.delivery.source_ref).sha,
 'at_ten_minutes': {'kind': later.kind, 'reason': later.reason},
 'guard_error': guard_error, 'guard_restored_result': restored.result,
 'role_calls': [{'role': call['role'], 'purpose': call['purpose']} for call in s.role_calls],
 'writes': s.port.writes(), 'query_code': code, 'query': json.loads(qio.getvalue())}))
"""
