"""Public control receipts describe this request, including failed attachment."""
import json
from argparse import Namespace
from dataclasses import replace
import time

from agent_run.cli.main import main
from support.control_scenarios import managed_approval


def test_public_resume_attached_to_unknown_host_is_unsuccessful(tmp_path, monkeypatch, capsys):
    kernel, _, physical, _, _, request, _, _ = managed_approval(tmp_path, monkeypatch, tickets=())
    # Remove the manager's current identity, without altering the stored Host.
    response = tmp_path / "manager" / "manager-response.json"
    document = json.loads(response.read_text())
    document["fields"]["InvocationID"] = "0" * 32
    response.write_text(json.dumps(document))
    for as_json in (False, True):
        arguments = ["resume", request.run_id, "--repo", physical.repository]
        if as_json:
            arguments.append("--json")
        assert main(arguments) != 0
        output = capsys.readouterr().out
        if as_json:
            receipt = json.loads(output)
            assert receipt["result"] == "attached"
            assert receipt["requested_action"] == "resume"
            assert receipt["requested_action_applied"] is False
            assert receipt["requested_action_succeeded"] is False
            assert receipt["action"] == "run"
        else:
            assert "new action not applied" in output
    assert kernel.active_host_action() == "original-host"
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE kind='resume' AND id!='prepare-managed'")


def test_public_approval_waits_for_delivery_and_cleanup_then_reads_repeated_request(
    tmp_path, monkeypatch, capsys,
):
    import time
    from types import SimpleNamespace
    from agent_run.runtime import action_observation
    kernel, runner, physical, _, calls, request, _, _ = managed_approval(tmp_path, monkeypatch, tickets=())
    # This existing transport fixture defaults to leaf Issue #2. This test
    # finishes its Parent-only Issue #1 through the same real close adapter.
    gh = tmp_path / "transport" / "bin" / "gh"
    from support.fixture_text import replace_once
    gh.write_text(replace_once(gh.read_text(), "'number':2,'node_id':'issue-node-2'",
                              "'number':1,'node_id':'issue-node-1'"))
    samples = []

    def advance(_seconds):
        samples.append(runner.run_once().kind)

    monkeypatch.setattr(action_observation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=advance))
    arguments = ["approve", request.run_id, "--repo", physical.repository, "--json"]
    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert samples, "The CLI must observe actual delivery, not only an applied grant"
    receipt = json.loads(output)
    assert receipt["requested_action_applied"] is True
    assert receipt["requested_action_succeeded"] is True
    assert receipt["delivery_completed"] is True
    assert receipt["request_outcome"] == "delivered"
    assert kernel.completed_final_action("approve") is not None
    writes = [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)]
    for action in ("approve", "resume"):
        assert main([action, request.run_id, "--repo", physical.repository, "--json"]) == 0
        repeated = json.loads(capsys.readouterr().out)
        assert repeated["request_outcome"] == "already_completed"
        assert repeated["requested_action_applied"] is False
        assert repeated["requested_action_succeeded"] is True
    assert [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)] == writes
    assert main(["approve", request.run_id, "--repo", physical.repository]) == 0
    plain = capsys.readouterr().out
    assert "Final delivery and required cleanup completed" in plain
    assert "does not prove" not in plain


def test_resume_without_post_request_execution_cannot_use_old_host_exit_as_success(tmp_path):
    from agent_run.model import HostObservation
    from agent_run.runtime.action_observation import _wait
    from support.core_operations import host_kernel
    kernel = host_kernel(tmp_path)
    host = kernel.admit_host("original-launch", kind="run", command=("controlled-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(host)
    running = HostObservation("running", host.unit_name, "original", "/host", "boot", 42, "start", usage_ready=True)
    assert kernel.observe_host(host, running) == "running"
    assert kernel.control_host(host, "stop", "stop-subject", subject="parent") == "applied"
    assert kernel.control_host(host, "resume", "quick-resume", subject="parent") == "applied"
    # Another observer has already settled the original execution. Its launch
    # Action stays original-launch, while the applied request is quick-resume.
    assert kernel.observe_host(host, replace(running, disposition="exited", exit_status=0,
        ended_at=time.time(), usage_released=True)) == "exited"
    assert kernel.active_host_action() is None
    receipt = {"result": "accepted", "status": "action_applied", "action": "resume",
               "action_id": "quick-resume", "host_action_id": "original-launch", "host": "exited"}
    observed = _wait(kernel, receipt, Namespace(command="resume"))
    assert observed["requested_action_applied"] is True
    assert observed.get("requested_action_succeeded", False) is False
    assert observed["request_outcome"] == "executor_exited"
