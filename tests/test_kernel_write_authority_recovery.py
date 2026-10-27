"""Recovery facts are checked after slow preparation and before live Core admission."""
from support.core_operations import prepare_scripted_agent
from dataclasses import asdict
import json

import pytest

from agent_run.errors import KernelError
from support.offline_api import SyntheticGitHub
from support.control_scenarios import managed_abandon_scenario


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


@pytest.mark.parametrize("operation", ["reopen_ticket", "recovery_comment"])
def test_real_core_preparation_cannot_write_after_foreign_recovery_transition(tmp_path, monkeypatch, operation):
    scenario, host = managed_abandon_scenario(tmp_path)
    assert scenario.run().kind == "complete"
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


@pytest.mark.parametrize("operation", ["pr_edit", "completion_comment", "close"])
def test_real_core_original_write_rechecks_its_prior_after_preparation(tmp_path, monkeypatch, operation):
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.model import DeliveryKind
    from support.effect_boundary import EffectBoundary
    from support.offline import Scenario

    scenario = Scenario(tmp_path / "delivery", DeliveryKind.TICKET,
                        checks=("fail", "none") if operation == "pr_edit" else ("none",))
    make_ports = scenario.make_ports

    def hold_original(binding):
        ports = make_ports(binding)
        execute = ports.execute
        def hold(effect, authority):
            selected = (effect.operation == "pr_write" and effect.pr_number is not None
                        if operation == "pr_edit" else effect.operation == operation)
            if selected:
                # Preserve a real admission for this original unresolved request.
                authority.prepare()()
                raise EffectBoundary(effect, authority)
            return execute(effect, authority)
        ports.execute = hold
        return ports

    with monkeypatch.context() as patch:
        patch.setattr(scenario, "make_ports", hold_original)
        with pytest.raises(EffectBoundary) as captured:
            for _ in range(160):
                step = scenario.step()
                assert step.kind not in {"paused", "waiting", "approval", "complete"}, step
    held = captured.value
    effect, authority = held.effect, held.authorize
    frozen = asdict(effect)
    kernel = scenario.kernel
    with kernel.storage.transaction(write=False) as tx:
        original = dict(tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,)))
        assert original["status"] == "sent" and original["attempts"] == 1
        payload = tx.get(original["payload_id"], effect.subject)
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
        physical=scenario.port.physical, actor=effect.actor if operation == "completion_comment" else "another-maintainer",
        written=lambda *args: None, read_faults=set())
    remote = scenario.port._remote(effect.repository)
    issue = f"repos/{effect.repository}/issues/{effect.payload.get('issue', scenario.delivery.issue)}"
    commands = len(scenario.port.objects.snapshot()["api_requests"])
    prepared, admitted = [], []
    body = None
    if operation == "completion_comment":
        baseline = max((event["id"] for event in scenario.port.objects.events(effect.payload["issue"])), default=0)
        intent = remote._comment_intent(effect, baseline)
        body = remote._comment_body(effect, intent)

    def prepare():
        final_admission = authority.prepare()
        prepared.append(effect.effect_id)
        if operation == "pr_edit":
            api.write(f"repos/{effect.repository}/pulls/{effect.pr_number}", "PATCH",
                      {"title": "External description", "body": "Preserve this external edit."})
        elif operation == "completion_comment":
            # A delayed copy of the already admitted original request becomes visible.
            api.write(issue + "/comments", "POST", {"body": body})
        else:
            api.write(issue, "PATCH", {"state": "closed"})
            api.write(issue, "PATCH", {"state": "open"})
        def final():
            admitted.append(effect.effect_id)
            final_admission()
        return final

    with pytest.raises(KernelError, match="pr_boundary_changed" if operation == "pr_edit" else "operation_boundary_changed"):
        remote.execute(effect, WriteAuthority(prepare))
    assert prepared == [effect.effect_id] and admitted == []
    assert asdict(effect) == frozen
    with kernel.storage.transaction(write=False) as tx:
        assert dict(tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,))) == original
        assert tx.get(original["payload_id"], effect.subject) == payload
    writes = scenario.port.objects.snapshot()["api_requests"][commands:]
    assert len(writes) == (2 if operation == "close" else 1)
    if operation == "pr_edit":
        pr = api.object(f"repos/{effect.repository}/pulls/{effect.pr_number}")
        assert pr["title"] == "External description" and pr["body"] == "Preserve this external edit."
        assert [(row["method"], row["endpoint"]) for row in writes] == [("PATCH", f"repos/{effect.repository}/pulls/{effect.pr_number}")]
    elif operation == "completion_comment":
        exact = [comment for comment in scenario.port.objects.comments(effect.payload["issue"]) if remote._marker(effect) in comment["body"]]
        assert len(exact) == 1 and exact[0]["body"] == body
        assert [(row["method"], row["endpoint"]) for row in writes] == [("POST", issue + "/comments")]
    else:
        assert api.object(issue)["state"] == "open"
        events = scenario.port.objects.events(effect.payload["issue"])[-2:]
        assert [event["event"] for event in events] == ["closed", "reopened"]
        assert all(event["actor"]["login"] == "another-maintainer" for event in events)


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
