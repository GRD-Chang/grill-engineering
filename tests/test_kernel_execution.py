"""Real local processes cross the allocated-call seam, never live Agent services."""

from support.repository_fixture import create_kernel

from dataclasses import asdict, replace
from pathlib import Path
import json
import os
import select
import signal
import subprocess
import sys
from typing import Any

import pytest

from agent_run.model import AgentCall, Commit, Delivery, DeliveryKind, Policy, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.runtime.execution import execute
from agent_run.model import Requirements
from support.core_operations import CoreOperations, ReadOnlyPorts, native_agent_fixture
from agent_run.core.operation_contract import DeliveryFacts
from support.offline import fixture_inputs
from agent_run.runtime.process_cleanup import child_subreaper


RAW = b'{"result_kind":"development","summary":"saved local work","human_blockers":null}'


def allocated(tmp_path: Path) -> tuple[Path, Kernel, AgentCall, ReadOnlyPorts]:
    path = tmp_path / "kernel.sqlite3"
    base = Commit("base", "base-tree")
    spec = RunSpec(
        "process-run", "example/repository", 1, "parent-v1", "graph-v1",
        (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                  "refs/heads/ticket-2", "refs/heads/run", checkout=str(tmp_path)),),
        Policy(ticket_rounds=1),
    )
    kernel, ports = native_agent_fixture(path, spec)
    execution = kernel.claim_executor("local-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    step = operations.until(lambda step: step.call is not None)
    assert step.call is not None
    return path, kernel, step.call, ports


def test_process_waits_for_final_guard_and_saved_return_survives_reopening(tmp_path: Path) -> None:
    path, kernel, call, ports = allocated(tmp_path)
    canary = tmp_path / "actual-agent-starts"
    runner = CoreOperations(kernel, call.execution, lambda binding: ports).runner
    assert not runner.worker_settled(call)
    checks: list[bool] = []

    def authorize_source() -> None:
        # Even the second, post-binding source check runs before Agent work.
        assert not canary.exists()
        checks.append(True)
        if len(checks) == 2:
            assert any(event["kind"] == "worker_started" and event["identity"] == call.invocation_id
                       for event in Kernel(path, run_id=kernel.storage.run_id).history(call.subject))
            with kernel.storage.transaction(write=False) as tx:
                witness = tx.one("SELECT process_identity FROM invocation_outputs WHERE invocation=? AND ordinal=?",
                                 (call.invocation_id, call.output_ordinal))[0]
                identity = json.loads(tx.get(witness, call.subject))
            assert identity["host_pid"] == os.getpid()
            for pid_field, group_field in (("pid", "cgroup"), ("host_pid", "host_cgroup")):
                memberships = Path(f"/proc/{identity[pid_field]}/cgroup").read_text().splitlines()
                assert f"0::{identity[group_field]}" in memberships
                assert identity[group_field].startswith("/")

    arguments = [sys.executable, "-I", "-c", (
        f"from pathlib import Path; import sys; "
        f"Path({str(canary)!r}).open('a').write('ran\\n'); "
        f"sys.stdout.buffer.write({RAW!r})"
    )]
    result = execute(
        kernel, call, arguments=arguments, cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5, authorize_source=authorize_source,
    )
    assert result is not None and result.returncode == 0 and result.raw == RAW
    assert result.identity["cgroup"] == result.identity["host_cgroup"]
    assert len(checks) == 2
    assert canary.read_text() == "ran\n"

    reopened = Kernel(path, run_id=kernel.storage.run_id)
    status = reopened.status(call.subject)
    assert status["pending_return"] is True
    assert reopened.worker_settled(call)
    assert runner.worker_settled(call)
    assert status["phase"] == "development"
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert execute(
        reopened, call, arguments=arguments, cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5,
    ) is None
    assert canary.read_text() == "ran\n"
    assert CoreOperations(reopened, call.execution, lambda binding: ports).once(agent_capacity=0).kind == "progress"
    assert reopened.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1


def test_stopped_epoch_can_settle_its_exact_late_process_return_without_advancing(tmp_path: Path) -> None:
    path, kernel, call, ports = allocated(tmp_path)

    class StopBeforeSettlement(Kernel):
        def receive(self, exact_call: AgentCall, raw: bytes) -> None:
            # execute has already observed actual process exit, but the raw
            # transaction has not started. This is the real revocation window.
            self.control("stop", "stop-before-raw")
            super().receive(exact_call, raw)

    kernel = StopBeforeSettlement(path, run_id=call.execution.run_id)
    result = execute(
        kernel, call, arguments=[sys.executable, "-I", "-c", f"import sys; sys.stdout.buffer.write({RAW!r})"],
        cwd=tmp_path, prompt="", environment={"PATH": os.defpath}, timeout=5,
    )
    assert result is not None and result.returncode == 0 and result.raw == RAW
    reopened = Kernel(path, run_id=kernel.storage.run_id)
    status = reopened.status(call.subject)
    assert status["stopped"] is True and status["pending_return"] is True
    assert status["phase"] == "development" and status["candidate"] is None
    assert status["gates"] == []
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert reopened.operation_state().active == ()
    with pytest.raises(KernelError, match="executor_fenced"):
        CoreOperations(reopened, call.execution, lambda binding: ports).once()


@pytest.mark.parametrize("committed", [False, True], ids=["precommit", "committed_reply_loss"])
def test_raw_persistence_failure_still_records_exact_process_exit(tmp_path: Path, git_repo: Path, committed: bool) -> None:
    path, kernel, call, ports = allocated(git_repo)
    starts = tmp_path / "actual-agent-pids"
    persistence_error = OSError("The raw persistence operation did not return successfully")
    tracked, untracked = git_repo / 'README.md', git_repo / 'unfinished.txt'
    original = tracked.read_bytes()
    original_head = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=git_repo,
        check=True, capture_output=True, text=True).stdout.strip()
    tracked_work, untracked_work = b'Tracked unfinished work\n', b'Untracked unfinished work\n'

    def retained_work():
        assert tracked.read_bytes() == tracked_work
        assert untracked.read_bytes() == untracked_work
        assert subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=git_repo,
            check=True, capture_output=True, text=True).stdout.strip() == original_head
        assert subprocess.run(['git', 'show', 'HEAD:README.md'], cwd=git_repo,
            check=True, capture_output=True).stdout == original
        assert subprocess.run(['git', 'diff', '--name-only'], cwd=git_repo,
            check=True, capture_output=True, text=True).stdout.splitlines() == ['README.md']
        assert 'unfinished.txt' in subprocess.run(['git', 'ls-files', '--others', '--exclude-standard'],
            cwd=git_repo, check=True, capture_output=True, text=True).stdout.splitlines()

    class FailedRawPersistence(Kernel):
        def receive(self, exact_call: AgentCall, raw: bytes) -> None:
            if committed:
                super().receive(exact_call, raw)
            raise persistence_error

    arguments = [sys.executable, "-I", "-c", (
        f"from pathlib import Path; import os, sys; "
        f"Path('README.md').write_bytes({tracked_work!r}); "
        f"Path('unfinished.txt').write_bytes({untracked_work!r}); "
        f"Path({str(starts)!r}).open('a').write(str(os.getpid()) + '\\n'); "
        f"sys.stdout.buffer.write({RAW!r})"
    )]
    resume_arguments = [sys.executable, '-I', '-c', (
        'from pathlib import Path; import os, subprocess, sys; '
        f"assert Path('README.md').read_bytes() == {tracked_work!r}; "
        f"assert Path('unfinished.txt').read_bytes() == {untracked_work!r}; "
        f"assert subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], check=True, "
        f"capture_output=True, text=True).stdout.strip() == {original_head!r}; "
        f"Path({str(starts)!r}).open('a').write(str(os.getpid()) + '\\n'); "
        f"sys.stdout.buffer.write({RAW!r})"
    )]
    with pytest.raises(OSError) as caught:
        execute(FailedRawPersistence(path, run_id=call.execution.run_id), call, arguments=arguments, cwd=git_repo, prompt="",
                environment={"PATH": os.defpath}, timeout=5)
    assert caught.value is persistence_error
    retained_work()
    first_start = starts.read_text()
    with pytest.raises(ChildProcessError):
        os.waitpid(int(first_start), os.WNOHANG)
    reopened = Kernel(path, run_id=kernel.storage.run_id)
    assert reopened.operation_state().active == ()
    assert any(event["kind"] == "worker_exited" and event["identity"] == call.invocation_id
               for event in reopened.history(call.subject))
    status = reopened.status(call.subject)
    assert status["pending_attempt"] == call.attempt_id and status["phase"] == "development"
    assert status["pending_return"] is committed
    assert status["gates"] == ([] if committed else ["execution_failed"])
    assert status["budgets"]["requirements"]["developments_used"] == 1
    with reopened.storage.transaction(write=False) as tx:
        output = tx.one('SELECT raw_id FROM invocation_outputs WHERE invocation=? AND ordinal=?',
            (call.invocation_id, call.output_ordinal))
        attempt = tx.one('SELECT status,result_id FROM attempts WHERE id=?', (call.attempt_id,))
        invocation = tx.one('SELECT status FROM invocations WHERE id=?', (call.invocation_id,))
        if committed:
            assert output['raw_id'] == attempt['result_id'] and output['raw_id'] is not None
            assert attempt['status'] == invocation['status'] == 'returned'
            assert tx.get(output['raw_id'], call.subject) == RAW
        else:
            assert output['raw_id'] is attempt['result_id'] is None
            assert attempt['status'] == 'running' and invocation['status'] == 'interrupted'
    assert execute(reopened, call, arguments=arguments, cwd=git_repo, prompt="",
                   environment={"PATH": os.defpath}, timeout=5) is None
    assert starts.read_text() == first_start
    retained_work()
    if committed:
        assert CoreOperations(reopened, call.execution, lambda binding: ports).once(agent_capacity=0).kind == "progress"
    else:
        reopened.control("resume", "resume-after-raw-save-failure", subject=call.subject)
        resumed = CoreOperations(reopened, call.execution, lambda binding: ports).until(lambda step: step.call is not None).call
        assert resumed is not None and resumed.attempt_id == call.attempt_id
        assert resumed.thread_id == call.thread_id and resumed.invocation_id != call.invocation_id
        retained_work()
        result = execute(reopened, resumed, arguments=resume_arguments, cwd=git_repo, prompt="",
                         environment={"PATH": os.defpath}, timeout=5)
        assert result is not None and result.returncode == 0 and result.raw == RAW
        assert len(starts.read_text().splitlines()) == 2
    retained_work()
    assert reopened.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1


def test_selected_credentials_pause_without_charging_or_reallocating_attempt(tmp_path: Path) -> None:
    path, kernel, call, ports = allocated(tmp_path)
    credential = tmp_path / "selected-worker-credential"
    canary = tmp_path / "agent-executed"
    arguments = [sys.executable, "-I", "-c", (
        f"from pathlib import Path; import sys; Path({str(canary)!r}).write_text('ran'); "
        f"sys.stdout.buffer.write({RAW!r})"
    )]
    assert execute(
        kernel, call, arguments=arguments, cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5, credential_file=credential,
    ) is None
    reopened = Kernel(path, run_id=kernel.storage.run_id)
    paused = reopened.status(call.subject)
    assert paused["pending_attempt"] == call.attempt_id and paused["gates"] == ["worker_unavailable"]
    assert paused["budgets"]["requirements"]["developments_used"] == 0
    assert not canary.exists() and reopened.operation_state().active == ()
    credential.write_text("Controlled reference availability; no online secret.")
    reopened.control("resume", "resume-selected-identity", subject=call.subject)
    resumed = CoreOperations(reopened, call.execution, lambda binding: ports).until(lambda step: step.call is not None)
    assert resumed.call is not None
    assert resumed.call.attempt_id == call.attempt_id and resumed.call.invocation_id != call.invocation_id
    assert resumed.call.thread_id == call.thread_id
    result = execute(
        reopened, resumed.call, arguments=arguments, cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5, credential_file=credential,
    )
    assert result is not None and result.returncode == 0 and result.raw == RAW
    assert canary.read_text() == "ran"
    assert reopened.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1


def test_committed_binding_response_loss_settles_actual_exit_and_resumes_same_attempt(tmp_path: Path) -> None:
    path, kernel, call, ports = allocated(tmp_path)
    canary = tmp_path / "agent-starts"

    class LostBindingResponse(Kernel):
        def agent_started(self, exact_call: AgentCall, identity: dict[str, Any]) -> None:
            super().agent_started(exact_call, identity)
            raise OSError("The durable binding response was lost")

    arguments = [sys.executable, "-I", "-c", (
        f"from pathlib import Path; import sys; Path({str(canary)!r}).open('a').write('ran\\n'); "
        f"sys.stdout.buffer.write({RAW!r})"
    )]
    with pytest.raises(OSError, match="binding response"):
        execute(LostBindingResponse(path, run_id=call.execution.run_id), call, arguments=arguments, cwd=tmp_path, prompt="",
                environment={"PATH": os.defpath}, timeout=5)
    reopened = Kernel(path, run_id=kernel.storage.run_id)
    assert not canary.exists()
    assert any(event["kind"] == "worker_started" and event["identity"] == call.invocation_id
               for event in reopened.history(call.subject))
    assert reopened.operation_state().active == ()
    assert reopened.status(call.subject)["gates"] == ["execution_failed"]
    reopened.control("resume", "resume-after-exact-exit", subject=call.subject)
    resumed = CoreOperations(reopened, call.execution, lambda binding: ports).until(lambda step: step.call is not None).call
    assert resumed is not None and resumed.attempt_id == call.attempt_id
    assert resumed.invocation_id != call.invocation_id
    result = execute(reopened, resumed, arguments=arguments, cwd=tmp_path, prompt="",
                     environment={"PATH": os.defpath}, timeout=5)
    assert result is not None and result.returncode == 0
    assert canary.read_text() == "ran\n"
    assert reopened.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="requires Linux pidfd")
def test_executor_loss_before_durable_binding_never_releases_or_restarts_worker(tmp_path: Path) -> None:
    path, kernel, call, _ = allocated(tmp_path)
    canary = tmp_path / "forbidden-agent-work"
    arguments = [sys.executable, "-I", "-c", f"from pathlib import Path; Path({str(canary)!r}).write_text('ran')"]
    request = tmp_path / "executor-request.json"
    request.write_text(json.dumps({"database": str(path), "call": asdict(call),
                                   "arguments": arguments, "cwd": str(tmp_path)}))
    read_fd, write_fd = os.pipe()
    with child_subreaper():
        try:
            executor = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).parent / "support" / "held_binding_executor.py"),
                 str(request), str(write_fd)], pass_fds=(write_fd,),
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        unrelated = None
        worker, pidfd = None, None
        try:
            unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import signal; signal.pause()"], start_new_session=True)
            assert select.select([read_fd], [], [], 5)[0] == [read_fd]
            worker = int(os.read(read_fd, 32))
            pidfd = os.pidfd_open(worker)
            assert execute(kernel, call, arguments=arguments, cwd=tmp_path, prompt="",
                           environment={"PATH": os.defpath}, timeout=5) is None
            assert len(kernel.operation_state().active) == 1
            executor.kill()
            assert executor.wait(timeout=5) == -signal.SIGKILL
            assert select.select([pidfd], [], [], 5)[0] == [pidfd]
            assert not canary.exists() and unrelated.poll() is None
            os.waitpid(worker, 0)
            worker = None
            # Actual exit is proved to this observer, but no forged Kernel
            # confirmation or second dispatch may clear the original lease.
            assert len(Kernel(path, run_id=kernel.storage.run_id).operation_state().active) == 1
            assert Kernel(path, run_id=kernel.storage.run_id).status(call.subject)["budgets"]["requirements"]["developments_used"] == 0
        finally:
            os.close(read_fd)
            if executor.poll() is None:
                executor.kill()
                executor.wait(timeout=5)
            if pidfd is not None:
                if not select.select([pidfd], [], [], 0)[0]:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                os.close(pidfd)
            if worker is not None:
                os.waitpid(worker, 0)
            if unrelated is not None:
                unrelated.kill()
                unrelated.wait(timeout=3)


def test_real_source_refresh_revokes_blocked_worker_before_argv(tmp_path: Path) -> None:
    source_path = tmp_path / "requirements.json"
    source = {"parent": {"number": 1, "title": "Parent", "body": "Original scope", "sub_issues": [],
                         "sub_issue_order_reliable": True}, "issues": []}
    source_path.write_text(json.dumps(source))
    requirements = Requirements.from_wire(json.loads(source_path.read_text()))
    base = Commit("base", "base-tree")
    spec = RunSpec("source-process", "example/repository", 1, "read-source", "read-graph", (
        Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "read-source", base,
                 "refs/heads/task", "refs/heads/main", checkout=str(tmp_path)),
    ))
    spec = replace(spec, repository_root=str(tmp_path.resolve()), language="zh", inputs=fixture_inputs())
    kernel = create_kernel(tmp_path / "kernel.sqlite3", spec, requirements=requirements)
    execution = kernel.claim_executor("source-executor")
    ports = ReadOnlyPorts(requirements, "refs/heads/task", "refs/heads/main", DeliveryFacts(base, None))
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    call = operations.until(lambda step: step.call is not None).call
    assert call is not None
    canary = tmp_path / "stale-agent-ran"
    reads = 0

    def refresh_source() -> None:
        nonlocal reads
        ports.requirements = Requirements.from_wire(json.loads(source_path.read_text()))
        reads += 1
        operations.runner.authorize_agent(call)
        if reads == 1:
            source["parent"]["body"] = "Requirement changed while the bootstrap was blocked"
            source_path.write_text(json.dumps(source))

    with pytest.raises(KernelError):
        execute(
            kernel, call, arguments=[sys.executable, "-I", "-c", f"from pathlib import Path; Path({str(canary)!r}).write_text('ran')"],
            cwd=tmp_path, prompt="", environment={"PATH": os.defpath}, timeout=5,
            authorize_source=refresh_source,
        )
    assert reads == 2 and not canary.exists()
    paused = Kernel(tmp_path / 'kernel.sqlite3', run_id=spec.run_id).status(call.subject)
    assert paused["gates"] == ["requeue_required"]
    assert paused["phase"] == "development" and paused["pending_return"] is False
    assert paused["budgets"]["requirements"]["developments_used"] == 0
    assert kernel.operation_state().active == ()
