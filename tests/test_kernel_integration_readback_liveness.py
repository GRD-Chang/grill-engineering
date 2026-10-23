"""A queued integration waiter cannot starve its owner's original readback."""
from support.offline import GraphScenario
from test_kernel_requirements import independent_source


def _pending_merge_with_waiter(tmp_path):
    scenario = GraphScenario(tmp_path / 'graph', independent_source())
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
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
    return scenario, now, retry, pending['id']


def test_due_original_merge_settles_before_its_integration_waiter(tmp_path):
    scenario, now, retry, original = _pending_merge_with_waiter(tmp_path)
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


def test_retry_and_stop_do_not_grant_the_waiter_or_repeat_a_write(tmp_path):
    scenario, now, retry, original = _pending_merge_with_waiter(tmp_path)
    before = scenario.port.writes()
    runner = scenario.runner()
    state = runner.state()
    assert state.disposition == 'waiting' and state.wake_at == retry
    scenario.step()
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
