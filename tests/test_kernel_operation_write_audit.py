"""A nonzero physical write remains audited when exact readback succeeds."""
from agent_run_kernel.operation_runner import OperationRunner
import json
from pathlib import Path
import shutil
import sys

from agent_run_kernel import Delivery, DeliveryKind, Kernel, RunSpec
from agent_run_kernel.model import ParentIssue, Requirements
from support.offline import Scenario, fixture_inputs
from test_kernel_remote_effects import _fixture


def test_actual_nonzero_push_is_audited_once_despite_target_readback(tmp_path, monkeypatch):
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    real_git = shutil.which("git")
    wrapper = tmp_path / "bin" / "git"
    wrapper.write_text("#!" + sys.executable + "\n" +
        "import subprocess,sys\n" +
        "result=subprocess.run([" + repr(real_git) + ",*sys.argv[1:]])\n" +
        "if sys.argv[1]=='push' and result.returncode==0:\n" +
        " print('Original successful push lost its acknowledgement',file=sys.stderr)\n" +
        " raise SystemExit(7)\n" +
        "raise SystemExit(result.returncode)\n")
    wrapper.chmod(0o755)
    owned = tmp_path / "work"
    job = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "untrusted", push.base,
        "refs/heads/native-task", "refs/heads/main", checkout=str(owned / "parent"),
        owned_root=str(owned), common_git_dir=str(git.root / ".git"))
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement"), ())
    spec = RunSpec("physical-audit", port.repository, 1, "untrusted", "untrusted", (job,),
        language="en", repository_root=str(git.root), inputs=fixture_inputs())
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", spec, requirements=source)
    execution = kernel.claim_executor("physical-owner")
    runner = OperationRunner(kernel, execution, lambda binding: port)
    agents = Scenario(tmp_path / "agents", DeliveryKind.PARENT_ONLY)
    agents.kernel, agents.port.git = kernel, git
    for _ in range(60):
        step = runner.run_once()
        if step.call is not None:
            runner.authorize_agent(step.call)
            kernel.receive(step.call, agents.agent(step.call))
        if kernel.status(job.key)["phase"] == "pr":
            break
    else:
        raise AssertionError("The original physical push never settled")
    with kernel.storage.transaction(write=False) as tx:
        effects = tx.rows("SELECT id,status,attempts FROM effects WHERE operation='push'")
        effect, = effects
        assert (effect["status"], effect["attempts"]) == ("confirmed", 1)
        failures = tx.rows("SELECT evidence,dispatch_serial FROM effect_failures WHERE effect=?", (effect["id"],))
        failure, = failures
        diagnostic = json.loads(tx.get(failure["evidence"], job.key))
        assert failure["dispatch_serial"] == 1 and diagnostic["returncode"] == 7
        assert "Original successful push lost its acknowledgement" in diagnostic["message"]
        assert len(tx.rows("SELECT position FROM history WHERE kind='effect_failed' AND identity=?", (effect["id"],))) == 1
    # Another committed transition may not repeat or erase the audit.
    assert runner.run_once().kind == "progress"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM effect_failures")[0] == 1


def test_publication_unknown_response_diagnostic_remains_public_after_successful_exact_readback(tmp_path):
    from agent_run_kernel.query import read

    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("pr_write")
    assert scenario.run().kind == "waiting"
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == "pr_write" and pending["status"] == "sent"
    # Repeated waits preserve one diagnostic for this intent and business scope.
    runner = scenario.runner()
    assert runner.run_once().kind == "waiting"
    now[0] = 105.25
    assert scenario.run().kind == "approval"
    public = read(scenario.kernel.storage.path, "history").data
    events = [event for event in public["timeline"] if event.get("evidence") == {
        "code": "write_response_unavailable", "message": "pr_write took effect; response lost"}]
    assert len(events) == 1
    assert pending["id"] in events[0]["identity"]
    assert public["publication_operation_retries"] == []
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT subject FROM waits")
