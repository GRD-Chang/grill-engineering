"""Driver bookkeeping only; no Core, Git, Task, Host or Worker execution proof."""
import inspect
from types import SimpleNamespace

import pytest

from agent_run.model import Step
from support.offline import GraphScenario, Scenario


def driver(monkeypatch, record, *, with_call=True):
    scenario = object.__new__(Scenario)
    events, state = [], {'phase': 'approval', 'resource_cleanup': None}
    call, raw = object(), b'original raw result'
    original = Step('progress', 'ticket:3', call=call if with_call else None)

    def mark(stage):
        events.append(stage)

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
def test_checkpoint_recording_is_opt_in(scenario_type):
    assert inspect.signature(scenario_type).parameters['record_checkpoints'].default is False


def test_neutral_observation_requires_collected_history(monkeypatch):
    scenario, *_ = driver(monkeypatch, False)
    with pytest.raises(ValueError, match="record_checkpoints=True"):
        scenario.neutral_observation({}, "input-digest")


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
