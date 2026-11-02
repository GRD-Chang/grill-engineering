"""Driver bookkeeping only; no Core, Git, Task, Host or Worker execution proof."""
import inspect
from types import SimpleNamespace

import pytest

from agent_run.model import Step
from support.offline import GraphScenario, Scenario


def driver(monkeypatch, record, *, with_call=True, failure=None):
    scenario = object.__new__(Scenario)
    events, state = [], {'phase': 'approval', 'resource_cleanup': None}
    call, raw = object(), b'original raw result'
    original = Step('progress', 'ticket:3', call=call if with_call else None)

    def mark(stage):
        events.append(stage)
        if failure is not None and failure[0] == stage:
            raise failure[1]

    def run_once():
        mark('run')
        return original

    def prepare(kernel, step):
        assert kernel is scenario.kernel and step is original
        mark('prepare')
        return step

    def authorize(value):
        assert value is call
        mark('authorize')

    def agent(value):
        assert value is call
        mark('agent')
        return raw

    def receive(value, result):
        assert value is call and result is raw
        mark('receive')

    def status(subject):
        assert subject == 'ticket:3'
        mark('status')
        return dict(state)

    runner = SimpleNamespace(run_once=run_once, authorize_agent=authorize)
    scenario.runner = lambda: runner
    scenario.kernel = SimpleNamespace(receive=receive, status=status)
    scenario.agent, scenario.delivery = agent, SimpleNamespace(key='ticket:3')
    scenario.record_checkpoints, scenario.checkpoints = record, []
    monkeypatch.setattr('support.core_operations.prepare_scripted_agent', prepare)
    return scenario, original, events, state, runner


@pytest.mark.parametrize('scenario_type', [Scenario, GraphScenario])
def test_checkpoint_recording_defaults_to_enabled(scenario_type):
    assert inspect.signature(scenario_type).parameters['record_checkpoints'].default is True


@pytest.mark.parametrize('with_call', [False, True])
def test_recording_switch_only_removes_the_extra_projection(monkeypatch, with_call):
    sequences = []
    for record in (True, False):
        scenario, step, events, state, _ = driver(monkeypatch, record, with_call=with_call)
        assert scenario.step() is step
        expected = ['run', 'prepare'] + (['authorize', 'agent', 'receive'] if with_call else [])
        assert events == expected + (['status'] if record else [])
        assert scenario.checkpoints == ([dict(state)] if record else [])
        sequences.append([event for event in events if event != 'status'])
    assert sequences[0] == sequences[1]


@pytest.mark.parametrize('record', [False, True])
def test_preparation_returns_the_admitted_step_without_replacing_its_call(monkeypatch, record):
    scenario, admitted, events, _, runner = driver(monkeypatch, record)
    preparation = Step('prepare', 'ticket:3', preparation=object())
    runner.run_once = lambda: (events.append('run') or preparation)

    def prepare(kernel, step):
        assert kernel is scenario.kernel and step is preparation
        events.append('prepare')
        return admitted

    monkeypatch.setattr('support.core_operations.prepare_scripted_agent', prepare)
    assert scenario.step() is admitted
    assert events == ['run', 'prepare', 'authorize', 'agent', 'receive'] + (['status'] if record else [])


@pytest.mark.parametrize('record', [False, True])
@pytest.mark.parametrize('stage', ['run', 'prepare', 'authorize', 'agent', 'receive'])
@pytest.mark.parametrize('error', [OSError('original failure'), KeyboardInterrupt('original cancellation')])
def test_business_failure_keeps_original_exception_and_never_records_or_replays(monkeypatch, record, stage, error):
    scenario, _, events, _, _ = driver(monkeypatch, record, failure=(stage, error))
    with pytest.raises(type(error)) as caught:
        scenario.step()
    assert caught.value is error
    order = ['run', 'prepare', 'authorize', 'agent', 'receive']
    assert events == order[:order.index(stage) + 1]
    assert scenario.checkpoints == []


def test_enabled_checkpoint_read_failure_still_propagates_after_receive(monkeypatch):
    error = OSError('original checkpoint read failure')
    scenario, _, events, _, _ = driver(monkeypatch, True, failure=('status', error))
    with pytest.raises(OSError) as caught:
        scenario.step()
    assert caught.value is error
    assert events == ['run', 'prepare', 'authorize', 'agent', 'receive', 'status']
    assert scenario.checkpoints == []


@pytest.mark.parametrize('record', [False, True])
def test_run_still_reads_current_cleanup_after_advancing(monkeypatch, record):
    scenario, _, events, state, runner = driver(monkeypatch, record, with_call=False)
    state['phase'] = 'checks'
    original = runner.run_once

    def advance():
        original()
        state.update(phase='complete', resource_cleanup={'state': 'pending', 'outcome': 'unknown'})
        return Step('waiting', 'ticket:3')

    runner.run_once = advance
    # This test changes the returned Step, so keep the preparation seam literal.
    monkeypatch.setattr('support.core_operations.prepare_scripted_agent', lambda _kernel, step: step)
    assert scenario.run().kind == 'complete'
    assert events == ['status', 'run'] + (['status'] if record else []) + ['status']


def test_enabled_trace_keeps_the_historical_approval_snapshot(monkeypatch):
    scenario, _, events, state, _ = driver(monkeypatch, True, with_call=False)
    scenario.step()
    state['phase'] = 'complete'
    scenario.role_calls = []
    scenario.development_count = scenario.review_count = scenario.publication_count = 0
    scenario.writes = lambda: []
    scenario.port = SimpleNamespace(objects=SimpleNamespace(snapshot=lambda: {}))
    trace = scenario.trace()
    assert trace['checkpoints'] == [{'phase': 'approval', 'resource_cleanup': None}]
    assert trace['final']['phase'] == 'complete'
    assert events == ['run', 'prepare', 'status', 'status']
