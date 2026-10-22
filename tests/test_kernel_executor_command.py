"""The actual service command preserves its selected Python and loader input."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from agent_run_kernel import executor
from agent_run_kernel import codex_execution
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.model import Commit, Delivery, DeliveryKind, KernelError, RunSpec


def test_host_command_seals_library_path_without_resolving_venv_or_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "venv" / "bin" / "python"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(selected))
    database, lock = tmp_path / "native.sqlite", tmp_path / "install.lock"
    environment = {"LD_LIBRARY_PATH": "/private/sqlite/lib:/another path/lib", "GH_TOKEN": "secret-sentinel"}
    command = executor.command(database, "original-action", lock, environment)
    assert command == (
        "/usr/bin/env", "LD_LIBRARY_PATH=/private/sqlite/lib:/another path/lib", str(selected),
        "-m", "agent_run_kernel.executor", "--database", str(database),
        "--action", "original-action", "--runner-lock", str(lock),
    )
    assert "secret-sentinel" not in " ".join(command)
    assert executor.command(database, "original-action", lock, {})[0] == str(selected)
    assert not database.exists() and not lock.exists()


def test_unmanaged_executor_entry_refuses_before_accessing_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    database, lock = tmp_path / "never-created.sqlite", tmp_path / "never-created.lock"
    assert executor.main(["--database", str(database), "--action", "unadmitted", "--runner-lock", str(lock)]) == 1
    assert "managed_host_required" in capsys.readouterr().err
    assert not database.exists() and not lock.exists()


def test_native_executor_enumerates_more_than_one_query_page(tmp_path: Path) -> None:
    from agent_run_kernel.model import Issue, ParentIssue, Requirements
    numbers = tuple(range(2, 132))
    deliveries = tuple(Delivery(f"ticket:{n}", DeliveryKind.TICKET, n, "untrusted", Commit("base", "tree"),
                                f"refs/heads/ticket-{n}", "refs/heads/run") for n in numbers)
    deliveries += (Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "untrusted", Commit("base", "tree"),
                            "refs/heads/run", "refs/heads/main"),)
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", numbers), tuple(
        Issue(n, f"Ticket {n}", "Required change", "OPEN", frozenset({"ready-for-agent"})) for n in numbers))
    kernel = Kernel.create(tmp_path / "wide.sqlite", RunSpec("wide", "example/project", 1, "parent", "graph", deliveries),
                           requirements=source)
    before = kernel.history("ticket:131")
    state = kernel.operation_state()
    assert state.disposition == "ready" and state.active == ()
    assert kernel.history("ticket:131") == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM subjects WHERE kind='ticket'")[0] == 130
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")


def test_prompt_preparation_failure_durably_releases_only_its_original_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from agent_run_kernel.offline import GraphScenario
    from support.core_operations import CoreOperations
    from test_kernel_requirements import independent_source
    scenario = GraphScenario(tmp_path / "preparation", independent_source())
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    runner = operations.runner
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None
    driver = executor._Executor(kernel, runner, {})

    def invalid_prompt(call: object) -> str:
        raise KernelError("prompt_resources_unavailable")

    def unexpected_worker(*arguments: object, **keywords: object) -> None:
        raise AssertionError("No process may start after prompt preparation failed")

    monkeypatch.setattr(codex_execution, "execute_selected_codex", unexpected_worker, raising=False)
    monkeypatch.setattr(executor, "render_role_prompt", invalid_prompt)
    driver.agent(call, threading.Event())
    assert runner.worker_settled(call)
    status = kernel.status(call.subject)
    assert status["pending_attempt"] == call.attempt_id
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert "worker_unavailable" in status["gates"]
    assert kernel.operation_state().active == ()


def test_stop_between_dispatch_and_write_does_not_invent_a_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run_kernel.offline import GraphScenario
    from support.core_operations import CoreOperations, scripted_return
    from test_kernel_requirements import independent_source
    scenario = GraphScenario(tmp_path / "stop", independent_source())
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    ports = operations.runner.ports
    original = ports.execute
    writes: list[str] = []
    selected = []

    def execute(effect, authorize):
        if effect.operation != "candidate":
            return original(effect, authorize)
        selected.append(effect)
        authorize()
        kernel.control("stop", "operator-stop")
        try:
            authorize()
        except KernelError as denied:
            assert denied.code == "executor_fenced"
            raise
        writes.append("unexpected")
        raise AssertionError("Stop must reject the second write guard")

    monkeypatch.setattr(ports, "execute", execute)
    for _ in range(30):
        step = operations.once()
        if step.call is not None:
            scripted_return(kernel, operations.runner, step.call, scenario.agent)
        if step.kind == "paused":
            assert step.reason == "executor_fenced"
            break
    else:
        raise AssertionError("The normal candidate boundary was not reached")
    assert len(selected) == 1 and writes == []
    assert not any(event["kind"] == "effect_failed" for event in kernel.history(selected[0].subject))
