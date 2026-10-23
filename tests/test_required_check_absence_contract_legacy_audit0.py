"""Focused compatibility contracts for unchanged old and current check consumers.

Only this audit-owned file is added. All API responses come from local fake gh.
Old Runs are created through Controller.start + real delivery engines in an
isolated fixed-02 child, then read by the formal old CLI. Current Runs use the
real source adapter and OperationRunner.run_once, then the native public CLI.
No prepared DB, producer/oracle, production source or credentials are edited.

Six cases intentionally fail on 9ec73691: public pending result, premature CI
repair, and the unknown-state deadline. They are not hidden behind xfail.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

OLD_REF = "02b008e43386a73c4510250732a7216d7dba17a5"
START = 100.25

FAKE_GH = r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
data = json.loads(Path(os.environ['CHECK_AUDIT_API']).read_text())
pr = data['pr']
repo = 'example/project'
with Path(os.environ['CHECK_AUDIT_CALLS']).open('a') as log:
    log.write(json.dumps(args) + '\n')
if args[:2] == ['api', '--hostname']:
    args = args[3:]
    args = ['api', *args]
if args[:2] == ['api', 'user']:
    print(json.dumps({'login': 'publisher'}))
elif args[:2] == ['repo', 'view']:
    print(json.dumps({'nameWithOwner': repo, 'defaultBranchRef': {'name': 'main', 'target': {'oid': pr['base']['sha']}}}))
elif args[:2] == ['pr', 'view']:
    print(json.dumps({'headRefName': pr['head']['ref'], 'headRefOid': pr['head']['sha'], 'headRepository': {'nameWithOwner': repo}, 'baseRefName': pr['base']['ref'], 'baseRefOid': pr['base']['sha'], 'mergeable': 'MERGEABLE', 'state': 'OPEN', 'mergeCommit': None}))
elif args[:2] == ['pr', 'checks']:
    required = '--required' in args
    print(json.dumps(data['checks'] if required else data.get('all_checks', data['checks'])))
    print(data.get('checks_stderr', '') if required else '', file=sys.stderr)
    raise SystemExit(data.get('checks_exit', 0) if required else 0)
elif args[:1] == ['api'] and args[1].startswith('repos/' + repo + '/pulls/'):
    print(json.dumps(pr))
elif args[:1] == ['api'] and args[1].startswith('repos/' + repo + '/commits/'):
    print(json.dumps({'sha': pr['base']['sha']}))
elif args[:2] == ['api', 'repos/' + repo + '/rulesets']:
    print(json.dumps([[{'id': 1, 'target': 'branch', 'enforcement': 'active'}]] if data.get('ruleset') else [[]]))
elif args[:2] == ['api', 'repos/' + repo + '/rulesets/1']:
    print(json.dumps({'id': 1, 'rules': [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': c, 'integration_id': None} for c in data.get('ruleset', [])]}}]}))
elif args[:1] == ['api'] and args[1].endswith('/protection/required_status_checks/contexts'):
    print(json.dumps(data.get('classic', [])))
else:
    raise SystemExit('Unexpected LOCAL fake-gh request: ' + repr(args))
"""

OLD_CALLER = r"""
import json, os, subprocess, sys, contextlib, io
from pathlib import Path
from agent_run.github_publish import GhGitHubPublisher
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.git import GitRepository
from agent_run.controller import Controller
from agent_run.state import StateStore
from agent_run.delivery import TicketDeliveryEngine
from agent_run.parent_delivery import ParentDeliveryEngine
from support.delivery_agents import PassAgents, issue
from agent_run import cli
req = json.load(sys.stdin)
root = Path(req['root'])
root.mkdir()
os.chdir(root)
for args in [('init', '-b', 'main'), ('config', 'user.name', 'Audit Fixture'), ('config', 'user.email', 'audit@example.invalid'), ('config', 'maintenance.auto', 'false'), ('config', 'gc.auto', '0')]:
    subprocess.run(['git', *args], check=True, capture_output=True)
(root / 'base.txt').write_text('base\n')
subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
subprocess.run(['git', 'commit', '-m', 'base'], check=True, capture_output=True)
git = GitRepository(root)
base = git.resolve('HEAD')
api = Path(os.environ['CHECK_AUDIT_API'])
payload = json.loads(api.read_text())

def bind(pr):
    payload['pr'] = {'number': pr.get('number', 17), 'state': 'open', 'title': 'Original PR', 'body': 'Original body', 'user': {'login': 'publisher'}, 'merged': False, 'merge_commit_sha': None, 'head': {'ref': pr.get('head_branch', 'source'), 'sha': pr.get('head_sha', base), 'repo': {'full_name': 'example/project'}}, 'base': {'ref': pr.get('base_branch', 'main'), 'sha': pr.get('base_sha', base), 'repo': {'full_name': 'example/project'}}}
    api.write_text(json.dumps(payload))
bind({})
if req['operation'] == 'adapter':
    p = GhGitHubPublisher('example/project', git)
    print(json.dumps(p.required_checks_snapshot(17, expected_head_sha=base)))
    raise SystemExit
fixture = root / 'github.json'
fixture.write_text(json.dumps({'repository': 'example/project', 'default_branch': 'main', 'default_head_sha': base, 'parent': {'number': 1, 'title': 'Parent', 'body': 'Deliver Ticket', 'sub_issues': [] if req.get('kind') == 'parent' else [2], 'sub_issue_order_reliable': True}, 'issues': {} if req.get('kind') == 'parent' else {'2': issue(2)}}))

class Publisher(FixtureGitHubPublisher):

    def required_checks_snapshot(self, pr_number, *, expected_head_sha):
        pr = self.live_pull_request(pr_number)
        bind({**pr, 'number': pr_number})
        return GhGitHubPublisher(self.data['repository'], self.git).required_checks_snapshot(pr_number, expected_head_sha=expected_head_sha)
states = StateStore(root / '.agent-run')
state, reused = Controller(FixtureGitHubReader(fixture), git, states).start(1)
rid = state['run_id']
kind = req.get('kind', 'ticket')
checkout = states.root / 'worktrees' / rid / ('parent' if kind == 'parent' else 'ticket-2')
agents = PassAgents(checkout)
publisher = Publisher(fixture, git)
engine = (ParentDeliveryEngine if kind == 'parent' else TicketDeliveryEngine)(git=git, states=states, github=publisher, agents=agents)
from agent_run.external_supervision import ExternalSupervisor, supervision_scope
with supervision_scope(ExternalSupervisor(now=lambda: 100.25, sleeper=lambda delay: None)):
    result = engine.deliver(rid)
before_approve = {'status': result['status'], 'phase': result.get('parent_job', {}).get('phase')}
approval_error = None
if req.get('approve') and kind == 'parent':
    try:
        with supervision_scope(ExternalSupervisor(now=lambda: 100.25, sleeper=lambda delay: None)):
            result = engine.approve(rid)
    except ValueError as error:
        approval_error = str(error)
durable = states.load_run(rid)
owner = durable['parent_job'] if kind == 'parent' else durable['ticket_jobs']['2']
import agent_run.external_supervision as query_clock
query_clock.monotonic = lambda: 100.25
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    code = cli.main(['status', rid, '--state-dir', str(states.root), '--json'])
from copy import deepcopy
from agent_run.external_supervision import ExternalSupervisor
retry_state = deepcopy(durable)
sleeps = []
first_retry = ExternalSupervisor(now=lambda: 100.25, sleeper=sleeps.append).before_retry(retry_state)
expired = deepcopy(durable)
at_ten = ExternalSupervisor(now=lambda: 700.25, sleeper=lambda delay: None).before_retry(expired)
print(json.dumps({'first_retry_sleeps': sleeps, 'allows_retry_at_ten_minutes': at_ten, 'status_at_ten_minutes': expired['status'], 'status': result['status'], 'phase': owner['phase'], 'checks': owner.get('required_checks_evidence'), 'ci': owner.get('ci_recovery'), 'supervision': durable.get('supervision_window'), 'diagnostics': durable.get('diagnostics'), 'events': agents.events, 'before_approve': before_approve, 'approval_grant': owner.get('approval_grant'), 'approval_error': approval_error, 'query_code': code, 'query_stdout': stream.getvalue(), 'mutations': json.loads(fixture.read_text()).get('delivery', {}).get('mutations', [])}))
"""

NATIVE_CALLER = r"""
import json, os, sys, io, contextlib
from pathlib import Path
from dataclasses import replace, asdict
from agent_run.github import GhGitHubReader
from agent_run_kernel import Issue, ParentIssue, Requirements, Kernel
from support.offline import GraphScenario
from agent_run_kernel.source_reading import read_required_check_facts, normalize_required_checks
from agent_run_kernel.query_cli import main
req = json.load(sys.stdin)
root = Path(req['root'])
root.mkdir()
api = Path(os.environ['CHECK_AUDIT_API'])
payload = json.loads(api.read_text())
kind = req.get('kind', 'ticket')
source = Requirements(ParentIssue(1, 'Parent', 'Deliver requested changes', (2,) if kind == 'ticket' else ()), (Issue(2, 'Ticket', 'Implement requested changes', 'OPEN', frozenset({'ready-for-agent'})),) if kind == 'ticket' else ())
s = GraphScenario(root / 'scenario', source)
now = [100.25]
s.kernel.clock = lambda: now[0]
base_make = s.make_ports
observations = []

def selected(binding):
    p = base_make(binding)
    original = p.delivery

    def delivery(request):
        facts = original(request)
        if request.check_head is not None and facts.pr is not None:
            pr = facts.pr
            payload['pr'] = {'number': pr.number, 'state': 'open', 'title': pr.title, 'body': pr.body, 'user': {'login': 'offline-publisher'}, 'merged': False, 'merge_commit_sha': None, 'head': {'ref': pr.source_ref.removeprefix('refs/heads/'), 'sha': pr.head.sha, 'repo': {'full_name': pr.head_repository}}, 'base': {'ref': pr.target_ref.removeprefix('refs/heads/'), 'sha': pr.base.sha, 'repo': {'full_name': pr.repository}}}
            api.write_text(json.dumps(payload))
            reader = GhGitHubReader(pr.repository, working_directory=binding.repository_root, runner_identity=binding.inputs.runner_identity)
            checks = normalize_required_checks(read_required_check_facts(reader, pr.number, expected_head_sha=request.check_head))
            facts = replace(facts, checks=checks)
            observations.append(asdict(checks))
        return facts
    p.delivery = delivery
    return p
s.make_ports = selected
for _ in range(80):
    step = s.runner().run_once()
    status = s.kernel.status(s.delivery.key)
    if step.call is not None:
        s.runner().authorize_agent(step.call)
        s.kernel.receive(step.call, s.agent(step.call))
    if observations and (step.kind in {'waiting', 'paused', 'approval', 'complete'} or status['purpose'] == 'ci' or status['phase'] in {'approval', 'merge'}):
        break
else:
    raise AssertionError('No native checks boundary')
status = s.kernel.status(s.delivery.key)
window = status.get('read_waits')
approval_error = None
if req.get('approve') and kind == 'parent':
    try:
        s.kernel.control('approve', 'audit-approval', subject=s.delivery.key, world=s.kernel.observe_control(s.delivery.key, s.make_ports))
    except Exception as error:
        approval_error = getattr(error, 'code', str(error))
from agent_run_kernel import query as query_clock
query_clock.time.time = lambda: now[0]
qio = io.StringIO()
with contextlib.redirect_stdout(qio):
    code = main(['status', s.spec.run_id, '--state-dir', str(s.kernel.storage.path), '--json'])
with s.kernel.storage.transaction(write=False) as tx:
    waits = [dict(x) for x in tx.all('SELECT * FROM waits')]
    grants = tx.one('SELECT count(*) FROM grants')[0]
now[0] = 700.25
later = s.runner().run_once() if waits else None
print(json.dumps({'grants': grants, 'at_ten_minutes': None if later is None else {'kind': later.kind, 'reason': later.reason}, 'status': status, 'step': {'kind': step.kind, 'reason': step.reason, 'retry_at': step.retry_at}, 'checks': observations[-1] if observations else None, 'waits': waits, 'query_code': code, 'query': json.loads(qio.getvalue()), 'role_calls': s.role_calls, 'approval_error': approval_error}))
"""


@pytest.fixture(scope="session")
def unchanged_old_source(tmp_path_factory):
    destination = tmp_path_factory.mktemp("required-check-fixed-02")
    repo = Path(__file__).resolve().parents[2]
    archived = subprocess.run(
        ["git", "-C", str(repo), "archive", OLD_REF, "project/src", "project/tests/support"],
        check=True, capture_output=True, timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
        for member in archive:
            path = destination / member.name
            assert path.resolve().is_relative_to(destination.resolve())
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile()
                stream = archive.extractfile(member)
                assert stream is not None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(stream.read())
    return destination, source_digests(destination)


def source_digests(source):
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*") if p.is_file()}


def observations(tmp_path, original, payload, *, kind="ticket", approve=False):
    source, digests = original
    assert source_digests(source) == digests
    api = tmp_path / "api.json"
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(0o700)
    # The normal autouse environment fixture already isolates HOME/XDG/Git.
    # Remove ambient credential names before either local fake-gh child runs.
    environment = {k: v for k, v in os.environ.items()
                   if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}}
    environment.update(PATH=str(binary) + os.pathsep + environment["PATH"],
                       CHECK_AUDIT_API=str(api), CHECK_AUDIT_CALLS=str(tmp_path / "calls.jsonl"),
                       PYTHONDONTWRITEBYTECODE="1")
    results = []
    for mode, script in (("old", OLD_CALLER), ("native", NATIVE_CALLER)):
        api.write_text(json.dumps({"pr": {}, "classic": [], **payload}))
        pythonpath = (os.pathsep.join(str(source / x) for x in ("project/src", "project/tests"))
                      if mode == "old" else str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            input=json.dumps({"root": str(tmp_path / (mode + "-run")), "operation": "engine",
                              "kind": kind, "approve": approve}),
            env={**environment, "PYTHONPATH": pythonpath},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        value = json.loads(result.stdout)
        if mode == "old":
            value["query"] = json.loads(value.pop("query_stdout"))
        assert value["query_code"] == 0
        results.append(value)
    assert source_digests(source) == digests
    (tmp_path / "public_observations.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return results


@pytest.mark.parametrize("configuration", ["ruleset", "classic", "partial-pass"])
def test_unreported_required_context_keeps_public_pending_contract(
    tmp_path, unchanged_old_source, configuration,
):
    payload = ({"classic": ["A"], "checks": [], "checks_exit": 1,
                "checks_stderr": "no required checks reported",
                "all_checks": [{"name": "optional", "bucket": "pass"}]} if configuration == "classic" else
               {"ruleset": ["A", "B"], "checks": [{"name": "A", "bucket": "pass"}]}
               if configuration == "partial-pass" else {"ruleset": ["A"], "checks": []})
    old, current = observations(tmp_path, unchanged_old_source, payload)
    assert old["checks"]["result"] == "pending" and old["status"] == "waiting_checks"
    assert old["query"]["progress"]["multi_ticket"]["tickets"][0]["required_checks"]["result"] == "pending"
    assert old["supervision"]["budget_seconds"] == 2700
    assert old["query"]["supervision"]["remaining_seconds"] == 2700
    assert current["query"]["supervision"]["remaining_seconds"] == 2700
    assert current["status"]["phase"] == "checks" and current["step"]["kind"] == "waiting"
    assert current["waits"][0]["deadline"] - current["waits"][0]["started_at"] == 2700
    assert old["first_retry_sleeps"] == [5] and current["step"]["retry_at"] == START + 5
    assert current["query"]["subjects"][0]["required_checks"]["result"] == old["checks"]["result"], (
        "Configured-but-unreported checks changed the public result from pending to unknown")


@pytest.mark.parametrize("unfinished", ["unreported", "pending"])
def test_ticket_ci_repair_waits_for_other_required_checks(tmp_path, unchanged_old_source, unfinished):
    checks = [{"name": "A", "bucket": "fail", "link": "https://external.example.invalid/check/A"}]
    if unfinished == "pending":
        checks.append({"name": "B", "bucket": "pending"})
    old, current = observations(tmp_path, unchanged_old_source, {"ruleset": ["A", "B"], "checks": checks})
    assert old["checks"]["result"] == current["checks"]["result"] == "fail"
    assert old["status"] == "waiting_external" and old["phase"] == "waiting_checks"
    assert old["ci"]["developments_used"] == 0
    assert old["query"]["progress"]["multi_ticket"]["integration_holder"] == 2
    assert old["diagnostics"][0]["code"] == "github_checks_observation_pending"
    assert current["status"]["purpose"] != "ci", (
        "Native runner queues CI Development while another required check is unfinished")


def test_unknown_bucket_keeps_original_convergence_deadline(tmp_path, unchanged_old_source):
    old, current = observations(tmp_path, unchanged_old_source, {
        "ruleset": ["A"], "checks": [{"name": "A", "bucket": "mystery"}]})
    assert old["checks"]["result"] == current["checks"]["result"] == "unknown"
    assert old["supervision"]["budget_seconds"] == 600
    assert old["allows_retry_at_ten_minutes"] is False
    assert old["status_at_ten_minutes"] == "supervision_timeout"
    assert current["at_ten_minutes"]["reason"] == "supervision_timeout", (
        "Unknown GitHub state changed from 10-minute convergence to 45-minute check wait")


def test_reported_pending_preserves_check_wait_and_retry_controls(tmp_path, unchanged_old_source):
    old, current = observations(tmp_path, unchanged_old_source, {
        "ruleset": ["A"], "checks": [{"name": "A", "bucket": "pending"}]})
    assert old["checks"]["result"] == current["checks"]["result"] == "pending"
    assert old["supervision"]["budget_seconds"] == 2700
    assert current["waits"][0]["deadline"] == START + 2700
    assert old["first_retry_sleeps"] == [5] and current["step"]["retry_at"] == START + 5
    assert old["allows_retry_at_ten_minutes"] is True
    assert current["at_ten_minutes"]["kind"] == "waiting"


def test_unreported_check_refuses_parent_approval_in_both_runtimes(tmp_path, unchanged_old_source):
    old, current = observations(tmp_path, unchanged_old_source, {
        "classic": ["A"], "checks": []}, kind="parent", approve=True)
    assert old["approval_error"] == "Parent-only delivery is not awaiting approval"
    assert old["approval_grant"] is None
    assert current["approval_error"] == "no_approval_boundary" and current["grants"] == 0


def test_no_required_configuration_keeps_parent_approval_eligible(tmp_path, unchanged_old_source):
    old, current = observations(tmp_path, unchanged_old_source, {"checks": []}, kind="parent")
    assert old["checks"]["result"] == current["checks"]["result"] == "none"
    assert old["status"] == "parent_approval_pending" and current["status"]["phase"] == "approval"
    assert old["query"]["operator_action"]["action"]["command"] == "approve"
    assert current["query"]["operator_actions"][0]["action"]["command"] == "approve"
