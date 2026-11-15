"""A queued integration waiter cannot starve its owner's original readback."""
import pytest

from support.offline import GraphScenario
from support.offline_prefixes import _prepared_stage
from support.run_scoped_kernel import prepare_domain_history
from support.requirements_scenarios import independent_source


def _prepare_waiter(scenario):
    scenario.kernel.clock = lambda: 100.25
    scenario.port.lose_response.add('merge')
    retry = None
    for _ in range(80):
        step = scenario.step()
        pending = scenario.kernel.status('ticket:3')['pending_write']
        if pending is not None and pending['status'] == 'sent' and retry is None:
            assert pending['operation'] == 'merge'
            retry = step.retry_at
        if step.subject == 'ticket:4' and step.reason == 'integration_seat':
            break
    else:
        raise AssertionError('Normal producer did not reach the original merge plus queued waiter')
    assert retry is not None and pending is not None
    assert scenario.kernel.status('ticket:3')['holds_integration']
    assert not scenario.kernel.status('ticket:4')['holds_integration']
    assert len([w for w in scenario.port.writes() if w['operation'] == 'merge']) == 1
    return retry, pending['id']


@pytest.fixture(scope='module')
def _merge_waiter_stage(tmp_path_factory):
    original = None

    def prepare(root):
        nonlocal original
        scenario = GraphScenario(root, independent_source())
        with prepare_domain_history(scenario):
            original = _prepare_waiter(scenario)
        return scenario

    with _prepared_stage(tmp_path_factory, 'merge-waiter', prepare) as checkout:
        assert original is not None
        yield checkout, original


@pytest.fixture
def pending_merge_with_waiter(_merge_waiter_stage, isolated_user_environment):
    checkout, (retry, original) = _merge_waiter_stage
    with checkout() as scenario:
        now = [100.25]
        scenario.kernel.clock = lambda: now[0]
        yield scenario, now, retry, original


def test_due_original_merge_settles_before_its_integration_waiter(pending_merge_with_waiter):
    scenario, now, retry, original = pending_merge_with_waiter
    now[0] = retry + 0.01
    step = scenario.step()
    assert (step.kind, step.subject) == ('progress', 'ticket:3')
    pending = scenario.kernel.status('ticket:3')['pending_write']
    assert pending['id'] == original and pending['status'] == 'observed'
    for _ in range(12):
        if not scenario.kernel.status('ticket:3')['holds_integration']:
            break
        assert not scenario.kernel.status('ticket:4')['holds_integration']
        scenario.step()
    else:
        raise AssertionError('Exact readback/completion did not release the original seat')
    assert scenario.kernel.status('ticket:3')['phase'] == 'complete'
    assert len([w for w in scenario.port.writes() if w['operation'] == 'merge']) == 1
    # The existing queued waiter gets the seat after its owner's completion.
    for _ in range(4):
        scenario.step()
        if scenario.kernel.status('ticket:4')['holds_integration']:
            break
    assert scenario.kernel.status('ticket:4')['holds_integration']


def test_retry_and_stop_do_not_grant_the_waiter_or_repeat_a_write(pending_merge_with_waiter):
    scenario, now, retry, original = pending_merge_with_waiter
    before = scenario.port.writes()
    runner = scenario.runner()
    state = runner.state()
    assert state.disposition == 'waiting' and state.wake_at == retry
    scenario.step()
    assert scenario.port.writes() == before
    pending = scenario.kernel.status('ticket:3')['pending_write']
    assert scenario.kernel.control('stop', 'stop-unknown-merge', subject='ticket:3') == 'applied'
    stopped = scenario.kernel.status('ticket:3')
    assert 'operator_stopped' in stopped['gates'] and stopped['holds_integration']
    assert stopped['pending_write'] == pending
    assert not scenario.kernel.status('ticket:4')['holds_integration']
    assert scenario.port.writes() == before
    scenario.kernel.control('stop', 'stop-original-readback')
    now[0] = retry + 0.01
    step = runner.run_once(agent_capacity=0)
    assert (step.kind, step.reason) == ('paused', 'executor_fenced')
    pending = scenario.kernel.status('ticket:3')['pending_write']
    assert pending['id'] == original and pending['status'] == 'sent'
    assert scenario.port.writes() == before
    assert not scenario.kernel.status('ticket:4')['holds_integration']
    assert scenario.kernel.status('ticket:3')['holds_integration']
