"""Mechanical Host units with scripted runner responses, not Core admission proof."""
import threading
import time
from types import SimpleNamespace

import pytest

from agent_run_kernel import executor
from agent_run_kernel.execution import ProcessExitUnknown
from agent_run_kernel.model import AgentCall, Execution, Profile, Purpose, Role, Step
from support.offline import fixture_inputs
from agent_run_kernel.operation_runner import OperationBinding, OperationState


@pytest.mark.parametrize("settled", [True, False])
def test_host_dispatches_call_then_polls_independent_io_and_requires_durable_exit(tmp_path, monkeypatch, settled):
    execution = Execution("host-unit", "owner", 1)
    call = AgentCall("attempt", "invocation", "ticket:2", Role.DEVELOPMENT, Purpose.REQUIREMENTS,
                     "thread", "original-lease", execution, {}, Profile(), 30.0, time.monotonic()+30)
    binding = OperationBinding(tmp_path, "example/project", 1, fixture_inputs(),
                               (tmp_path / "hidden",), ("FROZEN_SECRET",))
    entered, read_done, exited = threading.Event(), threading.Event(), threading.Event()
    capacities = []
    lifecycle = []

    class Runner:
        def __init__(self):
            self.binding = binding
        def activate_observation(self):
            assert lifecycle == [], "Host must activate exactly once before its first state read"
            lifecycle.append("activate")
        def run_once(self, *, agent_capacity=1):
            assert lifecycle[:2] == ["activate", "state"]
            capacities.append(agent_capacity)
            if len(capacities) == 1:
                return Step("agent", call.subject, call=call)
            assert len(capacities) == 2, "Host must respect waiting/quiescent disposition"
            assert entered.wait(5), "allocated AgentCall was retained before a later slow poll"
            read_done.set()
            return Step("waiting", call.subject)
        def state(self):
            assert lifecycle and lifecycle[0] == "activate"
            lifecycle.append("state")
            return OperationState(1, () if exited.is_set() and settled else (call.lease_id,),
                                  (call.lease_id,) if read_done.is_set() else (), None,
                                  "quiescent" if read_done.is_set() else "ready")
        def authorize_agent(self, selected):
            assert selected == call
            assert threading.current_thread().name.startswith("agent-run_")
        def worker_settled(self, selected):
            assert selected == call and exited.is_set()
            return settled

    class Core:
        storage = SimpleNamespace(path=tmp_path / "unused.sqlite")
        clock = staticmethod(time.monotonic)
        def operation_runner(self, selected, make_ports):
            assert selected == execution
            port = make_ports(binding)
            assert port.root == binding.repository_root and port.identity == binding.inputs.runner_identity
            assert port.remote == binding.inputs.repository_remote
            return Runner()

    def execute(_kernel, selected, **kwargs):
        assert selected == call
        assert kwargs["identity"] == binding.inputs.worker_identity
        assert kwargs["environment"] == {"FROZEN_ENV": "captured"}
        assert kwargs["hidden_paths"] == binding.hidden_paths
        assert kwargs["hidden_environment"] == binding.hidden_environment
        kwargs["authorize_source"]()  # Inline; it cannot queue behind its own Future.
        entered.set()
        try:
            assert read_done.wait(5), "I/O was queued behind the only Agent worker"
            assert kwargs["cancelled"].wait(5), "exact lease cancellation was lost"
        finally:
            exited.set()

    class Notifications:
        def __init__(self, *_args):
            pass
        def observe_committed(self):
            pass
        def close(self, **_kwargs):
            pass

    monkeypatch.setattr(executor, "render_role_prompt", lambda _call: "frozen prompt")
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex", execute)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", Notifications)
    if settled:
        assert executor.run(Core(), execution, {"FROZEN_ENV": "captured"}) == 0
    else:
        with pytest.raises(ProcessExitUnknown):
            executor.run(Core(), execution, {"FROZEN_ENV": "captured"})
    assert capacities == [1, 0]
    assert entered.is_set() and read_done.is_set() and exited.is_set()
    assert lifecycle[:2] == ["activate", "state"] and lifecycle.count("activate") == 1
