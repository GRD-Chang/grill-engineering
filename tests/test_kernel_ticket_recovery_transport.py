"""Recovery reads the original live close proof before reopening its Ticket."""
from dataclasses import asdict, replace
import json

import pytest

from agent_run_kernel import KernelError
from test_kernel_remote_effects import _fixture


@pytest.mark.parametrize("mode", ["closed", "already_open", "foreign_close", "missing_event",
                                  "missing_comment", "changed", "stopped"])
def test_ticket_recovery_preserves_original_close_ownership_and_fresh_authority(tmp_path, monkeypatch, mode):
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    comment = replace(push, effect_id="comment-original", operation="completion_comment", after_sha=None, pr_number=17,
                      payload={"issue": 2, "merge_sha": push.after_sha, "kind": "ticket"})
    comment_receipt = port.execute(comment, lambda: None)
    assert comment_receipt.disposition == "target"
    close = replace(comment, effect_id="close-original", operation="close",
                    payload={**comment.payload, "close_intent": comment_receipt.raw["close_intent"]})
    close_receipt = port.execute(close, lambda: None)
    assert close_receipt.disposition == "target"
    original_close = close_receipt.raw["event"]
    reopen = replace(close, effect_id="recover-original", operation="reopen_ticket", before_sha=push.after_sha,
                     payload={"issue": 2, "control_action": "abandon-original",
                              "completion": {"subject": push.subject, "generation": 1, "merge_sha": push.after_sha,
                                             "close_effect": close.effect_id},
                              "close_intent": close.payload["close_intent"], "close_receipt": asdict(close_receipt)})
    document = json.loads(state.read_text())
    external_reopen = {"id": 2, "event": "reopened", "actor": {"login": "maintainer"},
                       "created_at": "2026-10-21T10:02:00Z"}
    foreign_close = {"id": 3, "event": "closed", "actor": {"login": "external"},
                     "created_at": "2026-10-21T10:03:00Z"}
    if mode in {"already_open", "changed", "stopped", "foreign_close"}:
        document["events"].append(external_reopen)
        document["issue_state"] = "open"
    if mode == "foreign_close":
        document["events"].append(foreign_close)
        document["issue_state"] = "closed"
    if mode == "missing_event":
        document["events"] = []
    if mode == "missing_comment":
        document["comments"] = []
    if mode == "closed":
        document["lose_operation"] = "close"
    state.write_text(json.dumps(document))
    authorizations = []

    def authorize():
        authorizations.append(reopen.effect_id)
        if mode == "stopped":
            raise KernelError("executor_fenced")
        if mode == "changed":
            changed = json.loads(state.read_text())
            changed["events"].append(foreign_close)
            changed["issue_state"] = "closed"
            state.write_text(json.dumps(changed))

    if mode in {"changed", "stopped"}:
        with pytest.raises(KernelError, match="ticket_recovery_boundary_changed" if mode == "changed" else "executor_fenced"):
            port.execute(reopen, authorize)
        assert authorizations == [reopen.effect_id]
    else:
        result = port.execute(reopen, authorize)
        if mode in {"closed", "already_open"}:
            assert result.disposition == "target"
            assert result.raw["issue"] == 2 and result.raw["state"] == "OPEN"
            assert result.raw["prior_close"] == original_close
            assert authorizations == [reopen.effect_id]
            if mode == "closed":
                assert result.raw["write_returncode"] == 1
                assert result.raw.get("already_open") is not True
                assert result.raw["reopened_event"]["id"] > original_close["id"]
                assert result.raw["reopened_event"]["actor"] == {"login": "publisher"}
            else:
                assert result.raw["already_open"] is True
            assert port.reconcile(reopen).disposition == "target"
        else:
            assert result.disposition == ("third" if mode == "foreign_close" else "unknown")
            assert authorizations == []
            if mode == "foreign_close":
                assert result.raw["issue"] == 2 and result.raw["state"] == "CLOSED"
                assert result.raw["latest_close"] == foreign_close
    final = json.loads(state.read_text())
    assert len(final["comments"]) == (0 if mode == "missing_comment" else 1)
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("PATCH" in call for call in actual) == (2 if mode == "closed" else 1)
