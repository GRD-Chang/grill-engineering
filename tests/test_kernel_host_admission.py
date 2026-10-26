"""Task-wide admission uses real databases and durable launch identities."""
from support.core_operations import task_run

from pathlib import Path
from contextlib import contextmanager
from dataclasses import replace
import json
import subprocess
import sys
from typing import Iterator

import pytest

from agent_run.model import ParentIssue, Requirements
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from support.core_operations import CoreOperations
from support.offline import GraphScenario, Scenario
from agent_run.storage.sqlite import Transaction


def test_host_preflight_rejects_before_action_or_task_binding(tmp_path: Path) -> None:
    first = task_run(tmp_path, "first")[0]
    other = task_run(tmp_path, "other")[0]

    def unavailable() -> None:
        raise KernelError("user_bus_unavailable")

    with pytest.raises(KernelError, match="user_bus_unavailable"):
        first.admit_host("start", kind="run", command=("python", "-m", "native-host"),
                         environment_carrier=tmp_path / "carrier", preflight=unavailable)
    with first.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 0
        assert tx.one("SELECT executor FROM runs")[0] is None
    # A rejected preflight did not claim this Task for the first database.
    launch = other.admit_host("start-other", kind="run", command=("python", "-m", "native-host"),
                              environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert launch.run_id == "other"


def test_same_task_other_run_cannot_claim_an_allocated_or_unknown_host(tmp_path: Path) -> None:
    first = task_run(tmp_path, "first")[0]
    other = task_run(tmp_path, "other")[0]
    independent = task_run(tmp_path, "independent", parent=2)[0]
    arguments = {"kind": "run", "command": ("python", "-m", "native-host"),
                 "environment_carrier": tmp_path / "carrier", "preflight": lambda: None}
    launch = first.admit_host("start", **arguments)
    # No Invocation, process or dispatch yet; the bound Run still owns its Task.
    with pytest.raises(KernelError, match="task_run_busy"):
        other.admit_host("other-start", **arguments)
    assert independent.admit_host("independent-start", **arguments).run_id == "independent"
    assert first.admit_host("start", **arguments) == launch
    assert first.dispatch_host(launch) is True
    assert Kernel(first.storage.path).dispatch_host(launch) is False
    assert first.observe_host(launch, HostObservation("unknown", launch.unit_name, evidence="manager reply lost")) == "unknown"
    with pytest.raises(KernelError, match="task_run_busy"):
        other.admit_host("other-start", **arguments)
    assert first.dispatch_host(launch) is False


def test_host_observation_cannot_replace_the_first_systemd_invocation(tmp_path: Path) -> None:
    kernel = task_run(tmp_path, "first")[0]
    launch = kernel.admit_host("start", kind="run", command=("python", "-m", "native-host"),
                              environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(launch)
    running = HostObservation("running", launch.unit_name, "original", "/original-cgroup", "boot", 42, "start",
                              evidence="controlled manager and proc identity agree")
    assert kernel.observe_host(launch, running) == "running"
    replacement = HostObservation("exited", launch.unit_name, "replacement", "/original-cgroup", "boot", 42, "start",
                                  0, 110.0, "replacement exited and cgroup empty")
    assert kernel.observe_host(launch, replacement) == "unknown"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] == launch.owner
    original = HostObservation("exited", launch.unit_name, "original", "/original-cgroup", "boot", 42, "start",
                               0, 111.0, "original exited and cgroup empty")
    assert kernel.observe_host(launch, original) == "exited"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] is None


def test_two_processes_cannot_admit_different_runs_for_one_task(tmp_path: Path) -> None:
    first, other = task_run(tmp_path, "first")[0], task_run(tmp_path, "other")[0]
    script = """import json,sys
from pathlib import Path
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
sys.stdin.read(1)
kernel = Kernel(Path(sys.argv[1]))
try:
    request = kernel.admit_host('start', kind='run', command=('native-host',), environment_carrier=Path(sys.argv[2]), preflight=lambda: None)
    print(json.dumps({'run': request.run_id, 'dispatch': kernel.dispatch_host(request)}))
except KernelError as error:
    print(json.dumps({'error': error.code}))
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(kernel.storage.path), str(tmp_path / "carrier")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for kernel in (first, other)]
    try:
        for child in children:
            assert child.stdin is not None
            child.stdin.write("1")
            child.stdin.flush()
        outputs = [child.communicate(timeout=15) for child in children]
        assert all(child.returncode == 0 for child in children), outputs
        facts = [json.loads(output) for output, _ in outputs]
        assert sum(fact.get("dispatch") is True for fact in facts) == 1
        assert sum(fact.get("error") == "task_run_busy" for fact in facts) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_bound_run_survives_admission_transaction_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, other = task_run(tmp_path, "first")[0], task_run(tmp_path, "other")[0]
    transaction = first.storage.transaction

    @contextmanager
    def unavailable(*, write: bool = True) -> Iterator[Transaction]:
        if write:
            raise OSError("Admission database temporarily unavailable")
        with transaction(write=False) as tx:
            yield tx

    arguments = {"kind": "run", "command": ("native-host",), "environment_carrier": tmp_path / "carrier", "preflight": lambda: None}
    with monkeypatch.context() as patch:
        patch.setattr(first.storage, "transaction", unavailable)
        with pytest.raises(OSError, match="Admission database"):
            first.admit_host("start", **arguments)
    with pytest.raises(KernelError, match="task_run_busy"):
        other.admit_host("other", **arguments)
    with first.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 0
    request = Kernel(first.storage.path).admit_host("start", **arguments)
    assert first.dispatch_host(request)


def test_observed_running_requires_actual_usage_handoff_before_work(tmp_path: Path) -> None:
    kernel, ports = task_run(tmp_path, "first")
    request = kernel.admit_host("start", kind="run", command=("native-host",), environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    observation = HostObservation("running", request.unit_name, "original", "/host", "boot", 42, "start")
    assert kernel.observe_host(request, observation) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    with pytest.raises(KernelError, match="host_usage_unproven"):
        CoreOperations(kernel, execution, lambda binding: ports).once()
    assert kernel.observe_host(request, replace(observation, usage_ready=True, evidence="controlled Host owns shared usage lease")) == "running"
    assert CoreOperations(kernel, execution, lambda binding: ports).once().kind == "agent"


def test_complete_run_cannot_be_rebound_until_host_and_usage_retire(tmp_path: Path) -> None:
    scenario = GraphScenario(tmp_path / "delivery", Requirements(ParentIssue(1, "Parent", "Requirement"), ()))
    # Retire the fixture's trusted Executor before entering the managed seam.
    scenario.kernel.control("stop", "retire-fixture")
    scenario.kernel.confirm_executor_exit(scenario.execution)
    scenario.kernel.control("resume", "prepare-managed")
    request = scenario.kernel.admit_host("start", kind="run", command=("native-host",),
                                        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert scenario.kernel.dispatch_host(request)
    running = HostObservation("running", request.unit_name, "original", "/host", "boot", 42, "start", usage_ready=True)
    assert scenario.kernel.observe_host(request, running) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    assert scenario.run(approve=True).kind == "complete"
    other = Kernel.create(tmp_path / "other.sqlite3", replace(scenario.spec, run_id="replacement"))
    arguments = {"kind": "run", "command": ("native-host",), "environment_carrier": tmp_path / "carrier", "preflight": lambda: None}
    assert scenario.kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence="manager unavailable")) == "unknown"
    with pytest.raises(KernelError, match="task_run_busy"):
        other.admit_host("replace", **arguments)
    exited = HostObservation("exited", request.unit_name, "original", "/host", "boot", exit_status=0, ended_at=111.0)
    assert scenario.kernel.observe_host(request, exited) == "exited"
    with pytest.raises(KernelError, match="task_run_busy"):
        other.admit_host("replace", **arguments)
    assert scenario.kernel.observe_host(request, replace(exited, usage_released=True, evidence="controlled manager and guardian released shared usage")) == "exited"
    assert other.admit_host("replace", **arguments).run_id == "replacement"
