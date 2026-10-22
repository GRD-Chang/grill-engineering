"""Produce a mixed canonical v6 Run using its unchanged controller and Host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_run.managed_workspace import ManagedWorkspace
from support.frozen_v6_runtime import old_v6_runtime
from support.v6_close_service import close_service_environment


_WHOLE_SOURCE = r'''
import json,sys,os
from datetime import datetime,timezone
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import AgentProfileStore,ProfiledAgentBackend
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.executor_host import ExecutorSpec,FakeExecutorHost
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher,FixtureGitHubReader
from agent_run.state import StateStore,SimulatedProcessCrash
from agent_run.task_control import TaskControlStore
from agent_run.task_identity import TaskKey
from support.cli_payloads import passing_acceptance,publication,final_run_publication
import agent_run.controller
request=json.load(sys.stdin)
frozen,root,output=map(Path,(request['frozen'],request['repository'],request['output']))
assert Path(agent_run.controller.__file__).resolve().is_relative_to(frozen/'project/src')
git=GitRepository(root); states=StateStore(Path(request['state_root']))
fixture=output/'github.json'
fixture.write_text(json.dumps({'repository':'example/project','default_branch':'main',
 'default_head_sha':git.resolve('main'),'parent':{'number':1,'title':'Whole Run',
 'body':'Deliver A, then independent B and C.' if request.get('second_returned') else 'Deliver A, then B, then C.',
 'sub_issues':[2,3,4],'sub_issue_order_reliable':True},
 'issues':{str(n):{'number':n,'title':'Ticket '+str(n),'body':'Deliver feature '+str(n),
 'state':'OPEN','labels':['ready-for-agent'],
 'blocked_by':[] if n==2 else [{'number':2 if request.get('second_returned') else n-1,'state':'OPEN'}]} for n in (2,3,4)}}))
controller=Controller(FixtureGitHubReader(fixture),git,states)
state,reused=controller.start(1); assert not reused
run_id=state['run_id']
agents_file=output/'agents.json'
agents_file.write_text(json.dumps({
 'developments':[{'thread_id':'whole-development-'+str(n),'expected_thread_id':None,
 'summary':'Deliver feature '+str(n),'write_files':{'feature-'+str(n)+'.txt':'done '+str(n)+'\n'}} for n in (2,3,4)],
 'reviews':[passing_acceptance('whole-review-'+str(n),'Feature '+str(n)+' verified') for n in (2,3,4)],
 'publications':[{**publication(),'thread_id':'whole-development-'+str(n)} for n in (2,3,4)],
 'run_reviews':[passing_acceptance('whole-final-review','All three features verified')],
 'run_publications':[final_run_publication()]}))
calls=[]
class Agents(FixtureAgentBackend):
 def develop(self,request):
  calls.append('development'); return super().develop(request)
 def review(self,request):
  calls.append('review'); return super().review(request)
 def publication(self,request):
  calls.append('publication'); return super().publication(request)
backend=ProfiledAgentBackend(Agents(agents_file),AgentProfileStore(states.root),run_id=run_id)
class NumericClosePublisher(FixtureGitHubPublisher):
 # This mechanical service reports valid numeric event identities. It executes
 # the original fixture close first; no ownership is preseeded and no SQL is patched.
 def prepare_primary_ticket_close(self,**arguments):
  intent=super().prepare_primary_ticket_close(**arguments)
  assert intent is not None
  intent['intent_created_at']=datetime.now(timezone.utc).isoformat()
  self._delivery().setdefault('whole_close_intents',{})[str(arguments['ticket_number'])]=dict(intent)
  ticket,run=arguments['ticket_number'],arguments['run_id']
  binding=intent['intent_binding']
  body='\n'.join((f'<!-- agent-run:{run}:ticket-{ticket}:completed -->',
   f'<!-- agent-run:{run}:ticket-{ticket}:publisher-close-intent:{binding} -->',
   f'<!-- agent-run:{run}:ticket-{ticket}:publisher-close-baseline:{binding}:0 -->'))
  self._delivery().setdefault('whole_close_comments',{})[str(ticket)]={
   'id':3000+ticket,'body':body,'user':{'login':intent['actor']},'created_at':intent['intent_created_at']}
  self._save(); return intent
 def close_primary_ticket(self,**arguments):
  ownership=super().close_primary_ticket(**arguments)
  assert ownership is not None
  ticket=arguments['ticket_number']
  ownership={**ownership,'event_id':4100+ticket}
  self._delivery()['ticket_close_ownership'][str(ticket)]=dict(ownership)
  self._delivery().setdefault('whole_close_events',{})[str(ticket)]={
   'id':ownership['event_id'],'event':'closed','actor':{'login':ownership['actor']},
   'created_at':datetime.now(timezone.utc).isoformat()}
  self._save(); return ownership
publisher=NumericClosePublisher(fixture,git)
engine=TicketDeliveryEngine(git=git,states=states,github=publisher,agents=backend)
stop_ticket=3
def stop_after_b_raw():
 saved=StateStore(states.root).load_current_run(run_id)
 job=saved.get('ticket_jobs',{}).get(str(stop_ticket),{})
 pending=job.get('pending_semantic_attempt',{})
 if pending.get('returned_result',{}).get('kind')=='review':
  raise SimulatedProcessCrash('B Reviewer raw durable before consumption')
states._after_run_saved=stop_after_b_raw
task=TaskKey(root,'example/project',1); control=TaskControlStore(states.root)
claim=control.claim_action(task,kind='run',payload={'parent':1})
control.bind_run(task,claim.action_id,run_id,state_dir=states.root)
spec=ExecutorSpec(task,claim.action_id,run_id,claim.action['executor_generation'],cwd=root,state_root=states.root)
def execute_source():
 global stop_ticket
 a=engine.deliver(run_id)
 assert a['ticket_jobs']['2']['phase']=='completed'
 selected,reused=controller.resume(run_id)
 assert reused and selected['active_ticket_job']['ticket_number']==3
 try:
  engine.deliver(run_id)
 except SimulatedProcessCrash as error:
  assert str(error)=='B Reviewer raw durable before consumption'
 else:
  raise AssertionError('original B raw was not retained')
 if request.get('second_returned'):
  stop_ticket=4
  try:
   engine.deliver_ticket(run_id,4)
  except SimulatedProcessCrash:
   pass
  else:
   raise AssertionError('original C raw was not retained')
 control.record_application(task,action_id=claim.action_id,run_id=run_id,
  payload_digest=claim.action['payload_digest'],generation=spec.generation,state_dir=states.root)
 (output/'calls.json').write_text(json.dumps(calls))
 return states.load_current_run(run_id)
def execute():
 try:
  return execute_source()
 except BaseException:
  import traceback
  traceback.print_exc()
  raise
observed=FakeExecutorHost(separate_process=True).ensure(spec,control,execute=execute)
assert observed.status=='exited' and observed.pid
try:
 os.kill(observed.pid,0)
except ProcessLookupError:
 pass
else:
 raise AssertionError('original mixed Host was not reaped')
saved=states.load_current_run(run_id)
assert saved['ticket_jobs']['2']['phase']=='completed'
assert saved['ticket_jobs']['3']['pending_semantic_attempt']['returned_result']['kind']=='review'
assert bool(saved.get('ticket_jobs',{}).get('4',{}).get('pending_semantic_attempt'))==bool(request.get('second_returned'))
assert json.loads((output/'calls.json').read_text())==['development','review','publication','development','review']+(['development','review'] if request.get('second_returned') else [])
a,b=saved['ticket_jobs']['2'],saved['ticket_jobs']['3']
assert b['base_sha']==a['integrated_sha'] and b['base_sha']!=saved['base']['sha']
assert git.resolve(saved['run_branch'])==a['integrated_sha']
report={'run_id':run_id,'default_base':saved['base']['sha'],'run_branch':saved['run_branch'],
 'run_head':git.resolve(saved['run_branch']),'a_integrated':a['integrated_sha'],
 'a_publication':a['publication_sha'],'b_base':b['base_sha'],'b_candidate':b['candidate_sha'],
 'b_branch':b['ticket_branch'],'a_branch':a['ticket_branch'],
 'b_pending_attempt':b['pending_semantic_attempt']['attempt_id'],
 'calls':json.loads((output/'calls.json').read_text()),'second_returned':request.get('second_returned',False)}
(output/'whole-report.json').write_text(json.dumps(report))
print(json.dumps(report))
'''


def whole_run_managed_source(root: Path, repository: Path, *, second_returned: bool = False) -> tuple[ManagedWorkspace, dict[str, Path]]:
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
    environment = close_service_environment(root, {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join(str(frozen / item) for item in ('project/src', 'project/tests'))})
    observed = subprocess.run([sys.executable, '-c', _WHOLE_SOURCE], env=environment,
        input=json.dumps({'frozen': str(frozen), 'repository': str(managed.repository_root),
                          'output': str(output), 'state_root': str(managed.state_root), 'second_returned': second_returned}),
        capture_output=True, text=True, timeout=60)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    report = json.loads(observed.stdout)
    return managed, {'database': managed.state_root / 'state.sqlite3',
        'profile': managed.state_root / 'profiles' / (report['run_id'] + '.json'),
        'github': output / 'github.json', 'report': output / 'whole-report.json', 'frozen': frozen}


_WHOLE_CONTINUE = r'''
import json,sys,subprocess
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import AgentProfileStore,ProfiledAgentBackend
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher,FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
import agent_run.controller
request=json.load(sys.stdin)
root,state,output,frozen=map(Path,(request['repository'],request['state'],request['output'],request['frozen']))
assert Path(agent_run.controller.__file__).resolve().is_relative_to(frozen/'project/src')
run_id=request['run_id']; git=GitRepository(root); states=StateStore(state)
fixture=output/'github.json'
script=json.loads((output/'agents.json').read_text())
script['developments']=script['developments'][2:]
script['reviews']=script['reviews'][2:]
script['publications']=script['publications'][1:]
agents_file=output/'continuation-agents.json'; agents_file.write_text(json.dumps(script))
calls=[]
class Agents(FixtureAgentBackend):
 def develop(self,request):
  assert (Path(request['checkout'])/'feature-2.txt').read_text()=='done 2\n'
  assert (Path(request['checkout'])/'feature-3.txt').read_text()=='done 3\n'
  calls.append('development'); return super().develop(request)
 def review(self,request):
  calls.append('review'); return super().review(request)
 def publication(self,request):
  calls.append('publication'); return super().publication(request)
 def run_publication(self,request):
  calls.append('publication'); return super().run_publication(request)
backend=ProfiledAgentBackend(Agents(agents_file),AgentProfileStore(state),run_id=run_id)
publisher=FixtureGitHubPublisher(fixture,git)
engine=TicketDeliveryEngine(git=git,states=states,github=publisher,agents=backend)
engine.deliver(run_id)
controller=Controller(FixtureGitHubReader(fixture),git,states)
selected,reused=controller.resume(run_id)
assert reused and selected['active_ticket_job']['ticket_number']==4
engine.deliver(run_id)
before_final=states.load_current_run(run_id)
jobs=before_final['ticket_jobs']
assert all(jobs[str(number)]['phase']=='completed' for number in (2,3,4))
assert calls==['publication','development','review','publication']
selected,reused=controller.resume(run_id)
assert reused and not selected.get('active_ticket_job')
default=git.resolve('main')
RunAcceptanceEngine(git=git,states=states,agents=backend,github=publisher,default_head_sha=default).accept(run_id)
publication=RunPublicationEngine(git=git,states=states,agents=backend,github=publisher,
 default_branch='main',default_head_sha=default)
publication.publish(run_id)
publication.approve(run_id)
saved=states.load_current_run(run_id)
assert saved['status']=='completed'
assert calls==['publication','development','review','publication','review','publication']
external=json.loads(fixture.read_text())
assert all(external['issues'][str(number)]['state']=='CLOSED' for number in (2,3,4))
def tree(oid):
 return subprocess.run(['git','-C',str(root),'rev-parse',oid+'^{tree}'],check=True,capture_output=True,text=True).stdout.strip()
print(json.dumps({'calls':calls,'tree':tree(saved['run_publication']['integrated_sha']),
 'tickets':{number:{'budget':[job['review_budget']['development_attempts'],job['review_budget']['reviewer_invocations']],
 'tree':job['integrated_tree'],'development_thread':job['development_thread_id']} for number,job in jobs.items()},
 'mutations':[entry['action'] for entry in external['delivery']['mutations']],
 'remaining_refs':list(external['delivery']['published_branches'])}))
'''


def continue_original_whole_run(managed: ManagedWorkspace, paths: dict[str, Path]) -> dict:
    report = json.loads(paths['report'].read_bytes())
    frozen = paths['frozen']
    observed = subprocess.run([sys.executable, '-c', _WHOLE_CONTINUE],
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
             'PYTHONPATH': os.pathsep.join(str(frozen / item) for item in ('project/src', 'project/tests'))},
        input=json.dumps({'repository': str(managed.repository_root), 'state': str(managed.state_root),
            'output': str(paths['github'].parent), 'frozen': str(frozen), 'run_id': report['run_id']}),
        text=True, capture_output=True, timeout=60)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    return json.loads(observed.stdout)
