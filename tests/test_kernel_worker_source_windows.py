"""Real source callbacks retain one call and one launch across concurrent reads."""
from agent_run.core.operation_runner import OperationRunner
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import sys
from threading import Event, Thread, current_thread

import pytest

from agent_run.errors import KernelError
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.runtime.execution import execute
from agent_run.adapters.remote import RemoteEffects
from agent_run.adapters.source import read_requirements
from support.agent_continuation import _allocated_worker, RAW

@pytest.mark.parametrize("window", [1, 2], ids=["before_popen", "before_release"])
@pytest.mark.parametrize("change", ["labels", "requirement", "upstream"])
def test_worker_source_callback_preserves_authority_and_exact_process_settlement(tmp_path, monkeypatch, window, change):
    import agent_run.adapters.remote as external

    scenario, operations, state, call = _allocated_worker(tmp_path, monkeypatch)
    kernel = scenario.kernel
    canary = tmp_path / "actual-worker-starts"
    reached, release = Event(), Event()
    callbacks = 0
    outcomes, failures = [], []
    held_pidfd = None

    def read(reader, parent):
        observed = read_requirements(reader, parent)
        if current_thread() is thread and callbacks == window:
            reached.set()
            assert release.wait(10), "bounded callback release"
            if change == "upstream":
                return read_requirements(reader, parent)  # actual failed gh read, after the other commit
        return observed

    monkeypatch.setattr(external, "read_requirements", read)

    def authorize_source():
        nonlocal callbacks
        callbacks += 1
        operations.runner.observe(call.subject)

    def run():
        try:
            outcomes.append(execute(kernel, call, arguments=[sys.executable, "-I", "-c",
                f"from pathlib import Path;import sys;Path({str(canary)!r}).open('a').write('ran\\n');sys.stdout.buffer.write({RAW!r})"],
                cwd=Path(call.context["checkout"]), prompt="", environment=os.environ, timeout=15,
                authorize_source=authorize_source))
        except Exception as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert reached.wait(10), "the requested real callback reached external I/O"
        assert not canary.exists()
        with kernel.storage.transaction(write=False) as tx:
            output = dict(tx.one("SELECT * FROM invocation_outputs WHERE invocation=? AND ordinal=?",
                                 (call.invocation_id, call.output_ordinal)))
            assert tx.rows("SELECT id FROM leases WHERE id=?", (call.lease_id,))
            assert output["dispatch_state"] == ("sent" if window == 1 else "started")
            if window == 2:
                process = json.loads(tx.get(output["process_identity"], call.subject))
                held_pidfd = os.pidfd_open(process["pid"])
                assert not select.select([held_pidfd], [], [], 0)[0]
        document = json.loads(state.read_text())
        if change == "requirement":
            document["source_graph"]["issues"]["2"]["body"] = "A changed task requires a new generation"
        else:
            document["source_graph"]["issues"]["2"]["labels"].append("triaged")
        state.write_text(json.dumps(document))
        # A separate actual source reader, as in a concurrent public control,
        # commits while the original Host callback is outside its transaction.
        port = operations.runner.ports
        assert isinstance(port, RemoteEffects)
        current = RemoteEffects(port.root, port.repository, runner_identity=port.identity, remote=port.remote)
        world = OperationRunner(kernel, scenario.execution, lambda binding: current).observe(call.subject)
        if change == "requirement":
            original = kernel.delivery(call.subject)
            replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                                  checkout=str(Path(original.checkout).with_name("replacement-g2")))
            with pytest.raises(KernelError, match="generation_unsettled"):
                kernel.requeue(call.subject, "too-early", replacement, world)
        elif change == "upstream":
            document = json.loads(state.read_text())
            document["read_error"] = True
            state.write_text(json.dumps(document))
    finally:
        release.set()
        thread.join(20)
        if held_pidfd is not None:
            try:
                assert select.select([held_pidfd], [], [], 0)[0], "the original bootstrap really exited"
            finally:
                os.close(held_pidfd)
    assert not thread.is_alive()
    assert kernel.operation_state().active == ()
    if change == "labels":
        assert failures == []
        assert len(outcomes) == 1 and outcomes[0] is not None and outcomes[0].returncode == 0
        assert canary.read_text() == "ran\n"
        status = kernel.status(call.subject)
        assert status["pending_return"] is True and status["gates"] == []
        assert status["budgets"]["requirements"]["developments_used"] == 1
        assert sum(event["kind"] == "worker_started" for event in kernel.history(call.subject)) == 1
    else:
        assert not canary.exists() and len(failures) == 1
        assert isinstance(failures[0], ReadUnavailable if change == "upstream" else KernelError)
        if change == "requirement":
            assert kernel.status(call.subject)["gates"] == ["requeue_required"]
            assert kernel.requeue(call.subject, "replace-after-exit", replacement, world) == "applied"
            assert kernel.status(call.subject)["generation"] == 2
            assert execute(kernel, call, arguments=[sys.executable, "-c", "raise SystemExit('old call replayed')"],
                           cwd=tmp_path, prompt="", environment=os.environ, timeout=5) is None
