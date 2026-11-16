"""A stop receipt distinguishes completed delivery from accepted work."""
import pytest


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
