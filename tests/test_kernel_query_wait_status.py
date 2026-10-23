"""Public waiting status follows the recorded wait, without running Core."""
from dataclasses import replace
import json
import time

import pytest

from agent_run_kernel import Checks, DeliveryKind, Kernel
from agent_run_kernel import query
from support.offline import Scenario
from agent_run_kernel.operation_runner import OperationRunner, ReadUnavailable
from agent_run_kernel.query_cli import main
from agent_run_kernel.read_facts import read_boundaries
from tests.test_kernel_query_cli import _snapshot


@pytest.mark.parametrize('initial,later,wait_kind,budget,status', [
    ('mixed_fail', None, 'github_convergence', 600, 'waiting_external'),
    ('pending', None, 'required_checks', 2700, 'waiting_checks'),
    ('unknown', 'pending', 'required_checks', 2700, 'waiting_checks'),
    ('pending', 'unknown', 'required_checks', 2700, 'waiting_checks'),
    ('read_error', None, 'github_convergence', 600, 'waiting_external'),
])
def test_public_checks_status_reads_the_original_committed_wait(
    tmp_path, monkeypatch, capsys, initial, later, wait_kind, budget, status,
):
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET, checks=('pending',))
    now, observed = [100.25], [initial]
    scenario.kernel.clock = lambda: now[0]
    make_ports = scenario.make_ports

    def selected(binding):
        ports = make_ports(binding)
        delivery = ports.delivery

        def physical(request):
            facts = delivery(request)
            if request.check_head is not None and facts.pr is not None:
                if observed[0] == 'read_error':
                    raise ReadUnavailable('github_read_failed', 'Original checks read unavailable')
                result = 'fail' if observed[0] == 'mixed_fail' else observed[0]
                details = (('A', 'fail'), ('B', 'pending')) if observed[0] == 'mixed_fail' else (('required', result),)
                return replace(facts, checks=Checks(facts.pr.number, request.check_head, result, details))
            return facts

        ports.delivery = physical
        return ports

    scenario.make_ports = selected
    pending_transition = (initial, later) == ('unknown', 'pending')
    assert scenario.run().reason == ('github_convergence' if pending_transition else wait_kind)
    if later is not None:
        observed[0], now[0] = later, 105.25
        for _ in range(4):
            step = scenario.runner().run_once()
            if step.kind == 'waiting':
                assert step.reason == wait_kind
                break
            assert step.kind == 'progress' and step.call is None
        else:
            pytest.fail('Original checks wait was not retained')
    subject = scenario.delivery.key
    first_seen = 105.25 if pending_transition else 100.25
    original = scenario.kernel.status(subject)
    assert original['phase'] == 'checks' and original['holds_integration']
    assert original['pending_attempt'] is None and original['purpose'] != 'ci'
    with scenario.kernel.storage.transaction(write=False) as tx:
        window, = tx.rows('SELECT kind,started_at,deadline FROM waits WHERE subject=?', (subject,))
        assert tuple(window) == (wait_kind, first_seen, first_seen + budget)
    before = _snapshot(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('A read-only query attempted Core observation or advancement')

    monkeypatch.setattr(Kernel, '__init__', forbidden)
    monkeypatch.setattr(Kernel, 'begin_observation', forbidden)
    monkeypatch.setattr(Kernel, 'accept_requirements', forbidden)
    for method in ('__init__', 'run_once', 'observe', 'state'):
        monkeypatch.setattr(OperationRunner, method, forbidden)
    # Reading beyond the deadline cannot manufacture Core's timeout gate.
    monkeypatch.setattr(query.time, 'time', lambda: 4000.25)
    for command in ('status', 'history'):
        assert main([command, scenario.spec.run_id, '--state-dir', str(scenario.kernel.storage.path), '--json']) == 0
        document = json.loads(capsys.readouterr().out)
        assert document['status'] == status
        assert document['gate'] == ('required_checks' if status == 'waiting_checks' else None)
        recorded, = document['supervision_windows']
        assert (recorded['kind'], recorded['started_at'], recorded['deadline']) == (wait_kind, first_seen, first_seen + budget)
        assert recorded['timed_out'] is False
        assert document['operator_actions'] == []
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert read_boundaries(tx, time.time())['status'] == status
    assert _snapshot(tmp_path) == before
