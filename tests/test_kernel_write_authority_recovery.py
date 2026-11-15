"""Recovery facts are checked after slow preparation and before live Core admission."""
from support.core_operations import prepare_scripted_agent
from dataclasses import asdict, replace
import json

import pytest

from agent_run.errors import KernelError
from support.offline_api import SyntheticGitHub


def test_write_authority_does_not_cache_permission_before_its_boundary():
    from agent_run.contracts.write_authority import WriteAuthority, admit_write
    order = []
    stopped = False

    def prepare():
        order.append("prepare")
        def final():
            order.append("final")
            if stopped:
                raise KernelError("executor_fenced")
        return final

    def boundary():
        nonlocal stopped
        order.append("boundary")
        stopped = True

    with pytest.raises(KernelError, match="executor_fenced"):
        admit_write(WriteAuthority(prepare), boundary)
    assert order == ["prepare", "boundary", "final"]


def test_real_core_preparation_cannot_write_after_foreign_recovery_transition(monkeypatch, managed_completed_ticket):
    operation = "recovery_comment"
    scenario, host = managed_completed_ticket
    kernel = scenario.kernel
    kernel.control("stop", "retain-independent-ticket", subject="ticket:4")
    assert kernel.control_host(host, "abandon", "recovery-authority-boundary",
        resource_inspections=scenario.port.inspect_resources(kernel.generation_resources())) == "pending"
    runner = scenario.runner()
    ports = runner.ports
    execute, inspect, source, delivery = ports.execute, ports.inspect, ports.source, ports.delivery
    core_admit = kernel.authorize_effect_observation
    selected = []
    active = []
    admitted = []
    changing = []
    during_final = False

    def final(effect, *args, **kwargs):
        nonlocal during_final
        during_final = True
        admitted.append(effect.effect_id)
        try:
            return core_admit(effect, *args, **kwargs)
        finally:
            during_final = False

    def observe_source(*args):
        assert not during_final, "Final Core admission performed source I/O"
        return source(*args)

    def observe_delivery(*args):
        assert not during_final, "Final Core admission performed delivery I/O"
        return delivery(*args)

    def observe_resources(resources):
        assert not during_final, "Final Core admission performed resource I/O"
        result = inspect(resources)
        if active and active[-1].operation == operation and not changing:
            effect = active[-1]
            changing.append(effect.effect_id)
            with kernel.storage.transaction(write=False) as tx:
                row = dict(tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,)))
                assert row["status"] == "prepared" and row["attempts"] == 0
            api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
                physical=scenario.port.physical, actor="another-maintainer",
                written=lambda *args: None, read_faults=set())
            endpoint = f"repos/{effect.repository}/issues/{effect.payload['issue']}"
            # Only the raw synthetic provider changes; Core/proof materials stay intact.
            transitions = ("open", "closed") if operation == "reopen_ticket" else ("closed", "open")
            for state in transitions:
                assert api.write(endpoint, "PATCH", {"state": state}).returncode == 0
            assert api.object(endpoint)["state"] == transitions[-1]
        return result

    def write(effect, authority):
        active.append(effect)
        if effect.operation == operation:
            selected.append(asdict(effect))
        try:
            return execute(effect, authority)
        finally:
            active.pop()

    monkeypatch.setattr(kernel, "authorize_effect_observation", final)
    monkeypatch.setattr(ports, "source", observe_source)
    monkeypatch.setattr(ports, "delivery", observe_delivery)
    monkeypatch.setattr(ports, "inspect", observe_resources)
    monkeypatch.setattr(ports, "execute", write)
    before = scenario.port.writes()
    with pytest.raises(KernelError, match="ticket_recovery_boundary_changed") as refused:
        for _ in range(40):
            step = prepare_scripted_agent(kernel, runner.run_once())
            assert step.call is None
            if changing:
                break
        else:
            raise AssertionError("Normal Abandon never reached the original recovery preparation")
    assert len(selected) == 1 and changing == [selected[0]["effect_id"]]
    assert changing[0] not in admitted
    assert refused.value.code == "ticket_recovery_boundary_changed"
    assert not [item for item in scenario.port.writes()[len(before):] if item["operation"] == operation]
    effect = selected[0]
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT * FROM effects WHERE id=?", (effect["effect_id"],))
        assert row["status"] == "prepared" and row["attempts"] == 0
        assert json.loads(tx.get(row["payload_id"], effect["subject"])) == effect["payload"]
    assert kernel.delivery(effect["subject"]).key == effect["subject"]
    assert scenario.port.objects.events(effect["payload"]["issue"])[-1]["actor"]["login"] == "another-maintainer"


@pytest.mark.parametrize("operation", ["pr_edit", "completion_comment", "close", "reopen_ticket"])
def test_remote_write_rechecks_its_prior_after_preparation(tmp_path, monkeypatch, operation):
    """The physical boundary must refuse before calling final Core admission.

    The managed recovery test above proves real preparation/admission wiring;
    these cases own the adapter-specific PR/comment/event comparisons.
    """
    from agent_run.contracts.write_authority import WriteAuthority
    from support.remote_effects import _fixture, _publish_pr
    from support.fixture_text import replace_once
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    fixture_authority = WriteAuthority(lambda: lambda: None)
    # Recovery observes the Issue timestamp as well as the timeline. Supply this
    # explicit provider field through the real gh executable, not a Core result.
    binary = tmp_path / "bin" / "gh"
    binary.write_text(replace_once(binary.read_text(),
        "'node_id':'issue-node-2','state':d['issue_state']}",
        "'node_id':'issue-node-2','state':d['issue_state'],'updated_at':d.get('issue_updated_at','2026-10-21T10:01:00Z')}"))
    issue = f"repos/{push.repository}/issues/2"
    if operation == "pr_edit":
        creation, pr = _publish_pr(port, push)
        effect = replace(creation, effect_id="edit", pr_number=pr.number,
            payload={"title": "New title", "body": "New body", "prior": {"title": pr.title, "body": pr.body}})
    else:
        effect = replace(push, effect_id="comment", operation="completion_comment", before_sha=push.after_sha,
            after_sha=None, pr_number=17,
            payload={"issue": 2, "merge_sha": push.after_sha, "kind": "ticket", "gate_mode": "reviewed"})
        if operation in {"close", "reopen_ticket"}:
            comment = port.execute(effect, fixture_authority)
            effect = replace(effect, effect_id="close", operation="close",
                payload={**effect.payload, "close_intent": comment.raw["close_intent"]})
        if operation == "reopen_ticket":
            closed = port.execute(effect, fixture_authority)
            effect = replace(effect, effect_id="reopen", operation="reopen_ticket",
                payload={**effect.payload, "close_receipt": asdict(closed),
                    "completion": {"subject": effect.subject, "merge_sha": effect.before_sha, "close_effect": effect.effect_id}})
    frozen, prepared, admitted = asdict(effect), [], []
    before = len(calls.read_text().splitlines()) if calls.exists() else 0
    body = None
    if operation == "completion_comment":
        body = port._comment_body(effect, port._comment_intent(effect, 0))

    def prepare():
        prepared.append(effect.effect_id)
        if operation == "pr_edit":
            port._write(f"repos/{effect.repository}/pulls/{effect.pr_number}", "PATCH",
                {"title": "External description", "body": "Preserve this external edit."}, fixture_authority)
        elif operation == "completion_comment":
            port._write(issue + "/comments", "POST", {"body": body}, fixture_authority)
        else:
            # Another actor's timeline is an external input to this adapter.
            document = json.loads(state.read_text())
            transitions = ("closed", "reopened") if operation == "close" else ("reopened", "closed")
            for minute, event in enumerate(transitions, 2):
                document["events"].append({"id": len(document["events"]) + 1,
                    "event": event, "actor": {"login": "another-maintainer"},
                    "created_at": f"2026-10-21T10:0{minute}:00Z"})
            document.update(issue_state="open" if operation == "close" else "closed",
                            issue_updated_at="2026-10-21T10:03:00Z")
            state.write_text(json.dumps(document))
        return lambda: admitted.append(effect.effect_id)

    reason = ("pr_boundary_changed" if operation == "pr_edit" else
              "ticket_recovery_boundary_changed" if operation == "reopen_ticket" else "operation_boundary_changed")
    with pytest.raises(KernelError, match=reason):
        port.execute(effect, WriteAuthority(prepare))
    assert prepared == [effect.effect_id] and admitted == [] and asdict(effect) == frozen
    writes = [json.loads(line) for line in calls.read_text().splitlines()[before:] if "--method" in line]
    assert len(writes) == (0 if operation in {"close", "reopen_ticket"} else 1)
    document = json.loads(state.read_text())
    if operation == "pr_edit":
        assert document["prs"][0]["title"] == "External description"
        assert document["prs"][0]["body"] == "Preserve this external edit."
    elif operation == "completion_comment":
        exact = [comment for comment in document["comments"] if port._marker(effect) in comment["body"]]
        assert len(exact) == 1 and exact[0]["body"] == body
    else:
        expected = ["closed", "reopened"] if operation == "close" else ["reopened", "closed"]
        assert [event["event"] for event in document["events"][-2:]] == expected
        assert all(event["actor"]["login"] == "another-maintainer" for event in document["events"][-2:])
        assert document["issue_state"] == ("open" if operation == "close" else "closed")


def test_pr_noop_foreign_readback_does_not_dispatch_a_new_admission(tmp_path, monkeypatch):
    from agent_run.contracts.write_authority import WriteAuthority
    from dataclasses import replace
    from support.remote_effects import _fixture, _pr_effect

    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    fixture_authority = WriteAuthority(lambda: lambda: None)
    assert port.execute(push, fixture_authority).disposition == "target"
    creation = _pr_effect(push)
    published = port.execute(creation, fixture_authority)
    assert published.disposition == "target" and published.pr is not None
    pr = published.pr
    noop = replace(creation, effect_id="unchanged-original-description", pr_number=pr.number,
        payload={"title": pr.title, "body": pr.body, "prior": {"title": pr.title, "body": pr.body}})
    original = asdict(noop)
    before = len(calls.read_text().splitlines())
    final_calls = []

    def prepare():
        port._write(f"repos/{noop.repository}/pulls/{pr.number}", "PATCH",
            {"title": "Preserve external description", "body": "An independent edit"}, fixture_authority)
        return lambda: final_calls.append(noop.effect_id)

    observed = port.execute(noop, WriteAuthority(prepare))
    assert observed.disposition == "third" and observed.pr is not None
    assert observed.pr.number == pr.number and observed.pr.head == pr.head
    assert observed.pr.title == "Preserve external description" and observed.pr.body == "An independent edit"
    assert final_calls == [] and asdict(noop) == original
    commands = [json.loads(line) for line in calls.read_text().splitlines()[before:]]
    writes = [command for command in commands if "--method" in command]
    assert len(writes) == 1 and writes[0][writes[0].index("--method") + 1] == "PATCH"
    assert json.loads(state.read_text())["prs"][0]["title"] == "Preserve external description"
