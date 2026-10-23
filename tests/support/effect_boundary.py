"""Hold only a real physical port; selection and authority stay in Core."""
from contextlib import contextmanager
from agent_run.contracts.write_authority import WriteAuthority


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

