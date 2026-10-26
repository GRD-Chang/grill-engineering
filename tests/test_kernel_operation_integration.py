"""Actual Core operation runner through Host; Agent and GitHub are synthetic."""
from support.core_operations import prepare_scripted_agent
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
import pytest

import agent_run.runtime.executor as executor
from support.offline import GraphScenario
from test_kernel_host_observation import admit_scenario
from test_kernel_operation_runner import Ports
from support.requirements_scenarios import independent_source


class Notifications:
    def __init__(self, *_args):
        pass
    def observe_committed(self):
        pass
    def close(self, **_kwargs):
        pass


def _admit_host(scenario, tmp_path):
    """Use the admitted manager witness for this synthetic process adapter."""
    admit_scenario(scenario, tmp_path)
    action = scenario.kernel.active_host_action()
    assert action is not None
    usage = scenario.kernel.host_usage(action)
    assert usage is not None and usage.usage_ready and not usage.usage_released
    return {"host_pid": usage.main_pid, "host_start_token": usage.start_token,
            "boot_id": usage.boot_id, "host_cgroup": usage.cgroup, "cgroup": usage.cgroup}


def test_github_reader_can_be_imported_cold_in_an_independent_process():
    import os
    from pathlib import Path
    import subprocess
    import sys
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-c", 'import agent_run.adapters.github'],
                            env={**os.environ, "PYTHONPATH": str(source)},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_host_real_core_original_merge_readback_final_approval_and_cleanup(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path / "host", independent_source())
    host = _admit_host(scenario, tmp_path)
    ports = Ports(scenario)
    scenario.port.lose_response.add("merge")
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)

    def execute(kernel, preparation, **kwargs):
        call = prepare_scripted_agent(kernel, preparation).call
        assert call is not None
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"](call)
        process = {"pid": 4242, "start_token": "synthetic-worker", "pgid": 4242, **host}
        kernel.agent_started(call, process)
        kwargs["authorize_source"](call)
        kernel.agent_source_ready(call)
        try:
            scenario.select(call.subject)  # Metadata for the call Core already allocated.
            kernel.receive(call, scenario.agent(call))
        finally:
            kernel.agent_exited(call, process, 0)

    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex', execute)
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', Notifications)
    assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    final = scenario.kernel.source_status()["final_subject"]
    assert final is not None and scenario.kernel.status(final)["phase"] == "approval"
    assert len(scenario.kernel.source_status()["completions"]) == 2
    runner = OperationRunner(scenario.kernel, scenario.execution, lambda binding: ports)
    scenario.kernel.control("approve", "approve-final", subject=final, world=runner.observe(final))
    assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM subjects WHERE phase!='complete'")
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT subject FROM resource_cleanup WHERE state!='complete'")
        assert len(tx.rows("SELECT * FROM final_barriers")) == 1
    assert sum(item["operation"] == "merge" for item in scenario.port.writes()) == 3


@pytest.mark.parametrize("stage", ["source", "delivery"])
def test_bootstrap_source_outage_keeps_original_call_and_bounded_read_wait(tmp_path, monkeypatch, stage):
    import pytest
    from agent_run.model import ReadWait
    from support.core_operations import CoreOperations
    from support.remote_effects import _change, _fixture, _source_kernel
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    kernel = _source_kernel(tmp_path, port, push)
    execution = kernel.claim_executor("real-port-bootstrap")
    now = [100.25]
    kernel.clock = lambda: now[0]
    operations = CoreOperations(kernel, execution, lambda binding: port)
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None and kernel.dispatch_agent(call)
    runner = operations.runner
    source = port.source
    failing = [True]
    def read_source(parent):
        if stage == "source":
            _change(state, read_error=failing[0])
        facts = source(parent)  # Real fake-gh subprocess transport, no fake port exception.
        if stage == "delivery":
            _change(state, read_error=failing[0])
        return facts
    monkeypatch.setattr(port, "source", read_source)
    for instant, expected_retry in ((100.25, 105.25), (110.25, 120.25)):
        now[0] = instant
        if stage == "delivery":
            _change(state, read_error=False)
        with pytest.raises(ReadWait) as waiting:
            runner.authorize_agent(call)
        assert waiting.value.step.kind == "waiting" and waiting.value.retry_at == expected_retry
        with kernel.storage.transaction(write=False) as tx:
            window = tx.one("SELECT started_at,deadline FROM waits WHERE subject=?", (call.subject,))
            assert window["started_at"] == 100.25 and window["deadline"] == 700.25
            assert tx.one("SELECT id FROM leases")[0] == call.lease_id
            assert not tx.rows("SELECT * FROM effect_failures")
        assert not runner.worker_settled(call)
        assert kernel.status(call.subject)["pending_attempt"] == call.attempt_id
        assert kernel.status(call.subject)["budgets"]["requirements"]["developments_used"] == 0
    failing[0] = False
    _change(state, read_error=False)
    now[0] = 120.25
    runner.authorize_agent(call)
    assert not runner.worker_settled(call)
    import json
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert actual and not any("--method" in command for command in actual)


def test_real_core_n1_ci_poll_advances_during_independent_long_agent(tmp_path, monkeypatch):
    from dataclasses import replace
    import signal
    import threading
    from agent_run.model import Checks, Policy
    from support.offline_api import SyntheticGitHub
    source = independent_source()
    source = replace(source, issues=tuple(replace(issue, state="CLOSED") if issue.number == 4 else issue for issue in source.issues))
    scenario = GraphScenario(tmp_path / "concurrent", source, policy=Policy(concurrency=1))
    host = _admit_host(scenario, tmp_path)
    ports = Ports(scenario)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    runner = OperationRunner(scenario.kernel, scenario.execution, lambda binding: ports)
    for _ in range(80):
        if scenario.kernel.status("ticket:3")["phase"] == "checks":
            break
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        if step.call is not None:
            # Preserve the synthetic producer's exact start/return/exit hooks,
            # with a process witness belonging to this already admitted Host.
            call = step.call
            assert scenario.kernel.dispatch_agent(call)
            runner.authorize_agent(call)
            process = {"pid": 4242, "start_token": "synthetic", "pgid": 4242, **host}
            scenario.kernel.agent_started(call, process)
            scenario.select(call.subject)
            scenario.kernel.receive(call, scenario.agent(call))
            assert not runner.worker_settled(call)
            scenario.kernel.agent_exited(call, process, 0)
            assert runner.worker_settled(call)
    else:
        raise AssertionError("first Ticket did not reach normal CI")
    # Reopening the independent external Ticket makes fresh work eligible;
    # only Core interprets it and decides when to allocate its call.
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, scenario.spec.repository,
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set())
    endpoint = f"repos/{scenario.spec.repository}/issues/4"
    assert api.object(endpoint)["state"] == "closed"
    reopened = api.write(endpoint, "PATCH", {"state": "open"})
    assert reopened.returncode == 0 and api.object(endpoint)["state"] == "open"
    scenario.requirements = independent_source()
    assert next(issue for issue in ports.source(1).issues if issue.number == 4).state == "OPEN"
    now[0] = 105.25
    entered, polled = threading.Event(), threading.Event()
    delivery = ports.delivery
    def read(request):
        facts = delivery(request)
        if request.check_head is not None and facts.pr is not None and facts.pr.source_ref == "refs/heads/ticket-3":
            if entered.is_set():
                with scenario.kernel.storage.transaction(write=False) as tx:
                    assert len(tx.rows("SELECT id FROM leases")) == 1
                    assert tx.one("SELECT integration_holder FROM runs")[0] == "ticket:3"
                polled.set()
            return replace(facts, checks=Checks(facts.pr.number, facts.pr.head.sha, "none" if polled.is_set() else "pending"))
        return facts
    monkeypatch.setattr(ports, "delivery", read)
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    def execute(kernel, preparation, **kwargs):
        call = prepare_scripted_agent(kernel, preparation).call
        assert call is not None
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"](call)
        process = {"pid": 5252, "start_token": "synthetic-worker", "pgid": 5252, **host}
        kernel.agent_started(call, process)
        kwargs["authorize_source"](call)
        kernel.agent_source_ready(call)
        try:
            if call.subject == "ticket:4" and not entered.is_set():
                now[0] = 111.25
                entered.set()
                assert polled.wait(8), "CI poll stalled behind the only Agent worker"
            scenario.select(call.subject)
            kernel.receive(call, scenario.agent(call))
        finally:
            kernel.agent_exited(call, process, 0)
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex', execute)
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', Notifications)
    # The 8-second handshake above verifies CI progress alongside the Agent.
    # This 90-second bound only prevents the full Git/Host-to-Final fixture hanging.
    def expired(_signum, _frame):
        raise TimeoutError("CI/Agent full-flow fixture exceeded its 90-second watchdog")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("CI/Agent concurrency watchdog requires an inactive ITIMER_REAL")
    previous_handler = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, 90)
        assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    assert entered.is_set() and polled.is_set()
    assert scenario.kernel.status(scenario.kernel.source_status()["final_subject"])["phase"] == "approval"


def test_real_core_future_failure_and_raw_cannot_release_unknown_process(tmp_path, monkeypatch):
    import pytest
    from agent_run.runtime.execution import ProcessExitUnknown
    scenario = GraphScenario(tmp_path / "unknown", independent_source())
    host = _admit_host(scenario, tmp_path)
    ports = Ports(scenario)
    selected = []
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    def execute(kernel, preparation, **kwargs):
        call = prepare_scripted_agent(kernel, preparation).call
        assert call is not None
        selected.append(call)
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"](call)
        process = {"pid": 6262, "start_token": "synthetic-worker", "pgid": 6262, **host}
        kernel.agent_started(call, process)
        kwargs["authorize_source"](call)
        kernel.agent_source_ready(call)
        scenario.select(call.subject)
        kernel.receive(call, scenario.agent(call))
        raise RuntimeError("synthetic exit observation unavailable")
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex', execute)
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', Notifications)
    with pytest.raises(ProcessExitUnknown):
        executor.run(scenario.kernel, scenario.execution, {})
    assert len(selected) == 1
    call = selected[0]
    with pytest.raises(KernelError, match="executor_fenced"):
        OperationRunner(scenario.kernel, scenario.execution, lambda binding: ports)
    assert not scenario.kernel.worker_settled(call)
    assert scenario.kernel.status(call.subject)["pending_return"]
    assert call.lease_id in scenario.kernel.operation_state().active
