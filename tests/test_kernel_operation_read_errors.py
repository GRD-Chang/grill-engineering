"""Physical read outages become Core waits without importing concrete adapters."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run_kernel import Kernel
from agent_run_kernel.model import ReadWait
from test_kernel_operation_runner import setup


def test_cold_github_import_does_not_cycle_through_operation_runner():
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", "import agent_run.github; from agent_run_kernel.operation_runner import OperationRunner"],
        env={**os.environ, "PYTHONPATH": source}, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_bootstrap_read_outage_retains_original_wait_and_lease(tmp_path):
    from agent_run_kernel.operation_runner import ReadUnavailable
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
    runner = s.kernel.operation_runner(s.execution, lambda binding: ports)
    now[0] = 700.25
    with pytest.raises(ReadWait) as expired:
        runner.authorize_agent(call)
    assert expired.value.step.reason == "supervision_timeout"
    assert runner.state().active == (call.lease_id,)
    assert not runner.worker_settled(call)
