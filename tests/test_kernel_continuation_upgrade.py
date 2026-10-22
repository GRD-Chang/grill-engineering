"""Upgrade real v17 native records through the one official v18 DDL path."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from agent_run_kernel import Commit, Execution, Issue, Kernel, ParentIssue, Requirements, World
from agent_run_kernel.attempt_continuation import load_continuation
from agent_run_kernel.storage import PREVIOUS_PROTOCOL, PROTOCOL, Storage, Transaction
from agent_run_kernel.query import read
from test_kernel_exploration import ticket_spec
from support.core_operations import CoreOperations, ReadOnlyPorts
from agent_run_kernel.operation_runner import DeliveryFacts


_NATIVE_V17 = '2d25932862c9faf506fef7c5692e78bfb3e7d9ba'
_PRODUCE = '''
from pathlib import Path
from dataclasses import replace
import json,sys
from agent_run_kernel import Commit,Delivery,DeliveryKind,Kernel,Policy,RunSpec,World
from agent_run_kernel.model import wire_json
from agent_run_kernel.offline import fixture_inputs
r=json.load(sys.stdin)
base=Commit("base","base-tree")
spec=RunSpec("offline-run","example/repository",1,"parent-v1","graph-v1",
    (Delivery("ticket:2",DeliveryKind.TICKET,2,"ticket-v1",base,"refs/heads/ticket-2","refs/heads/run"),),
    Policy(ticket_rounds=1,development_deadline_seconds=7),language="en",inputs=fixture_inputs())
k=Kernel.create(Path(r["path"]),spec)
k.clock=lambda:100.0
e=k.claim_executor("native-v17")
w=World("parent-v1","graph-v1","ticket-v1",base)
c=k.advance("ticket:2",e,w).call
assert c is not None
k.bind_thread(c,"original-backend")
if r["state"]=="failed":
    for n in range(2):
        assert k.dispatch_agent(c)
        p={"pid":23+n,"pgid":23+n,"start_token":"native-start-"+str(n),"host_pid":11,"host_start_token":"host-start"}
        k.agent_started(c,p)
        k.agent_exited(c,p,1)
        if n==0:
            c=k.advance("ticket:2",e,w).call
            assert c is not None
else:
    raw=({"result_kind":"human_blocker","summary":None,"human_blockers":["Choose original formatting."]}
         if r["state"]=="human" else {"result_kind":"development","summary":"Retained native return.","human_blockers":None})
    k.receive(c,wire_json(raw))
    if r["state"] in {"human","consumed"}:
        step=k.advance("ticket:2",e,w)
        assert step.reason=="human_blocker" if r["state"]=="human" else step.kind=="progress"
print(json.dumps({"attempt":c.attempt_id,"thread":c.thread_id,"invocation":c.invocation_id}))
'''


# _PRODUCE remains the original v17 integrity golden. The runnable variant
# executes the same frozen v17 product code with explicit source/root inputs.
_RUNNABLE_SETUP = """
from agent_run_kernel import Issue, ParentIssue, Requirements
requirements=Requirements(ParentIssue(1,"Parent","Original requirement",(2,)),
    (Issue(2,"Ticket","Original behavior","OPEN",frozenset({"ready-for-agent"})),))
final=Delivery("final:1",DeliveryKind.FINAL_RUN,1,"unread",base,"refs/heads/run","refs/heads/main")
spec=replace(spec,repository_root=str(Path(r["path"]).parent.resolve()),deliveries=(*spec.deliveries,final))
k=Kernel.create(Path(r["path"]),spec,requirements=requirements)
"""
_RUNNABLE_PRODUCE = _PRODUCE.replace('k=Kernel.create(Path(r["path"]),spec)', _RUNNABLE_SETUP).replace(
    'w=World("parent-v1","graph-v1","ticket-v1",base)',
    'w=World(k.source_status()["observed_parent_revision"],k.source_status()["observed_graph_revision"],k.delivery("ticket:2").revision,base)')


@pytest.fixture(scope='module')
def native_v17_source(tmp_path_factory):
    root = tmp_path_factory.mktemp('native-v17-continuation')
    archive = subprocess.run(['git', 'archive', _NATIVE_V17, 'project/src'],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as contents:
        for member in contents:
            if not member.isfile():
                continue
            relative = Path(member.name)
            assert not relative.is_absolute() and '..' not in relative.parts
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = contents.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
    return root / 'project/src'


def _produce(source: Path, path: Path, state: str, *, script: str = _PRODUCE):
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps({'path': str(path), 'state': state}),
        env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _schema(path: Path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


@pytest.mark.parametrize('state', ['returned', 'human', 'failed', 'consumed'])
def test_real_native_upgrade_reopen_and_fresh_schema_preserve_exact_facts(tmp_path, native_v17_source, state):
    path = tmp_path / 'native.sqlite3'
    original = _produce(native_v17_source, path, state)
    before_query = path.read_bytes()
    assert read(path, 'status').data['run_id'] == 'offline-run'
    assert path.read_bytes() == before_query
    with Storage(path).transaction(write=False) as tx:
        assert tx.one('SELECT protocol FROM meta')[0] == PREVIOUS_PROTOCOL
        old_context = tx.one('SELECT context_id FROM invocation_outputs WHERE invocation=? ORDER BY ordinal DESC LIMIT 1',
                             (original['invocation'],))[0]
    kernel = Kernel(path)
    with kernel.storage.transaction(write=False) as tx:
        attempt = tx.one('SELECT * FROM attempts WHERE id=?', (original['attempt'],))
        opening = json.loads(tx.get(attempt['opening_id'], 'ticket:2'))
        facts = load_continuation(tx, original['attempt'])
        count = tx.one('SELECT count(*) FROM material')[0]
        assert tx.one('SELECT protocol FROM meta')[0] == PROTOCOL
    assert attempt['thread_id'] == original['thread']
    assert attempt['context_id'] == old_context
    assert opening == {'provenance_id': None, 'ordinary_used': 0, 'capacity_count': 0, 'prior': None}
    assert facts.ordinary_used == int(state == 'failed')
    again = Kernel(path)
    with again.storage.transaction(write=False) as tx:
        assert tx.one('SELECT opening_id FROM attempts WHERE id=?', (original['attempt'],))[0] == attempt['opening_id']
        assert tx.one('SELECT count(*) FROM material')[0] == count
    fresh = Kernel.create(tmp_path / 'fresh.sqlite3', ticket_spec())
    assert _schema(path) == _schema(fresh.storage.path)
    # This historical fixture lacks repository_root. Keep upgrade/basis unit
    # assertions here; actual resumed allocations use the runnable v17 fixture.
    if state == 'consumed':
        assert kernel.status('ticket:2')['phase'] == 'candidate'
    elif state == 'returned':
        assert kernel.status('ticket:2')['pending_return'] is True
    elif state == 'human':
        kernel.control('resume', 'resume-upgraded', subject='ticket:2', message='Keep it.')
        with kernel.storage.transaction(write=False) as tx:
            assert load_continuation(tx, original['attempt']).thread_id == original['thread']
    else:
        assert facts.ordinary_used == 1
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1


def test_upgrade_failure_rolls_back_schema_meta_and_original_native_rows(tmp_path, native_v17_source, monkeypatch):
    path = tmp_path / 'native.sqlite3'
    _produce(native_v17_source, path, 'human')
    schema = _schema(path)
    before = path.read_bytes()
    put = Transaction.put

    def fail_opening(tx, subject, purpose, body):
        if purpose == 'attempt_continuation_opening':
            raise OSError('controlled storage failure')
        return put(tx, subject, purpose, body)

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, 'put', fail_opening)
        with pytest.raises(OSError, match='controlled storage failure'):
            Kernel(path)
    assert path.read_bytes() == before
    assert _schema(path) == schema
    with Storage(path).transaction(write=False) as tx:
        assert tx.one('SELECT protocol FROM meta')[0] == PREVIOUS_PROTOCOL
    assert Kernel(path).status('ticket:2')['pending_return'] is True


@pytest.mark.parametrize('state', ['returned', 'human', 'failed', 'consumed'])
def test_source_backed_v17_upgrade_continues_through_real_runner(tmp_path, native_v17_source, state):
    path = tmp_path / 'runnable-native.sqlite3'
    original = _produce(native_v17_source, path, state, script=_RUNNABLE_PRODUCE)
    kernel = Kernel(path)
    kernel.clock = lambda: 1000.0
    requirements = Requirements(ParentIssue(1, 'Parent', 'Original requirement', (2,)), (
        Issue(2, 'Ticket', 'Original behavior', 'OPEN', frozenset({'ready-for-agent'})),
    ))
    ports = ReadOnlyPorts(requirements, 'refs/heads/ticket-2', 'refs/heads/run',
                          DeliveryFacts(Commit('base', 'base-tree'), None))
    operations = CoreOperations(kernel, Execution('offline-run', 'native-v17', 1), lambda binding: ports)
    if state == 'returned':
        assert operations.once().kind == 'progress'
        assert kernel.status('ticket:2')['phase'] == 'candidate'
    elif state != 'consumed':
        if state == 'failed':
            assert operations.once().reason == 'execution_recovery_unavailable'
        kernel.control('resume', 'resume-upgraded', subject='ticket:2', message='Keep it.')
        call = operations.once().call
        assert call is not None and call.attempt_id == original['attempt']
        assert call.thread_id == original['thread'] and call.deadline_at == 1007.0
    else:
        assert kernel.status('ticket:2')['phase'] == 'candidate'
    assert kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1
