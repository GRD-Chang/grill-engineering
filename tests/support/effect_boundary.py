"""Hold only a real physical port; selection and authority stay in Core."""
from contextlib import contextmanager
import json

from agent_run.errors import KernelError
from agent_run.storage.run_scope import transaction_scope

import pytest

from agent_run.contracts.write_authority import WriteAuthority
from support.core_operations import CoreOperations
from support.offline import Scenario


class EffectBoundary(Exception):
    """A test barrier at the selected physical write, never a Core selector."""

    def __init__(self, effect, authorize: WriteAuthority | None):
        self.effect, self.authorize = effect, authorize
        self.phase = "execute" if authorize is not None else "reconcile"
        super().__init__(effect.operation)


@contextmanager
def effect_boundary(scenario, monkeypatch, operation, *, sent=False):
    make_ports = scenario.make_ports

    def intercepted(binding):
        ports = make_ports(binding)
        execute, reconcile = ports.execute, ports.reconcile

        def hold(effect, authorize):
            if effect.operation == operation:
                if sent:
                    authorize.prepare()()
                raise EffectBoundary(effect, authorize)
            return execute(effect, authorize)

        def readback(effect):
            if effect.operation == operation:
                raise EffectBoundary(effect, None)
            return reconcile(effect)

        ports.execute, ports.reconcile = hold, readback
        return ports

    with monkeypatch.context() as boundary_patch:
        boundary_patch.setattr(scenario, "make_ports", intercepted)
        yield


def workspace_intent(scenario, monkeypatch, *, sent=False):
    with effect_boundary(scenario, monkeypatch, "workspace", sent=sent):
        with pytest.raises(EffectBoundary) as boundary:
            CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).until(lambda step: False, limit=16)
    return boundary.value


def advance_to_effect(scenario: Scenario, monkeypatch, operation: str, *, developments: int = 0,
                      sent: bool = False) -> EffectBoundary:
    # The existing runner chooses every subject and transition. Only the real
    # physical port is held, using the same boundary fixture as remote tests.
    for _ in range(150):
        if scenario.development_count >= developments:
            break
        scenario.step()
    with effect_boundary(scenario, monkeypatch, operation, sent=sent):
        with pytest.raises(EffectBoundary) as boundary:
            for _ in range(150):
                step = scenario.step()
                assert step.kind not in {"paused", "complete", "approval", "waiting"}
    assert boundary.value.effect.operation == operation and boundary.value.phase == "execute"
    if sent:
        assert scenario.kernel.status(boundary.value.effect.subject)["pending_write"]["status"] == "sent"
    return boundary.value


def _merge_record(scenario: Scenario):
    # Audit only records produced through real Core APIs, never prepare a row.
    with scenario.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        row = tx.one("SELECT * FROM effects WHERE run_id=? AND subject=? AND operation='merge' ORDER BY rowid DESC LIMIT 1",
                     (scope.run_id, scenario.delivery.key))
        return dict(row), json.loads(tx.get(row["payload_id"], row["subject"])), (
            None if row["receipt_id"] is None else json.loads(tx.get(row["receipt_id"], row["subject"])))


def _local_marker(scenario: Scenario) -> str:
    with scenario.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        return str(tx.one("SELECT e.before_sha FROM effects e JOIN subjects s ON s.run_id=e.run_id AND s.id=e.subject AND s.generation=e.generation WHERE e.run_id=? AND e.subject=? AND e.operation='workspace' AND e.status='confirmed'",
                          (scope.run_id, scenario.delivery.key))[0])


def state_of(s, original):
    with s.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        return tuple(tx.one("SELECT status,attempts FROM effects WHERE run_id=? AND id=?", (scope.run_id, original["id"])))


def effect_state(s, effect):
    with s.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        if effect.execution.run_id != scope.run_id:
            raise KernelError("effect_binding")
        return tuple(tx.one("SELECT status,attempts,payload_id FROM effects WHERE run_id=? AND id=?", (scope.run_id, effect.effect_id)))
