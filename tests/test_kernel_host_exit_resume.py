"""Core receipt contracts; synthetic Host witnesses are not physical E2E proof."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, Execution, HostObservation, Policy, RunSpec
from support.core_operations import native_agent_fixture, prepare_scripted_agent
from support.offline import Scenario


def managed_run(scenario, tmp_path):
    kernel = scenario.kernel
    kernel.control('stop', 'retire-fixture')
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control('resume', 'prepare-managed')
    host = kernel.admit_host('original-host', kind='run', command=('controlled-host',),
        environment_carrier=tmp_path/'carrier', preflight=lambda: None)
    assert kernel.dispatch_host(host)
    running = HostObservation('running', host.unit_name, 'original', '/host', 'boot', 42, 'host-start', usage_ready=True)
    assert kernel.observe_host(host, running) == 'running'
    execution = Execution(host.run_id, host.owner, host.epoch)
    scenario.execution = execution
    runner = OperationRunner(kernel, execution, scenario.make_ports)
    return kernel, host, running, runner


def interrupted_run(tmp_path, *, count=2, admitted=True, bound=True):
    # Original Core admissions and synthetic process witnesses need no Git producer.
    deliveries = []
    for number in range(2, 2 + count):
        checkout = tmp_path / f'work-{number}'
        checkout.mkdir()
        deliveries.append(Delivery(f'ticket:{number}', DeliveryKind.TICKET, number, 'Original',
            Commit('a' * 40, 'b' * 40), 'refs/heads/work', 'refs/heads/target', checkout=str(checkout)))
    kernel, ports = native_agent_fixture(tmp_path / 'state' / 'kernel.sqlite3', RunSpec(
        'host-recovery', 'example/repository', 1, 'parent', 'graph', tuple(deliveries),
        policy=Policy(concurrency=count)))
    scenario = SimpleNamespace(kernel=kernel, requirements=ports.requirements,
        execution=kernel.claim_executor('fixture'), make_ports=lambda _: ports)
    kernel, host, running, runner = managed_run(scenario, tmp_path)
    requests, calls = [], []
    for _ in range(30):
        step = runner.run_once(agent_capacity=count)
        if step.preparation is None:
            continue
        request = step.preparation
        requests.append(request)
        if admitted:
            call = prepare_scripted_agent(kernel, request).call
            assert call is not None and kernel.dispatch_agent(call)
            kernel.bind_thread(call, 'original-thread-'+call.subject)
            if bound:
                kernel.agent_started(call, {'pid': 100+len(calls), 'start_token': 'unit-worker-'+call.subject,
                    'pgid':100+len(calls),'host_pid':42,'host_start_token':'host-start','boot_id':'boot',
                    'host_cgroup':'/host','cgroup':'/host/workers'})
                kernel.agent_source_ready(call)
            calls.append(call)
        if len(requests)==count:
            break
    assert len(requests)==count
    exited = replace(running, disposition='exited', exit_status=137, ended_at=time.time(), usage_released=True)
    return scenario, kernel, runner, host, running, exited, requests, calls


def test_exact_host_exit_allows_one_run_resume_without_an_operator_stop(tmp_path):
    scenario, kernel, runner, host, running, exited, requests, calls = interrupted_run(tmp_path)
    budgets = {call.subject:kernel.status(call.subject)['budgets'] for call in calls}
    assert kernel.observe_host(host, exited)=='exited'
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM leases')
    assert kernel.resume_subject() is None
    assert kernel.control('resume', 'first-public-resume-scope')=='applied'
    assert kernel.control('resume', 'first-public-resume-scope')=='applied'
    assert kernel.observe_host(host,exited)=='exited'
    with pytest.raises(KernelError,match='no_resumable_boundary'):
        kernel.control('resume','duplicate-exit-is-not-new-authority')
    for call in calls:
        assert kernel.status(call.subject)['budgets']==budgets[call.subject]
        assert kernel.status(call.subject)['gates']==[]
    next_host=kernel.admit_host('replacement-host',kind='run',command=('controlled-host',),
        environment_carrier=tmp_path/'next-carrier',preflight=lambda:None)
    assert kernel.dispatch_host(next_host)
    assert kernel.observe_host(next_host, replace(running,unit_name=next_host.unit_name,invocation_id='replacement',
        main_pid=43,start_token='next-start'))=='running'
    new_runner=OperationRunner(kernel,Execution(next_host.run_id,next_host.owner,next_host.epoch),scenario.make_ports)
    resumed=[]
    for _ in range(30):
        step=new_runner.run_once(agent_capacity=2)
        if step.preparation is not None:
            resumed.append(step.preparation)
        if len(resumed)==2:break
    assert len(resumed)==2
    assert {(r.attempt_id,r.thread_id,r.reported_thread) for r in resumed}=={
        (c.attempt_id,c.thread_id,'original-thread-'+c.subject) for c in calls}
    assert all(kernel.status(c.subject)['budgets']==budgets[c.subject] for c in calls)


@pytest.mark.parametrize('boundary', ['unknown', 'foreign', 'usage-pending', 'unbound-worker'])
def test_unproved_or_unreleased_host_cannot_form_a_run_resume_boundary(tmp_path, boundary):
    _, kernel, _, host, running, exited, requests, calls = interrupted_run(tmp_path, count=1, bound=boundary!='unbound-worker')
    before=kernel.status(calls[0].subject)['budgets']
    if boundary=='unknown':
        assert kernel.observe_host(host, replace(running,disposition='unknown'))=='unknown'
    elif boundary=='foreign':
        assert kernel.observe_host(host, replace(exited,invocation_id='foreign'))=='unknown'
    elif boundary=='usage-pending':
        assert kernel.observe_host(host,replace(exited,usage_released=False))=='exited'
    else:
        assert kernel.observe_host(host,exited)=='exited'
    with pytest.raises(KernelError):
        kernel.control('resume','must-not-recover-unknown')
    with pytest.raises(KernelError):
        kernel.admit_host('must-not-replace-unknown',kind='run',command=('controlled-host',),
            environment_carrier=tmp_path/'blocked-carrier',preflight=lambda:None)
    assert kernel.status(calls[0].subject)['budgets']==before
    if boundary=='usage-pending':
        assert kernel.observe_host(host,exited)=='exited'
        assert kernel.resume_subject() is None
        assert kernel.control('resume','after-accurate-usage-release')=='applied'


@pytest.mark.parametrize('local', ['stop', 'human', 'failure', 'requirements'])
def test_run_host_recovery_preserves_unrelated_local_and_graph_boundaries(tmp_path, local):
    scenario,kernel,runner,host,_,exited,requests,calls=interrupted_run(tmp_path)
    first=calls[0]
    if local=='stop':
        kernel.control('stop','local-stop-must-remain',subject=first.subject)
    elif local in {'human','failure'}:
        raw=b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Original human decision"]}'
        if local=='human':
            kernel.receive(first,raw)
        with kernel.storage.transaction(write=False) as tx:
            import json
            identity=json.loads(tx.get(tx.one('SELECT process_identity FROM invocation_outputs WHERE invocation=?',(first.invocation_id,))[0],first.subject))
        kernel.agent_exited(first,identity,0 if local=='human' else 1,
            failure=None if local=='human' else 'Unrelated original Worker failure',failure_kind='fatal')
        if local=='human':
            for _ in range(10):
                runner.run_once(agent_capacity=0)
                if 'human_blocker' in kernel.status(first.subject)['gates']:break
        assert ('human_blocker' if local=='human' else 'execution_failed') in kernel.status(first.subject)['gates']
    else:
        changed=replace(scenario.requirements,issues=tuple(replace(issue,body='Changed exact requirement')
            if issue.number==2 else issue for issue in scenario.requirements.issues))
        kernel.refresh(lambda:changed)
        assert 'requeue_required' in kernel.status(first.subject)['gates']
    original={call.subject:kernel.status(call.subject)['gates'] for call in calls}
    blocked=kernel.source_status()['blocked']
    budget={call.subject:kernel.status(call.subject)['budgets'] for call in calls}
    assert kernel.observe_host(host,exited)=='exited'
    assert kernel.resume_subject() is None
    assert kernel.control('resume','resume-only-host-interruption')=='applied'
    assert kernel.source_status()['blocked']==blocked
    for call in calls:
        assert kernel.status(call.subject)['gates']==original[call.subject]
        assert kernel.status(call.subject)['budgets']==budget[call.subject]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT resume_action FROM attempts WHERE id=?',(first.attempt_id,))[0] is None


def test_exact_host_exit_recovers_original_preparation_without_invocation_or_charge(tmp_path):
    scenario,kernel,runner,host,running,exited,requests,calls=interrupted_run(tmp_path,admitted=False)
    assert not calls
    assert kernel.observe_host(host,exited)=='exited'
    assert all(kernel.worker_settled(request) for request in requests)
    assert kernel.resume_subject() is None
    assert kernel.control('resume','recover-preparation-only')=='applied'
    for request in requests:
        assert kernel.status(request.subject)['gates']==[]
        assert kernel.status(request.subject)['budgets']['requirements']['developments_used']==0
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM invocations')
        assert not tx.rows('SELECT id FROM leases')
        assert all(tx.one('SELECT resume_action FROM attempts WHERE id=?',(request.attempt_id,))[0]
                   =='recover-preparation-only' for request in requests)


@pytest.mark.parametrize('stage',['approval','complete'])
def test_normal_quiescent_host_exit_creates_no_artificial_resume_boundary(tmp_path,stage):
    scenario=Scenario(tmp_path/'scenario',DeliveryKind.PARENT_ONLY)
    kernel,host,running,_=managed_run(scenario,tmp_path)
    assert scenario.run().kind=='approval'
    if stage=='complete':
        assert scenario.run(approve=True).kind=='complete'
    before=kernel.status(scenario.delivery.key)
    assert kernel.observe_host(host,replace(running,disposition='exited',exit_status=0,
        ended_at=time.time(),usage_released=True))=='exited'
    after=kernel.status(scenario.delivery.key)
    assert after['gates']==before['gates']==[]
    assert after['budgets']==before['budgets']
    with pytest.raises(KernelError,match='control_subject_ambiguous'):
        kernel.resume_subject()


def test_host_recovery_reads_original_unknown_write_before_any_new_write(tmp_path,monkeypatch):
    scenario=Scenario(tmp_path/'scenario',DeliveryKind.PARENT_ONLY)
    kernel,host,running,_=managed_run(scenario,tmp_path)
    now=[kernel.clock()]
    kernel.clock=lambda:now[0]
    scenario.port.lose_response.add('push')
    assert scenario.run().kind=='waiting'
    original=kernel.status(scenario.delivery.key)['pending_write']
    assert original['operation']=='push' and original['status']=='sent'
    with kernel.storage.transaction(write=False) as tx:
        original_payload=tx.one('SELECT payload_id,attempts FROM effects WHERE id=?',(original['id'],))
        assert original_payload['attempts']==1
        payload=original_payload['payload_id']
        windows=[dict(row) for row in tx.all('SELECT scope_id,kind,started_at,deadline FROM waits')]
    assert kernel.observe_host(host,replace(running,disposition='exited',exit_status=137,
        ended_at=time.time(),usage_released=True))=='exited'
    assert kernel.resume_subject() is None
    assert kernel.control('resume','recover-original-sent-write')=='applied'
    assert kernel.status(scenario.delivery.key)['pending_write']==original
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(row) for row in tx.all('SELECT scope_id,kind,started_at,deadline FROM waits')]==windows
    next_host=kernel.admit_host('reconcile-host',kind='run',command=('controlled-host',),
        environment_carrier=tmp_path/'reconcile-carrier',preflight=lambda:None)
    assert kernel.dispatch_host(next_host)
    assert kernel.observe_host(next_host,replace(running,unit_name=next_host.unit_name,
        invocation_id='replacement',main_pid=43,start_token='next-start'))=='running'
    scenario.execution=Execution(next_host.run_id,next_host.owner,next_host.epoch)
    runner=scenario.runner()
    observed=[]
    reconcile=runner.ports.reconcile
    def read_original(effect):
        observed.append(effect)
        return reconcile(effect)
    monkeypatch.setattr(runner.ports,'reconcile',read_original)
    monkeypatch.setattr(runner.ports,'execute',lambda *_args:pytest.fail('fresh writes cannot precede original reconciliation'))
    runner.activate_observation()
    for _ in range(3):
        step=runner.run_once()
        if observed:
            break
        assert step.kind=='waiting' and step.retry_at is not None,step
        now[0]=step.retry_at
    assert step.kind=='progress',(step,observed)
    assert len(observed)==1 and observed[0].effect_id==original['id'] and observed[0].dispatch_serial==1
    with kernel.storage.transaction(write=False) as tx:
        row=tx.one('SELECT status,attempts,payload_id FROM effects WHERE id=?',(original['id'],))
        assert row['status']=='observed' and row['attempts']==1 and row['payload_id']==payload


def test_resume_selector_is_not_cached_host_admission_permission(tmp_path):
    _,kernel,_,host,_,exited,_,calls=interrupted_run(tmp_path,count=1)
    assert kernel.observe_host(host,exited)=='exited'
    assert kernel.resume_subject() is None
    replacement=kernel.admit_host('interleaved-host',kind='run',command=('controlled-host',),
        environment_carrier=tmp_path/'interleaved-carrier',preflight=lambda:None)
    before=kernel.status(calls[0].subject)
    with pytest.raises(KernelError,match='executor_exit_unproven'):
        kernel.control('resume','stale-selector-is-not-a-grant')
    assert kernel.host_request('interleaved-host')==replacement
    assert kernel.status(calls[0].subject)==before


def test_durable_original_raw_is_consumed_before_any_replacement_invocation(tmp_path):
    scenario,kernel,_,host,running,exited,requests,calls=interrupted_run(tmp_path,count=1)
    original=calls[0]
    raw=b'{"result_kind":"development","summary":"Original durable result","human_blockers":null}'
    kernel.receive(original,raw)
    assert kernel.observe_host(host,exited)=='exited'
    assert kernel.resume_subject() is None
    assert kernel.control('resume','consume-original-raw-first')=='applied'
    replacement=kernel.admit_host('raw-reconcile-host',kind='run',command=('controlled-host',),
        environment_carrier=tmp_path/'raw-carrier',preflight=lambda:None)
    assert kernel.dispatch_host(replacement)
    assert kernel.observe_host(replacement,replace(running,unit_name=replacement.unit_name,
        invocation_id='replacement',main_pid=43,start_token='next-start'))=='running'
    runner=OperationRunner(kernel,Execution(replacement.run_id,replacement.owner,replacement.epoch),scenario.make_ports)
    step=runner.run_once()
    assert step.kind=='progress' and step.preparation is None
    assert kernel.status(original.subject)['phase']=='candidate'
    assert kernel.status(original.subject)['budgets']['requirements']['developments_used']==1
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0]==1
        row=tx.one('SELECT status,result_id,thread_id FROM attempts WHERE id=?',(original.attempt_id,))
        assert row['status']=='consumed' and row['thread_id']==original.thread_id
        assert tx.get(row['result_id'],original.subject)==raw


def test_host_exit_does_not_block_continuation_of_an_already_approved_final_action(tmp_path):
    scenario=Scenario(tmp_path/'scenario',DeliveryKind.PARENT_ONLY)
    kernel,host,running,_=managed_run(scenario,tmp_path)
    assert scenario.run().kind=='approval'
    assert kernel.control('approve','original-final-grant',subject=scenario.delivery.key,world=scenario.world())=='pending'
    for _ in range(5):
        scenario.step()
        pending=kernel.status(scenario.delivery.key)['pending_write']
        if pending and pending['operation']=='merge':
            break
    assert pending and pending['operation']=='merge'
    with kernel.storage.transaction(write=False) as tx:
        grant=tx.one('SELECT grant_id FROM subjects WHERE id=?',(scenario.delivery.key,))[0]
    assert kernel.observe_host(host,replace(running,disposition='exited',exit_status=137,
        ended_at=time.time(),usage_released=True))=='exited'
    assert kernel.status(scenario.delivery.key)['gates']==[]
    # This is continuation of the existing noncancellable approved Action,
    # not a new Resume/approval or an implicit replacement grant.
    replacement=kernel.admit_host('approved-continuation-host',kind='run',command=('controlled-host',),
        environment_carrier=tmp_path/'approved-carrier',preflight=lambda:None)
    assert kernel.dispatch_host(replacement)
    assert kernel.observe_host(replacement,replace(running,unit_name=replacement.unit_name,
        invocation_id='replacement',main_pid=43,start_token='next-start'))=='running'
    scenario.execution=Execution(replacement.run_id,replacement.owner,replacement.epoch)
    assert scenario.run().kind=='complete'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM grants')[0]==1
        assert tx.one('SELECT action FROM grants WHERE id=?',(grant,))[0]=='original-final-grant'
        assert tx.one('SELECT status FROM actions WHERE id=\'original-final-grant\'')[0]=='complete'


def test_failed_executor_preserves_each_preparation_owner_for_one_run_resume(tmp_path):
    scenario,kernel,_,host,_,exited,requests,calls=interrupted_run(tmp_path,admitted=False)
    assert not calls and {request.subject for request in requests}=={'ticket:2','ticket:3'}
    before={request.subject:kernel.status(request.subject)['budgets'] for request in requests}
    with kernel.executor_scope(scenario.execution) as close_admission:
        close_admission(OSError('controlled Host preparation failure'))
    with kernel.storage.transaction(write=False) as tx:
        original=[dict(row) for row in tx.all('SELECT g.subject,g.evidence,m.subject AS evidence_owner,m.body '
            'FROM gates g JOIN material m ON m.id=g.evidence WHERE g.code=\'worker_unavailable\' ORDER BY g.subject')]
        assert len(original)==2
    assert kernel.observe_host(host,exited)=='exited'
    assert all(kernel.worker_settled(request) for request in requests)
    assert kernel.resume_subject() is None
    assert kernel.control('resume','resume-all-original-preparations')=='applied'
    for request in requests:
        assert kernel.status(request.subject)['gates']==[]
        assert kernel.status(request.subject)['budgets']==before[request.subject]
        with kernel.storage.transaction(write=False) as tx:
            row=tx.one('SELECT resume_action,thread_id FROM attempts WHERE id=?',(request.attempt_id,))
            assert row['resume_action']=='resume-all-original-preparations'
            assert row['thread_id']==request.thread_id
    assert all(row['subject']==row['evidence_owner'] for row in original)
    assert len({row['evidence'] for row in original})==2
    assert original[0]['body']==original[1]['body']
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM invocations')
        assert not tx.rows('SELECT id FROM leases')
        assert not tx.rows('SELECT scope_id FROM waits WHERE kind=\'agent_preparation\'')
        assert tx.one('SELECT executor FROM runs')[0] is None
