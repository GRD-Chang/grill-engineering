"""Original reaped-host role returns; only the observer stops consumption."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_run.managed_workspace import ManagedWorkspace
from support.frozen_v6_runtime import old_v6_runtime
from support.v6_close_service import close_service_environment
from test_kernel_migration_completed_history import _COMPLETE


_RAW = _COMPLETE.replace('def deliver():', '''from agent_run.state import SimulatedProcessCrash
wanted = request['returned_role']
def stop_after_raw():
    saved = StateStore(states.root).load_current_run(run_id)
    pending = saved['ticket_jobs']['2'].get('pending_semantic_attempt', {})
    returned = pending.get('returned_result')
    if returned is not None and returned.get('kind') in ({'publication', 'publication_wire'} if wanted == 'publication' else {wanted}):
        raise SimulatedProcessCrash('original durable raw before consumption')
states._after_run_saved = stop_after_raw
def deliver():''').replace(
    '''    TicketDeliveryEngine(git=git, states=states, github=publisher,
                         agents=backend).deliver(run_id)''',
    '''    try:
        TicketDeliveryEngine(git=git, states=states, github=publisher,
                             agents=backend).deliver(run_id)
    except SimulatedProcessCrash as error:
        assert str(error) == 'original durable raw before consumption'
    else:
        raise AssertionError('original did not retain the requested raw return')''')
_RAW = _RAW[:_RAW.index('after = states.load_current_run(run_id)')] + r'''
after = states.load_current_run(run_id)
job = after['ticket_jobs']['2']
pending = job['pending_semantic_attempt']
assert pending['returned_result']['kind'] in ({'publication', 'publication_wire'} if wanted == 'publication' else {wanted})
assert after['status'] == 'active'
assert calls == ['development', 'review', 'publication'][:{'development':1,'review':2,'publication':3}[wanted]]
report = {'run_id':run_id,'phase':job['phase'],'calls':calls,'pending':pending,
          'budget':job['review_budget'],'candidate_sha':job.get('candidate_sha'),
          'review_findings':request.get('review_findings',False),
          'run_branch':after['run_branch'],'ticket_branch':job['ticket_branch']}
(output/'returned-report.json').write_text(json.dumps(report))
print(json.dumps({'run_id':run_id,'status':after['status']}))
'''


def returned_managed_source(root: Path, repository: Path, role: str, *, review_findings: bool = False,
        segmentless_events: bool = False) -> tuple[ManagedWorkspace, dict[str, Path]]:
    """Run unchanged 02 through a real local Host and keep original SQL/Git."""
    root.mkdir(parents=True, exist_ok=True)
    managed = ManagedWorkspace.for_repository('example/project')
    managed.root.mkdir(parents=True)
    repository.rename(managed.repository_root)
    managed.state_root.mkdir()
    remote = 'https://github.com/example/project.git'
    subprocess.run(['git', '-C', str(managed.repository_root), 'remote', 'add', 'origin', remote],
                   check=True, capture_output=True, timeout=10)
    (managed.root / '.agent-run-workspace.json').write_text(json.dumps({
        'repository': managed.repository, 'remote_url': remote}) + '\n')
    assert managed.open_path() == managed.repository_root
    frozen, output = root / 'unchanged-02', root / 'source'
    old_v6_runtime(frozen)
    output.mkdir()
    environment = close_service_environment(root, {
        **os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join(str(frozen / item) for item in ('project/src', 'project/tests'))})
    observed = subprocess.run([sys.executable, '-c', _RAW], env=environment, text=True,
        input=json.dumps({'frozen': str(frozen), 'repository': str(managed.repository_root),
                          'output': str(output), 'state_root': str(managed.state_root),
                          'rounds': 2 if review_findings else 1, 'controlled': True, 'actual_github_close': False,
                          'review_findings': review_findings,
                          'segmentless_events': segmentless_events,
                          'returned_role': role}), capture_output=True, timeout=45)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    run_id = json.loads(observed.stdout)['run_id']
    return managed, {'database': managed.state_root / 'state.sqlite3',
                     'profile': managed.state_root / 'profiles' / (run_id + '.json'),
                     'github': output / 'github.json', 'report': output / 'returned-report.json',
                     'frozen': frozen}


_CONTINUE = r'''
import json,sys,subprocess
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import AgentProfileStore,ProfiledAgentBackend
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.state import StateStore
import agent_run.delivery
request=json.load(sys.stdin)
frozen=Path(request['frozen'])
assert Path(agent_run.delivery.__file__).resolve().is_relative_to(frozen/'project/src')
root=Path(request['repository']); states=StateStore(Path(request['state']))
run_id=request['run_id']; wanted=request['role']; calls=[]
findings=request.get('review_findings',False)
class Agents(FixtureAgentBackend):
    def develop(self, request):
        assert findings,'the original durable Development raw must not be replayed'
        calls.append('development'); return super().develop(request)
    def review(self, request):
        assert wanted=='development' or findings,'the original Reviewer raw must not be replayed'
        calls.append('review'); return super().review(request)
    def publication(self, request):
        assert wanted!='publication','the original Publication raw must not be replayed'
        calls.append('publication'); return super().publication(request)
agents=Path(request['agents'])
if findings:
    inputs=json.loads(agents.read_text())
    inputs['developments']=inputs['developments'][1:]
    inputs['reviews']=inputs['reviews'][1:]
    agents=agents.with_name('findings-continuation-agents.json')
    agents.write_text(json.dumps(inputs))
backend=ProfiledAgentBackend(Agents(agents),AgentProfileStore(states.root),run_id=run_id)
git=GitRepository(root)
TicketDeliveryEngine(git=git,states=states,github=FixtureGitHubPublisher(Path(request['github']),git),agents=backend).deliver(run_id)
after=states.load_current_run(run_id); job=after['ticket_jobs']['2']
assert after['status']=='ticket_completed' and job['phase']=='completed'
assert calls==(['development','review','publication'] if findings else {'development':['review','publication'],'review':['publication'],'publication':[]}[wanted])
external=json.loads(Path(request['github']).read_text())
assert external['issues']['2']['state']=='CLOSED'
def tree(oid):
 return subprocess.run(['git','-C',str(root),'rev-parse',oid+'^{tree}'],check=True,capture_output=True,text=True).stdout.strip()
print(json.dumps({'calls':calls,'candidate_tree':tree(job['candidate_sha']),
 'integrated_tree':job['integrated_tree'],'budget':[job['review_budget']['development_attempts'],job['review_budget']['reviewer_invocations']],
 'state':external['issues']['2']['state'],'development_thread':job['development_thread_id'],
 'mutations':[item['action'] for item in external['delivery']['mutations']]}))
'''


def continue_original_returned(managed: ManagedWorkspace, paths: dict[str, Path], role: str) -> dict[str, object]:
    report = json.loads(paths['report'].read_text())
    frozen = paths['frozen']
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join(str(frozen / item) for item in ('project/src', 'project/tests'))}
    observed = subprocess.run([sys.executable, '-c', _CONTINUE], env=environment, text=True,
        input=json.dumps({'frozen': str(frozen), 'repository': str(managed.repository_root),
                          'state': str(managed.state_root), 'run_id': report['run_id'], 'role': role,
                          'agents': str(paths['report'].parent / 'agents.json'), 'github': str(paths['github']),
                          'review_findings': report.get('review_findings', False)}),
        capture_output=True, timeout=45)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    return json.loads(observed.stdout)
