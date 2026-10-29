"""Mechanical Host units with scripted runner responses, not Core admission proof."""
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import agent_run.runtime.executor as executor
from agent_run.runtime.execution import ProcessExitUnknown
from agent_run.model import AgentCall, AgentPreparation, Execution, Profile, Purpose, Role, Step
from support.offline import fixture_inputs
from agent_run.core.operation_contract import OperationBinding, OperationState
from support.notification import _NoNotifications


def test_disabled_notification_substitute_preserves_required_storage_keyword(tmp_path):
    selected = SimpleNamespace(path=tmp_path / 'unopened.sqlite3', run_id='exact-run')
    observer = _NoNotifications(selected.path, tmp_path, {}, storage=selected)
    assert observer.storage is selected
    observer.observe_committed()
    observer.close(drain=False)
    with pytest.raises(TypeError, match='storage'):
        _NoNotifications(selected.path, tmp_path, {})
    assert not selected.path.exists()


def test_busy_host_uses_supervision_without_requesting_an_unused_work_choice(tmp_path, monkeypatch):
    entered, released, supervised = threading.Event(), threading.Event(), []

    class Runner:
        def activate_observation(self):
            pass
        def state(self):
            assert not entered.is_set() or released.is_set(), 'Busy Host requested an unused work choice'
            return OperationState(1, (), (), time.monotonic() + 0.01,
                                  'quiescent' if released.is_set() else 'ready')
        def supervise(self):
            assert entered.wait(5)
            supervised.append(True)
            released.set()
            return SimpleNamespace(cancel=(), wake_at=None)
        def run_once(self, **options):
            entered.set()
            assert released.wait(5), 'Host never read fresh busy supervision'
            return Step('progress', '')

    class Core:
        storage = SimpleNamespace(path=tmp_path / 'unopened.sqlite3')
        clock = staticmethod(time.monotonic)
        @contextmanager
        def executor_scope(self, execution):
            try:
                yield lambda error: released.set()
            finally:
                released.set()

    monkeypatch.setattr(executor, 'OperationRunner', lambda *args: Runner())
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', _NoNotifications)
    assert executor.run(Core(), Execution('controlled-host', 'owner', 1), {}) == 0
    assert entered.is_set() and supervised == [True]
    assert not any(t.name.startswith('agent-run-io') and t.is_alive() for t in threading.enumerate())


@pytest.mark.parametrize("settled", [True, False])
def test_host_dispatches_call_then_polls_independent_io_and_requires_durable_exit(tmp_path, monkeypatch, settled):
    execution = Execution("host-unit", "owner", 1)
    call = AgentCall("attempt", "invocation", "ticket:2", Role.DEVELOPMENT, Purpose.REQUIREMENTS,
                     "thread", "original-lease", execution, {}, Profile(), 30.0, time.monotonic()+30)
    preparation = AgentPreparation(call.lease_id, call.attempt_id, call.subject, call.role, call.purpose,
        call.thread_id, execution, call.context, call.profile, call.deadline_seconds, time.monotonic()+600)
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
                return Step("preparing", call.subject, preparation=preparation)
            assert len(capacities) == 2, "Host must respect waiting/quiescent disposition"
            assert entered.wait(5), "allocated AgentCall was retained before a later slow poll"
            read_done.set()
            return Step("waiting", call.subject)
        def state(self):
            assert lifecycle and lifecycle[0] == "activate"
            lifecycle.append("state")
            finished = exited.is_set() and settled
            return OperationState(1, (call.lease_id,) if entered.is_set() and not finished else (),
                                  (call.lease_id,) if read_done.is_set() else (), None,
                                  "quiescent" if read_done.is_set() else "ready",
                                  (preparation.reservation_id,) if not entered.is_set() and not finished else ())
        def supervise(self):
            current = self.state()
            return SimpleNamespace(cancel=current.cancel, wake_at=current.wake_at)
        def authorize_agent(self, selected):
            assert selected == call
            assert threading.current_thread().name.startswith("agent-run_")
        def worker_settled(self, selected):
            assert selected == preparation and exited.is_set()
            return settled

    class Core:
        storage = SimpleNamespace(path=tmp_path / "unused.sqlite")
        clock = staticmethod(time.monotonic)
        @contextmanager
        def executor_scope(self, selected):
            assert selected == execution
            yield lambda error: lifecycle.append("admission-closed")
    def make_runner(core, selected, make_ports):
        assert selected == execution
        port = make_ports(binding)
        assert port.root == binding.repository_root and port.identity == binding.inputs.runner_identity
        assert port.remote == binding.inputs.repository_remote
        return Runner()

    def execute(_kernel, selected, **kwargs):
        assert selected == preparation
        assert kwargs["identity"] == binding.inputs.worker_identity
        assert kwargs["environment"] == {"FROZEN_ENV": "captured"}
        assert kwargs["hidden_paths"] == binding.hidden_paths
        assert kwargs["hidden_environment"] == binding.hidden_environment
        kwargs["authorize_source"](call)  # Inline; it cannot queue behind its own Future.
        entered.set()
        try:
            assert read_done.wait(5), "I/O was queued behind the only Agent worker"
            assert kwargs["cancelled"].wait(5), "exact lease cancellation was lost"
        finally:
            exited.set()

    class Notifications:
        def __init__(self, *_args, storage):
            assert storage is Core.storage
        def observe_committed(self):
            pass
        def close(self, **_kwargs):
            pass

    monkeypatch.setattr(executor, "OperationRunner", make_runner)
    monkeypatch.setattr(executor, "render_role_prompt", lambda _call: "frozen prompt")
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex', execute)
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', Notifications)
    if settled:
        assert executor.run(Core(), execution, {"FROZEN_ENV": "captured"}) == 0
    else:
        with pytest.raises(ProcessExitUnknown):
            executor.run(Core(), execution, {"FROZEN_ENV": "captured"})
    assert capacities == [1, 0]
    assert entered.is_set() and read_done.is_set() and exited.is_set()
    assert lifecycle[:2] == ["activate", "state"] and lifecycle.count("activate") == 1
