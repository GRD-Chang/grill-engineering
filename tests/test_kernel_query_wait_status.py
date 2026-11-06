"""Public waiting status follows the recorded wait, without running Core."""
import json
import time

from agent_run.model import Checks, DeliveryKind
from agent_run.core.kernel import Kernel
import agent_run.queries.run as query
from support.offline import Scenario
from agent_run.core.operation_runner import OperationRunner
from agent_run.cli.main import main
from agent_run.queries.read_facts import read_boundaries
from support.environment import durable_snapshot as _snapshot


def test_public_checks_status_reads_the_original_committed_wait(tmp_path, monkeypatch, capsys):
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET, checks=('pending',))
    scenario.kernel.clock = lambda: 100.25
    assert scenario.run().reason == 'required_checks'
    subject = scenario.delivery.key
    original = scenario.kernel.status(subject)
    assert original['phase'] == 'checks' and original['holds_integration']
    assert original['pending_attempt'] is None and original['purpose'] != 'ci'
    with scenario.kernel.storage.transaction(write=False) as tx:
        window, = tx.rows('SELECT kind,started_at,deadline FROM waits WHERE subject=?', (subject,))
        assert tuple(window) == ('required_checks', 100.25, 2800.25)
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
        assert document['status'] == 'waiting_checks'
        recorded, = document['supervision_windows']
        assert (recorded['kind'], recorded['started_at'], recorded['deadline']) == ('required_checks', 100.25, 2800.25)
        assert recorded['timed_out'] is False
        assert document['operator_actions'] == []
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert read_boundaries(tx, time.time())['status'] == 'waiting_checks'
    assert _snapshot(tmp_path) == before
