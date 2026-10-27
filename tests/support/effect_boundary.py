"""Hold only a real physical port; selection and authority stay in Core."""
from contextlib import contextmanager

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
