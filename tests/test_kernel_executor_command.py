"""The actual service command preserves its selected Python and loader input."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from agent_run_kernel import executor
from agent_run_kernel import codex_execution
from agent_run_kernel import local_effects
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.model import Commit, Delivery, DeliveryKind, KernelError, RunSpec, World


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
    keys = [f"ticket:{number}" for number in range(2, 132)]
    deliveries = tuple(Delivery(key, DeliveryKind.TICKET, number, "revision", Commit("base", "tree"),
                                f"refs/heads/{key}", "refs/heads/run")
                       for number, key in enumerate(keys, 2))
    kernel = Kernel.create(tmp_path / "wide.sqlite", RunSpec("wide", "example/project", 1, "parent", "graph", deliveries))
    driver = object.__new__(executor._Executor)
    driver.kernel = kernel
    ordered = driver.subjects({"work_fifo": [keys[-1]], "integration_fifo": [keys[0]]})
    assert ordered == [keys[-1], *keys[:-1]]
    assert len(ordered) == 130


def test_prompt_preparation_failure_durably_releases_only_its_original_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:2", DeliveryKind.TICKET, 2, "revision", base, "refs/heads/ticket", "refs/heads/run")
    kernel = Kernel.create(tmp_path / "run.sqlite", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,)))
    execution = kernel.claim_executor("executor")
    call = kernel.advance(delivery.key, execution, World("parent", "graph", "revision", base)).call
    assert call is not None
    driver = object.__new__(executor._Executor)
    driver.kernel = kernel
    driver.worker_identity = {"source": "host"}
    driver.environment = {}
    driver.hidden_paths = ()
    driver.hidden_environment = ()

    def invalid_prompt(call: object) -> str:
        raise KernelError("prompt_resources_unavailable")

    def unexpected_worker(*arguments: object, **keywords: object) -> None:
        raise AssertionError("No process may start after prompt preparation failed")

    monkeypatch.setattr(codex_execution, "execute_selected_codex", unexpected_worker, raising=False)
    monkeypatch.setattr(executor, "render_role_prompt", invalid_prompt)
    driver.agent(call)
    status = kernel.status(call.subject)
    assert status["pending_attempt"] == call.attempt_id
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert "worker_unavailable" in status["gates"]
    assert kernel.scheduling()["active"] == []


def test_stop_between_dispatch_and_write_does_not_invent_a_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:2", DeliveryKind.TICKET, 2, "revision", base, "refs/heads/ticket", "refs/heads/run")
    kernel = Kernel.create(tmp_path / "run.sqlite", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,)))
    execution = kernel.claim_executor("executor")
    world = World("parent", "graph", "revision", base)
    call = kernel.advance(delivery.key, execution, world).call
    assert call is not None
    kernel.receive(call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')
    assert kernel.advance(delivery.key, execution, world, allow_agent=False).kind == "progress"
    step = kernel.advance(delivery.key, execution, world)
    assert step.effect is not None and step.effect.operation == "candidate"
    writes: list[str] = []

    class Local:
        def __init__(self, *arguments: object, **keywords: object) -> None:
            pass

        def execute(self, effect: object, authorize: object) -> None:
            assert callable(authorize)
            authorize()
            kernel.control("stop", "operator-stop")
            authorize()
            writes.append("unexpected")

    monkeypatch.setattr(local_effects, "LocalEffects", Local)
    driver = object.__new__(executor._Executor)
    driver.kernel, driver.execution = kernel, execution
    driver.remote = type("Remote", (), {"git": object()})()
    driver.observe = lambda subject: world
    with pytest.raises(KernelError, match="executor_fenced"):
        driver.effect(step)
    assert writes == []
    assert not any(event["kind"] == "effect_failed" for event in kernel.history(delivery.key))
