"""Converging remote reads wait without granting or replaying a write."""
import json
import os
from pathlib import Path
import select
import sys
from threading import Event, Thread

import pytest

from agent_run.runtime.execution import execute
from agent_run.runtime.executor import _make_ports
from support.core_operations import CoreOperations
from agent_run.errors import KernelError
from agent_run.model import ReadWait
from test_kernel_remote_effects import _fixture, _publish_pr, _source_kernel
from test_kernel_worker_source_windows import _allocated_worker, RAW


def test_executor_waits_for_real_pr_and_remote_ref_observations_to_converge(tmp_path, monkeypatch):
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    _effect, published = _publish_pr(port, push)
    kernel = _source_kernel(tmp_path, port, push)
    now = 1000.25
    kernel.clock = lambda: now
    execution = kernel.claim_executor("read-supervision")
    operations = CoreOperations(kernel, execution, _make_ports)
    # Git already has the published head. The independently read PR has not
    # converged yet: this is the adapter's actual github_observation_pending
    # producer, not an injected exception or a contradictory repository binding.
    document = json.loads(state.read_text())
    document["prs"][0]["head"]["sha"] = push.base.sha
    state.write_text(json.dumps(document))
    previous_calls = len(calls.read_text().splitlines())

    step = operations.once()

    assert step.kind == "waiting" and step.call is None and step.effect is None
    assert step.retry_at == now + 5
    assert kernel.operation_state().wake_at == step.retry_at
    assert kernel.status("parent:1")["gates"] == []
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM effects")
        window = tx.one("SELECT started_at,deadline,next_retry_at FROM waits WHERE subject=? AND kind=?",
                        ("parent:1", "github_convergence"))
        assert window["started_at"] == now and window["deadline"] == now + 600
        assert window["next_retry_at"] == step.retry_at
    observed_calls = [json.loads(line) for line in calls.read_text().splitlines()][previous_calls:]
    assert observed_calls and not any("--method" in call for call in observed_calls)
    first_wait_calls = calls.read_text()
    repeated = operations.once()
    assert repeated.kind == "waiting" and repeated.retry_at == step.retry_at
    assert calls.read_text() == first_wait_calls
    assert kernel.operation_state().wake_at == step.retry_at
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT deadline FROM waits WHERE subject=? AND kind=?",
                      ("parent:1", "github_convergence"))[0] == now + 600
    # A later read can converge without changing the original PR or dispatching
    # anything. The ordinary loop, rather than an execute retry, owns that read.
    document["prs"][0]["head"]["sha"] = published.head.sha
    state.write_text(json.dumps(document))
    now = step.retry_at
    assert operations.runner.observe("parent:1").pr == published


@pytest.mark.parametrize("window", [1, 2], ids=["before_popen", "before_release"])
@pytest.mark.parametrize("outcome", ["converge", "stop", "timeout"])
def test_worker_read_wait_keeps_original_call_and_one_process(tmp_path, monkeypatch, window, outcome):
    scenario, operations, state, call = _allocated_worker(tmp_path, monkeypatch)
    kernel = scenario.kernel
    now = [kernel.clock()]
    kernel.clock = lambda: now[0]
    reached, release = Event(), Event()
    canary = tmp_path / "actual-worker-starts"
    callbacks = 0
    waits, results, failures = [], [], []
    held_pidfd = None

    def authorize_source():
        nonlocal callbacks
        callbacks += 1
        if callbacks == window:
            document = json.loads(state.read_text())
            document["read_error"] = True
            state.write_text(json.dumps(document))
        try:
            operations.runner.authorize_agent(call)
        except ReadWait as waiting:
            waits.append(waiting.step)
            reached.set()
            assert release.wait(10), "bounded actual source-read wait release"
            raise

    def run():
        try:
            results.append(execute(kernel, call, arguments=[sys.executable, "-I", "-c",
                f"from pathlib import Path;import sys;Path({str(canary)!r}).open('a').write('ran\\n');sys.stdout.buffer.write({RAW!r})"],
                cwd=Path(call.context["checkout"]), prompt="", environment=os.environ, timeout=15,
                authorize_source=authorize_source))
        except Exception as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert reached.wait(10), "actual fake-gh read failed in the selected process window"
        assert waits[0].kind == "waiting" and waits[0].retry_at is not None
        assert not canary.exists()
        status = kernel.status(call.subject)
        assert status["gates"] == []
        assert status["budgets"]["requirements"]["developments_used"] == 1
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT process_identity,dispatch_state FROM invocation_outputs WHERE invocation=? AND ordinal=?",
                         (call.invocation_id, call.output_ordinal))
            assert row["dispatch_state"] == ("sent" if window == 1 else "started")
            assert tx.rows("SELECT id FROM leases WHERE id=?", (call.lease_id,))
            if window == 2:
                identity = json.loads(tx.get(row["process_identity"], call.subject))
                held_pidfd = os.pidfd_open(identity["pid"])
                assert not select.select([held_pidfd], [], [], 0)[0]
        if outcome == "converge":
            document = json.loads(state.read_text())
            document.pop("read_error")
            state.write_text(json.dumps(document))
            now[0] = waits[0].retry_at
        elif outcome == "stop":
            kernel.control("stop", "stop-original-read-wait")
        else:
            now[0] += 600
    finally:
        release.set()
        thread.join(20)
        if held_pidfd is not None:
            try:
                assert select.select([held_pidfd], [], [], 0)[0], "the original bootstrap actually exited"
            finally:
                os.close(held_pidfd)
    assert not thread.is_alive()
    expected_starts = 1 if outcome == "converge" else window - 1
    assert sum(event["kind"] == "worker_started" for event in kernel.history(call.subject)) == expected_starts
    assert kernel.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1
    assert kernel.operation_state().active == ()
    if outcome != "converge":
        assert results == [] and len(failures) == 1
        assert isinstance(failures[0], ReadWait if outcome == "timeout" else KernelError)
        assert not canary.exists()
        status = kernel.status(call.subject)
        if outcome == "timeout":
            assert "supervision_timeout" in status["gates"]
        else:
            assert status["stopped"]
        assert "worker_unavailable" not in status["gates"]
        if outcome == "timeout" and window == 1:
            with kernel.storage.transaction(write=False) as tx:
                evidence = tx.one("SELECT start_evidence FROM invocation_outputs WHERE invocation=? AND ordinal=?",
                                  (call.invocation_id, call.output_ordinal))[0]
                assert json.loads(tx.get(evidence, call.subject))["code"] == "github_read_wait"
        return
    assert failures == []
    assert len(results) == 1 and results[0].returncode == 0
    assert canary.read_text() == "ran\n"
    assert callbacks == 3
    assert kernel.status(call.subject)["pending_return"] is True
