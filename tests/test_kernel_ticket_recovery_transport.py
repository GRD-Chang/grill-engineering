"""Recovery uses committed Issue currentness and independent exact write receipts."""

from agent_run.contracts.write_authority import WriteAuthority
from dataclasses import asdict, replace
import json

import pytest

from agent_run.errors import KernelError
from agent_run.adapters.git_objects import LocalGit
from agent_run.model import Effect, Execution
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.adapters.remote import RemoteEffects
from support.managed_abandon_transport import install_currentness
from test_kernel_managed_entry_end_to_end import _CODEX, _control_environment


def _closed_ticket(git_repo, tmp_path, monkeypatch):
    _environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    install_currentness(tmp_path, state)
    port = RemoteEffects(git_repo, "example/project", runner_identity={"source": "host"})
    base = LocalGit(git_repo).commit("HEAD")
    comment = Effect("comment-original", "ticket:2", "completion_comment", "example/project",
                     "refs/heads/ticket-2", "refs/heads/run", base.sha, None, None, base, 17, "publisher",
                     {"issue": 2, "merge_sha": base.sha, "kind": "ticket"}, Execution("run-1", "worker", 1))
    comment_receipt = port.execute(comment, WriteAuthority(lambda: lambda: None))
    assert comment_receipt.disposition == "target"
    close = replace(comment, effect_id="close-original", operation="close",
                    payload={**comment.payload, "close_intent": comment_receipt.raw["close_intent"]})
    close_receipt = port.execute(close, WriteAuthority(lambda: lambda: None))
    assert close_receipt.disposition == "target"
    reopen = replace(close, effect_id="recover-original", operation="reopen_ticket",
                     payload={"issue": 2, "control_action": "abandon-original",
                              "completion": {"subject": close.subject, "generation": 1, "merge_sha": base.sha,
                                             "close_effect": close.effect_id},
                              "close_intent": close.payload["close_intent"], "close_receipt": asdict(close_receipt)})
    return port, reopen, state, tmp_path / "gh-calls.jsonl"


def _foreign_transition(document, event, actor):
    observed = {"id": len(document["events"]) + 1, "event": event, "actor": {"login": actor},
                "created_at": "2026-10-21T11:00:00Z"}
    document["events"].append(observed)
    document["events_by_issue"]["2"].append(observed)
    issue = document["source_graph"]["issues"]["2"]
    issue.update(state="OPEN" if event == "reopened" else "CLOSED", updated_at=observed["created_at"])
    return observed


@pytest.mark.parametrize("mode", ["closed", "already_open", "foreign_close", "missing_event",
                                  "missing_comment", "metadata", "missing_timestamp", "equivalent_timestamp", "changed", "stopped"])
def test_ticket_recovery_preserves_original_close_ownership_and_fresh_authority(git_repo, tmp_path, monkeypatch, mode):
    port, reopen, state, calls = _closed_ticket(git_repo, tmp_path, monkeypatch)
    original_close = reopen.payload["close_receipt"]["raw"]["event"]
    document = json.loads(state.read_text())
    if mode in {"already_open", "foreign_close"}:
        _foreign_transition(document, "reopened", "maintainer")
    if mode == "foreign_close":
        _foreign_transition(document, "closed", "external")
    if mode == "missing_event":
        document["events"] = document["events_by_issue"]["2"] = []
    if mode == "missing_comment":
        document["comments"] = document["comments_by_issue"]["2"] = []
    if mode == "metadata":
        document["source_graph"]["issues"]["2"]["updated_at"] = "2026-10-21T12:00:00Z"
    if mode == "missing_timestamp":
        document["source_graph"]["issues"]["2"]["updated_at"] = None
    if mode == "closed":
        document["lose_operation"] = "close"
    state.write_text(json.dumps(document))
    preparations, authorizations = [], []
    original_intent = asdict(reopen)

    def authorize():
        authorizations.append(reopen.effect_id)
        if mode == "stopped":
            raise KernelError("executor_fenced")

    def prepare():
        preparations.append(reopen.effect_id)
        if mode == "equivalent_timestamp":
            changed = json.loads(state.read_text())
            issue = changed["source_graph"]["issues"]["2"]
            issue["updated_at"] = issue["updated_at"].replace("Z", "+00:00")
            state.write_text(json.dumps(changed))
        if mode == "changed":
            changed = json.loads(state.read_text())
            _foreign_transition(changed, "reopened", "maintainer")
            _foreign_transition(changed, "closed", "external")
            state.write_text(json.dumps(changed))
        return authorize

    if mode in {"changed", "stopped"}:
        with pytest.raises(KernelError, match="ticket_recovery_boundary_changed" if mode == "changed" else "executor_fenced"):
            port.execute(reopen, WriteAuthority(prepare))
        assert preparations == [reopen.effect_id]
        assert authorizations == ([] if mode == "changed" else [reopen.effect_id])
    elif mode in {"missing_event", "missing_comment", "metadata", "missing_timestamp"}:
        with pytest.raises(ReadUnavailable) as pending:
            port.execute(reopen, WriteAuthority(prepare))
        assert pending.value.code == "github_recovery_observation_pending"
        assert pending.value.message == {
            "missing_event": "Original close event is unproven",
            "missing_comment": "Original close comment is unproven",
            "metadata": "Issue updated_at does not prove the original recovery transition",
            "missing_timestamp": "Issue updated_at does not prove the original recovery transition",
        }[mode]
        assert pending.value.write_returncode is None and pending.value.write_diagnostic == ""
        assert asdict(reopen) == original_intent
        assert json.loads(state.read_text()) == document
        assert authorizations == []
        assert preparations == []
    else:
        result = port.execute(reopen, WriteAuthority(prepare))
        if mode in {"closed", "equivalent_timestamp"}:
            assert result.disposition == "target" and result.raw["state"] == "OPEN"
            assert result.raw["prior_close"] == original_close
            assert result.raw["write_returncode"] == (1 if mode == "closed" else 0)
            assert result.raw["reopened_event"]["id"] > original_close["id"]
            assert result.raw["reopened_event"]["actor"] == "publisher"
            assert authorizations == [reopen.effect_id]
            assert preparations == [reopen.effect_id]
            assert port.reconcile(reopen).disposition == "target"
        else:
            assert result.disposition == "third" and result.raw["reason"] == "not_owned"
            assert result.raw["issue"] == 2 and result.raw["prior_close"] == original_close
            assert authorizations == []
            assert preparations == []
    final = json.loads(state.read_text())
    assert len(final["comments"]) == (0 if mode == "missing_comment" else 1)
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("PATCH" in call for call in actual) == (2 if mode in {"closed", "equivalent_timestamp"} else 1)


@pytest.mark.parametrize("mode", ["lost_ack", "equivalent_timestamp", "metadata", "foreign_reopen", "foreign_close", "changed", "stopped"])
def test_recovery_comment_has_one_exact_marker_and_independent_fresh_authority(git_repo, tmp_path, monkeypatch, mode):
    port, reopen, state, calls = _closed_ticket(git_repo, tmp_path, monkeypatch)
    reopened = port.execute(reopen, WriteAuthority(lambda: lambda: None))
    assert reopened.disposition == "target"
    effect = replace(reopen, effect_id="recovery-comment-original", operation="recovery_comment",
                     payload={**reopen.payload, "reopen_effect": reopen.effect_id, "reopen_receipt": asdict(reopened),
                              "comment_body": "Run run-1 abandoned before default delivery; Ticket #2, PR #17, commit " + reopen.before_sha})
    document = json.loads(state.read_text())
    if mode == "lost_ack":
        document["lose_operation"] = "comment"
    if mode == "equivalent_timestamp":
        event = document["events_by_issue"]["2"][-1]
        event["created_at"] = event["created_at"].replace("Z", "+00:00")
        document["source_graph"]["issues"]["2"]["updated_at"] = event["created_at"]
    if mode == "metadata":
        document["source_graph"]["issues"]["2"]["updated_at"] = "2026-10-21T12:00:00Z"
    if mode in {"foreign_reopen", "foreign_close"}:
        if mode == "foreign_reopen":
            _foreign_transition(document, "closed", "external")
            _foreign_transition(document, "reopened", "maintainer")
        else:
            _foreign_transition(document, "closed", "external")
    state.write_text(json.dumps(document))
    preparations, authorizations = [], []
    original_intent = asdict(effect)

    def authorize():
        authorizations.append(effect.effect_id)
        if mode == "stopped":
            raise KernelError("executor_fenced")

    def prepare():
        preparations.append(effect.effect_id)
        if mode == "changed":
            changed = json.loads(state.read_text())
            changed["source_graph"]["issues"]["2"]["updated_at"] = "2026-10-21T12:00:00Z"
            state.write_text(json.dumps(changed))
        return authorize

    if mode in {"metadata", "changed"}:
        with pytest.raises(ReadUnavailable) as pending:
            port.execute(effect, WriteAuthority(prepare))
        assert pending.value.code == "github_recovery_observation_pending"
        assert pending.value.message == "Issue updated_at does not prove the original recovery transition"
        assert pending.value.write_returncode is None and pending.value.write_diagnostic == ""
        assert asdict(effect) == original_intent
        final_issue = json.loads(state.read_text())["source_graph"]["issues"]["2"]
        assert final_issue["state"] == "OPEN" and final_issue["updated_at"] == "2026-10-21T12:00:00Z"
        assert preparations == ([effect.effect_id] if mode == "changed" else [])
        assert authorizations == []
    elif mode == "stopped":
        with pytest.raises(KernelError, match="executor_fenced"):
            port.execute(effect, WriteAuthority(prepare))
        assert authorizations == [effect.effect_id]
        assert preparations == [effect.effect_id]
    elif mode in {"foreign_reopen", "foreign_close"}:
        result = port.execute(effect, WriteAuthority(prepare))
        assert result.disposition == "third" and result.raw["reason"] == "not_owned"
        assert authorizations == []
        assert preparations == []
    else:
        result = port.execute(effect, WriteAuthority(prepare))
        assert result.disposition == "target" and result.raw["write_returncode"] == (1 if mode == "lost_ack" else 0)
        assert result.raw["reopened_event"] == reopened.raw["reopened_event"]
        assert result.raw["comment"]["actor"] == "publisher"
        assert result.raw["comment"]["body"] == port._marker(effect) + "\n" + effect.payload["comment_body"]
        assert authorizations == [effect.effect_id]
        assert preparations == [effect.effect_id]
        changed = json.loads(state.read_text())
        changed["source_graph"]["issues"]["2"]["updated_at"] = "2026-10-21T12:00:00Z"
        state.write_text(json.dumps(changed))
        assert port.reconcile(effect).raw == {key: value for key, value in result.raw.items() if not key.startswith("write_")}
        assert port.execute(effect, WriteAuthority(lambda: lambda: pytest.fail("exact target cannot write again"))).disposition == "target"
    final = json.loads(state.read_text())
    assert len(final["comments"]) == (2 if mode in {"lost_ack", "equivalent_timestamp"} else 1)
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("POST" in call for call in actual) == (2 if mode in {"lost_ack", "equivalent_timestamp"} else 1)
    assert sum("PATCH" in call for call in actual) == 2
