"""Real local processes cross the allocated-call seam, never live Agent services."""

from pathlib import Path
import os
import sys

from agent_run_kernel import AgentCall, Commit, Delivery, DeliveryKind, Kernel, Policy, RunSpec, World
from agent_run_kernel.execution import execute


RAW = b'{"result_kind":"development","summary":"saved local work","human_blockers":null}'


def allocated(tmp_path: Path) -> tuple[Path, Kernel, AgentCall, World]:
    path = tmp_path / "kernel.sqlite3"
    base = Commit("base", "base-tree")
    spec = RunSpec(
        "process-run", "example/repository", 1, "parent-v1", "graph-v1",
        (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                  "refs/heads/ticket-2", "refs/heads/run", checkout=str(tmp_path)),),
        Policy(ticket_rounds=1),
    )
    kernel = Kernel.create(path, spec)
    execution = kernel.claim_executor("local-executor")
    world = World("parent-v1", "graph-v1", "ticket-v1", base)
    step = kernel.advance("ticket:2", execution, world)
    assert step.call is not None
    return path, kernel, step.call, world


def test_process_waits_for_final_guard_and_saved_return_survives_reopening(tmp_path: Path) -> None:
    path, kernel, call, world = allocated(tmp_path)
    canary = tmp_path / "actual-agent-starts"
    checks: list[bool] = []

    def authorize_source() -> None:
        # Even the second, post-binding source check runs before Agent work.
        assert not canary.exists()
        checks.append(True)
        if len(checks) == 2:
            assert any(event["kind"] == "worker_started" and event["identity"] == call.invocation_id
                       for event in Kernel(path).history(call.subject))

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
    assert len(checks) == 2
    assert canary.read_text() == "ran\n"

    reopened = Kernel(path)
    status = reopened.status(call.subject)
    assert status["pending_return"] is True
    assert status["phase"] == "development"
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert execute(
        reopened, call, arguments=arguments, cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5,
    ) is None
    assert canary.read_text() == "ran\n"
    assert reopened.advance(call.subject, call.execution, world, allow_agent=False).kind == "progress"
    assert reopened.status(call.subject)["budgets"]["requirements"]["developments_used"] == 1
