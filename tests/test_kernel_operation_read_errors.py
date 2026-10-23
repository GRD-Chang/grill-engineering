"""Physical read outages become Core waits without importing concrete adapters."""
from agent_run.core.operation_runner import OperationRunner
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run.core.kernel import Kernel
from agent_run.model import ReadWait
from test_kernel_operation_runner import setup


def test_cold_github_import_does_not_cycle_through_operation_runner():
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", 'import agent_run.adapters.github; from agent_run.core.operation_runner import OperationRunner'],
        env={**os.environ, "PYTHONPATH": source}, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_bootstrap_read_outage_retains_original_wait_and_lease(tmp_path):
    from agent_run.core.operation_contract import ReadUnavailable
    s, ports, runner = setup(tmp_path, dependent=True)
    for _ in range(12):
        step = runner.run_once()
        if step.call is not None:
            call = step.call
            break
    else:
        raise AssertionError("Core did not allocate the original call")
    assert s.kernel.dispatch_agent(call)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "synthetic source outage")
    ports.source = unavailable
    with pytest.raises(ReadWait) as first:
        runner.authorize_agent(call)
    assert first.value.retry_at == 105.25
    assert runner.state().active == (call.lease_id,)
    assert not runner.worker_settled(call)
    now[0] = 101.25
    with pytest.raises(ReadWait) as second:
        runner.authorize_agent(call)
    assert second.value.retry_at == first.value.retry_at
    s.kernel = Kernel(s.kernel.storage.path)
    s.kernel.clock = lambda: now[0]
    runner = OperationRunner(s.kernel, s.execution, lambda binding: ports)
    now[0] = 700.25
    with pytest.raises(ReadWait) as expired:
        runner.authorize_agent(call)
    assert expired.value.step.reason == "supervision_timeout"
    assert runner.state().active == (call.lease_id,)
    assert not runner.worker_settled(call)


def test_source_read_window_survives_stop_and_a_proven_executor_handoff(tmp_path):
    from agent_run.core.operation_contract import ReadUnavailable

    s, ports, runner = setup(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "Same source outage")

    ports.source = unavailable
    assert runner.run_once().retry_at == 105.25
    now[0] = 200.25
    s.kernel.control("stop", "stop-original-reader")
    s.kernel.confirm_executor_exit(s.execution)
    s.kernel.control("resume", "resume-same-read")
    s.execution = s.kernel.claim_executor("replacement-reader")
    runner = OperationRunner(s.kernel, s.execution, lambda binding: ports)
    assert runner.run_once().kind == "waiting"
    with s.kernel.storage.transaction(write=False) as tx:
        wait = tx.one("SELECT started_at,deadline,retry_count FROM waits")
        assert tuple(wait) == (100.25, 700.25, 2)
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")
    now[0] = 700.25
    assert runner.run_once().reason == "supervision_timeout"
    with s.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
        assert not tx.rows("SELECT id FROM attempts")


@pytest.mark.parametrize("physical_read", ["source", "delivery"])
def test_slow_successful_read_cannot_allocate_past_the_original_wait_deadline(tmp_path, physical_read):
    from agent_run.core.operation_contract import ReadUnavailable

    s, ports, runner = setup(tmp_path)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    source, delivery = ports.source, ports.delivery

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "Original source outage")

    ports.source = unavailable
    assert runner.run_once().retry_at == 105.25
    ports.source = source
    read = source if physical_read == "source" else delivery

    def slow(request):
        result = read(request)
        now[0] = 701.25
        return result

    setattr(ports, physical_read, slow)
    now[0] = 105.25
    expired = runner.run_once()
    assert expired.reason == "supervision_timeout" and expired.call is None
    with s.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")
