"""A finished published Task reports completion without creating another Action."""
from dataclasses import replace
import json

import pytest

from agent_run.cli.main import main
from agent_run.config.defaults import UserDefaultsStore
from support.control_scenarios import managed_approval
from support.environment import durable_snapshot as _snapshot


def test_completed_stop_public_plain_and_json_preserve_terminal_state(tmp_path, monkeypatch, capsys):
    language = "zh"
    kernel, runner, physical, _state, _calls, request, subject, _source = managed_approval(
        tmp_path, monkeypatch, tickets=(), language=language)
    # This helper originally stops at approval; its old Issue route hardcodes
    # Ticket 2. Here it must expose the actual Parent selected by the API path.
    fake = tmp_path / "transport" / "bin" / "gh"
    fake.write_text(fake.read_text().replace("'number':2,'node_id':'issue-node-2'",
        "'number':int(endpoint.split('/')[4]),'node_id':'issue-node-'+endpoint.split('/')[4]"))
    arguments = [request.run_id, "--repo", physical.repository, "--state-dir", str(kernel.storage.path)]
    assert main(["approve", *arguments, "--json"]) == 0
    capsys.readouterr()
    for _ in range(80):
        step = runner.run_once()
        assert step.call is None
        status = kernel.status(subject)
        if status["phase"] == "complete" and status["resource_cleanup"]["state"] == "complete":
            break
    else:
        raise AssertionError("The normal published Task did not complete and retire its resources")
    observed = kernel.host_usage("original-host")
    assert observed is not None
    assert kernel.observe_host(request, replace(observed, disposition="exited", exit_status=0,
        ended_at=111.0, usage_ready=False, usage_released=True)) == "exited"
    assert kernel.active_host_action() is None and kernel.operation_state().active == ()
    UserDefaultsStore().configure(language="en" if language == "zh" else "zh")
    before = _snapshot(tmp_path)
    assert main(["stop", *arguments]) == 0
    human = capsys.readouterr()
    assert human.err == ""
    assert ("交付已完成；本次未执行新动作" if language == "zh" else
            "Delivery already completed; no new action was performed") in human.out
    assert not any(word in human.out for word in ("请求已受理", "Request accepted", "未确认", "Unconfirmed",
                                                  "不表示 Agent", "does not prove that an Agent"))
    assert request.run_id not in human.out
    assert _snapshot(tmp_path) == before
    assert main(["stop", *arguments, "--json"]) == 0
    machine = capsys.readouterr()
    assert machine.err == ""
    assert json.loads(machine.out) == {"result": "completed", "status": "completed", "run_id": request.run_id,
                                      "action_id": None, "action": "stop", "host": "exited"}
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("result,status,action_id,host,expected", [
    ("accepted", "action_complete", "original-action", "exited", ("请求处理已完成", "Request processing complete")),
    ("accepted", "action_failed", "original-action", "exited", ("请求处理失败", "Request processing failed")),
    ("attached", "action_pending", None, "running", ("本次新操作未应用", "new action not applied")),
    ("accepted", "unapplied", "original-action", "unknown", ("未确认", "Unconfirmed")),
    ("completed", "completed", "unexpected-action", "exited", ("未确认", "Unconfirmed")),
    ("completed", "completed", None, "unknown", ("未确认", "Unconfirmed")),
])
def test_nonterminal_receipts_never_claim_delivery_completion(capsys, language, result, status, action_id, host, expected):
    from argparse import Namespace
    from agent_run.cli.main import _print_receipt

    _print_receipt(Namespace(command="stop", repo="example/project", run_id="1", ticket=None, state_dir=None),
                   {"result": result, "status": status, "action_id": action_id, "action": "stop", "host": host}, language)
    output = capsys.readouterr().out
    assert expected[0 if language == "zh" else 1] in output
    assert ("不表示 Agent 已启动或交付已完成" if language == "zh" else
            "does not prove that an Agent has started or delivery has completed") in output
    assert "交付已完成；本次未执行新动作" not in output
    assert "Delivery already completed; no new action was performed" not in output
    assert "original-action" not in output and "unexpected-action" not in output


@pytest.mark.parametrize("language,expected", [
    ("zh", "交付已完成；本次未执行新动作"),
    ("en", "Delivery already completed; no new action was performed"),
])
def test_completed_receipt_uses_requested_language_without_claiming_new_work(capsys, language, expected):
    from argparse import Namespace
    from agent_run.cli.main import _print_receipt

    _print_receipt(Namespace(command="stop", repo="example/project", run_id="1", ticket=None, state_dir=None),
        {"result": "completed", "status": "completed", "action_id": None, "action": "stop", "host": "exited"}, language)
    output = capsys.readouterr().out
    assert expected in output
    assert not any(word in output for word in ("请求已受理", "Request accepted", "未确认", "Unconfirmed"))
