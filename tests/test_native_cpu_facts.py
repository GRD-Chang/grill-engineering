"""CPU evidence contracts; no native business or time-namespace acceptance."""
import json
import os
from types import SimpleNamespace
import pytest
from support import native_cpu_facts as cpu
from support import host_cost_trace as costs


def test_real_snapshot_is_bounded_numeric_and_does_not_invent_missing_counters():
    sample=cpu.snapshot(cgroup=True)
    assert sample['pid']==os.getpid()
    assert all(type(sample[key]) is int for key in ('tid','monotonic_ns','thread_cpu_ns','process_cpu_ns'))
    assert len(sample['waited_children_cpu_us'])==2
    for key in ('thread_schedstat','cpu_stat','cpu_pressure','cpu_max'):
        row=sample[key]
        assert set(row)=={'text','file_identity'} or set(row)=={'unavailable'}
        if 'text' in row:assert len(row['text'])<=4096
    assert len(json.dumps(sample))<4096


@pytest.mark.parametrize('read_kind',[OSError,TimeoutError,InterruptedError,KeyboardInterrupt])
@pytest.mark.parametrize('close_kind',[OSError,TimeoutError,InterruptedError,KeyboardInterrupt])
def test_cpu_read_double_fault_keeps_control_and_closes_once(monkeypatch,read_kind,close_kind):
    original,cleanup=read_kind('read'),close_kind('close');closed=[]
    def fail_read(*args):raise original
    def close(fd):closed.append(fd);raise cleanup
    monkeypatch.setattr(cpu,'os',SimpleNamespace(O_RDONLY=0,O_CLOEXEC=0,open=lambda *a:7,
        fstat=lambda fd:SimpleNamespace(st_dev=1,st_ino=2),read=fail_read,close=close))
    expected=(original if isinstance(original,(TimeoutError,InterruptedError,KeyboardInterrupt))
              or not isinstance(cleanup,(TimeoutError,InterruptedError,KeyboardInterrupt)) else cleanup)
    with pytest.raises(type(expected)) as got:cpu._read('/fixed')
    assert got.value is expected and closed==[7]


@pytest.mark.parametrize('failure',[OSError('ordinary'),TimeoutError('watchdog'),InterruptedError('interrupt')])
def test_unavailable_is_explicit_and_controls_propagate(monkeypatch,failure):
    def read(path):raise failure
    monkeypatch.setattr(cpu,'_read',read)
    if isinstance(failure,(TimeoutError,InterruptedError)):
        with pytest.raises(type(failure)) as got:cpu.snapshot(cgroup=True)
        assert got.value is failure
    else:
        sample=cpu.snapshot(cgroup=True)
        assert sample['cpu_stat']=={'unavailable':'OSError'}


def test_nested_thread_cpu_excludes_only_same_thread_children(tmp_path,monkeypatch):
    now=[0]
    monkeypatch.setattr(costs,'thread_time_ns',lambda:now[0])
    root=tmp_path/'host-costs';root.mkdir(mode=0o700);trace=costs.HostCosts(root)
    try:
        with trace.span('outer'):
            now[0]=10
            with trace.span('inner'):now[0]=30
            now[0]=50
        trace.flush()
        result,=costs.read_costs(tmp_path)['hosts']
        assert result['cpu_totals']=={'outer':[50,30],'inner':[20,20]}
        assert result['cpu_start']['pid']==os.getpid()==result['cpu_snapshot']['pid']
        assert costs.read_costs(tmp_path)['reader_cpu']['pid']==os.getpid()
    finally:os.close(trace.directory)


@pytest.mark.parametrize('original_fails',[False,True])
@pytest.mark.parametrize('failure_kind',[TimeoutError,InterruptedError])
def test_cpu_exit_probe_preserves_business_exception(tmp_path,monkeypatch,original_fails,failure_kind):
    root=tmp_path/'host-costs';root.mkdir(mode=0o700);trace=costs.HostCosts(root)
    original=RuntimeError('business');control=failure_kind('probe');calls=[0]
    def timer():
        calls[0]+=1
        if calls[0]==2:raise control
        return 0
    monkeypatch.setattr(costs,'thread_time_ns',timer)
    try:
        with pytest.raises(type(original if original_fails else control)) as got:
            with trace.span('business'):
                if original_fails:raise original
        assert got.value is (original if original_fails else control)
    finally:os.close(trace.directory)


def test_oversized_cpu_file_closes_once_without_returning_partial_facts(monkeypatch):
    closed=[]
    monkeypatch.setattr(cpu,'os',SimpleNamespace(O_RDONLY=0,O_CLOEXEC=0,open=lambda *a:7,
        fstat=lambda fd:SimpleNamespace(st_dev=1,st_ino=2),read=lambda fd,size:b'x'*size,close=closed.append))
    with pytest.raises(ValueError):cpu._read('/fixed')
    assert closed==[7]
