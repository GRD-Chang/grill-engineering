"""Actual Core operation runner through Host; Agent and GitHub are synthetic."""
from agent_run_kernel import executor
from agent_run_kernel.offline import GraphScenario
from test_kernel_operation_runner import Ports
from test_kernel_requirements import independent_source


class Notifications:
    def __init__(self, *_args):
        pass
    def observe_committed(self):
        pass
    def close(self, **_kwargs):
        pass


def test_github_reader_can_be_imported_cold_in_an_independent_process():
    import os
    from pathlib import Path
    import subprocess
    import sys
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-c", "import agent_run.github"],
                            env={**os.environ, "PYTHONPATH": str(source)},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_host_real_core_original_merge_readback_final_approval_and_cleanup(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path / "host", independent_source())
    ports = Ports(scenario)
    scenario.port.lose_response.add("merge")
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)

    def execute(kernel, call, **kwargs):
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"]()
        process = {"pid": 4242, "start_token": "synthetic-worker", "pgid": 4242,
                   "host_pid": 4241, "host_start_token": "synthetic-host"}
        kernel.agent_started(call, process)
        try:
            scenario.select(call.subject)  # Metadata for the call Core already allocated.
            kernel.receive(call, scenario.agent(call))
        finally:
            kernel.agent_exited(call, process, 0)

    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex", execute)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", Notifications)
    assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    final = scenario.kernel.source_status()["final_subject"]
    assert final is not None and scenario.kernel.status(final)["phase"] == "approval"
    assert len(scenario.kernel.source_status()["completions"]) == 2
    runner = scenario.kernel.operation_runner(scenario.execution, lambda binding: ports)
    scenario.kernel.control("approve", "approve-final", subject=final, world=runner.observe(final))
    assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM subjects WHERE phase!='complete'")
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT subject FROM resource_cleanup WHERE state!='complete'")
        assert len(tx.rows("SELECT * FROM final_barriers")) == 1
    assert sum(item["operation"] == "merge" for item in scenario.port.writes()) == 3


def test_bootstrap_source_outage_keeps_original_call_and_bounded_read_wait(tmp_path, monkeypatch):
    import pytest
    from agent_run.github import GitHubReadError
    from agent_run_kernel.model import ReadWait
    scenario = GraphScenario(tmp_path / "bootstrap", independent_source())
    ports = Ports(scenario)
    runner = scenario.kernel.operation_runner(scenario.execution, lambda binding: ports)
    for _ in range(30):
        step = runner.run_once()
        if step.call is not None:
            call = step.call
            break
    else:
        raise AssertionError("normal Development call was not allocated")
    assert scenario.kernel.dispatch_agent(call)
    def unavailable(_parent):
        raise GitHubReadError("github_read_failed", "synthetic source outage")
    monkeypatch.setattr(ports, "source", unavailable)
    with pytest.raises(ReadWait) as waiting:
        runner.authorize_agent(call)
    assert waiting.value.step.kind == "waiting" and waiting.value.retry_at is not None
    assert not runner.worker_settled(call)
    assert scenario.kernel.status(call.subject)["pending_attempt"] == call.attempt_id
    assert scenario.kernel.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1


def test_real_core_n1_ci_poll_advances_during_independent_long_agent(tmp_path, monkeypatch):
    from dataclasses import replace
    import threading
    from agent_run_kernel.model import Checks, Policy
    from test_kernel_operation_runner import settle_agent
    source = independent_source()
    source = replace(source, issues=tuple(replace(issue, state="CLOSED") if issue.number == 4 else issue for issue in source.issues))
    scenario = GraphScenario(tmp_path / "concurrent", source, policy=Policy(concurrency=1))
    ports = Ports(scenario)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    runner = scenario.kernel.operation_runner(scenario.execution, lambda binding: ports)
    for _ in range(80):
        if scenario.kernel.status("ticket:3")["phase"] == "checks":
            break
        step = runner.run_once()
        if step.call is not None:
            settle_agent(scenario, runner, step.call)
    else:
        raise AssertionError("first Ticket did not reach normal CI")
    # Reopening the independent external Ticket makes fresh work eligible;
    # only Core interprets it and decides when to allocate its call.
    scenario.requirements = independent_source()
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
    def execute(kernel, call, **kwargs):
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"]()
        process = {"pid": 5252, "start_token": "synthetic-worker", "pgid": 5252,
                   "host_pid": 5251, "host_start_token": "synthetic-host"}
        kernel.agent_started(call, process)
        try:
            if call.subject == "ticket:4" and not entered.is_set():
                now[0] = 111.25
                entered.set()
                assert polled.wait(8), "CI poll stalled behind the only Agent worker"
            scenario.select(call.subject)
            kernel.receive(call, scenario.agent(call))
        finally:
            kernel.agent_exited(call, process, 0)
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex", execute)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", Notifications)
    assert executor.run(scenario.kernel, scenario.execution, {}) == 0
    assert entered.is_set() and polled.is_set()
    assert scenario.kernel.status(scenario.kernel.source_status()["final_subject"])["phase"] == "approval"


def test_real_core_future_failure_and_raw_cannot_release_unknown_process(tmp_path, monkeypatch):
    import pytest
    from agent_run_kernel.execution import ProcessExitUnknown
    scenario = GraphScenario(tmp_path / "unknown", independent_source())
    ports = Ports(scenario)
    selected = []
    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    def execute(kernel, call, **kwargs):
        selected.append(call)
        assert kernel.dispatch_agent(call)
        kwargs["authorize_source"]()
        process = {"pid": 6262, "start_token": "synthetic-worker", "pgid": 6262,
                   "host_pid": 6261, "host_start_token": "synthetic-host"}
        kernel.agent_started(call, process)
        scenario.select(call.subject)
        kernel.receive(call, scenario.agent(call))
        raise RuntimeError("synthetic exit observation unavailable")
    monkeypatch.setattr("agent_run_kernel.codex_execution.execute_selected_codex", execute)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", Notifications)
    with pytest.raises(ProcessExitUnknown):
        executor.run(scenario.kernel, scenario.execution, {})
    assert len(selected) == 1
    call = selected[0]
    runner = scenario.kernel.operation_runner(scenario.execution, lambda binding: ports)
    assert not runner.worker_settled(call)
    assert scenario.kernel.status(call.subject)["pending_return"]
    assert call.lease_id in runner.state().active
