"""Repair write fences and readback retain real Core, SQLite and private Git.

GitHub and Agent outputs use the existing scenario ports. The physical write
barrier changes no Core selection, stored review or cumulative allowance.
"""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.model import DeliveryKind
from support.effect_boundary import advance_to_effect, effect_state
from support.run_scoped_kernel import domain_scenario
from support.parent_repair import _repairs, _requests, _run, _until


@pytest.mark.parametrize("sent", [False, True], ids=["prepared", "sent-not-applied"])
@pytest.mark.parametrize("changed_ref", ["target_ref", "source_ref", "repair_source"], ids=["default", "run", "repair-source"])
def test_repair_merge_intent_cannot_authorize_a_new_combination(
    tmp_path, monkeypatch, domain_scenario, changed_ref, sent,
):
    scenario = domain_scenario(tmp_path / "write-fence", DeliveryKind.FINAL_RUN,
                               reviews=("fail", "pass"), checks=("pass",))
    held = advance_to_effect(scenario, monkeypatch, "merge", sent=sent)
    effect = held.effect
    assert effect.payload["repair_id"]
    before = scenario.kernel.status(scenario.delivery.key)
    old_pr = scenario.port.pr_by_number(effect.pr_number)
    latest = scenario.port.external_remote_append(effect.source_ref if changed_ref == "repair_source" else getattr(scenario.delivery, changed_ref),
        {"concurrent-append.txt": "New independently committed work\n"}, "write-fence-append")
    # Drive the saved, unstarted intent through its ordinary Core entry. It
    # must be superseded without a physical merge or a new Development call.
    final = _run(scenario)
    assert final.kind == "approval", (final, scenario.kernel.status(scenario.delivery.key))
    assert effect_state(scenario, effect)[:2] == ("retired", int(sent))
    assert scenario.development_count == 1 and scenario.review_count == 3
    repairs = _repairs(scenario)
    assert len(repairs) == 1 and repairs[0]["number"] == old_pr.number
    run = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert scenario.port.git.is_ancestor(latest.sha, run.sha)
    assert scenario.port.git.run("show", f"{run.sha}:concurrent-append.txt").stdout == "New independently committed work\n"
    assert len(_requests(scenario, "PUT", "/merge")) == 2  # Historical Ticket and latest repair.
    assert not any(row["operation"] == "merge" and row["intent"] == effect.effect_id
                   for row in scenario.writes())
    after = scenario.kernel.status(scenario.delivery.key)
    assert after["budgets"]["integration"]["developments_used"] == before["budgets"]["integration"]["developments_used"]
    assert after["budgets"]["integration"]["reviews_used"] == before["budgets"]["integration"]["reviews_used"] + 1


@pytest.mark.parametrize("changed_basis", ["run", "requirements"])
def test_lost_repair_merge_receipt_is_settled_before_reaccepting_changed_basis(
    tmp_path, monkeypatch, domain_scenario, changed_basis,
):
    scenario = domain_scenario(tmp_path / "lost-merge", DeliveryKind.FINAL_RUN,
                               reviews=("fail", "pass"), checks=("none",))
    held = advance_to_effect(scenario, monkeypatch, "merge", sent=True)
    effect, authority = held.effect, held.authorize
    before = scenario.kernel.status(scenario.delivery.key)
    scenario.port.lose_response.add("merge")
    with pytest.raises(ReadUnavailable, match="merge took effect; response lost"):
        scenario.port.execute(effect, authority)
    merged = scenario.port.pr_by_number(effect.pr_number)
    assert merged.state == "MERGED"
    if changed_basis == "run":
        latest = scenario.port.external_remote_append(scenario.delivery.source_ref,
            {"after-merge.txt": "Work appended after the acknowledged-lost merge\n"}, "after-merge")
    else:
        latest = merged.merged
        scenario.requirements = replace(scenario.requirements, parent=replace(scenario.requirements.parent,
            body=scenario.requirements.parent.body + "\nReaccept the current Parent requirements."))
        retained = Path(scenario.delivery.checkout) / "retained-uncommitted.txt"
        retained.write_text("Preserve work after the original merge\n")
    assert scenario.kernel.status(effect.subject)["pending_write"]["status"] == "sent"
    # Reload and resume through the public control boundary. The original
    # sent intent survives; a new Executor must read it before planning work.
    path, run_id = scenario.kernel.storage.path, scenario.kernel.storage.run_id
    scenario.kernel.control("stop", "stop-after-lost-repair")
    scenario.kernel.confirm_executor_exit(scenario.execution)
    scenario.kernel = Kernel(path, run_id=run_id)
    scenario.kernel.clock = lambda: 100.
    scenario.kernel.control("resume", "resume-lost-repair")
    scenario.execution = scenario.kernel.claim_executor("resumed-repair-executor")
    consumed = _until(scenario, lambda state: state["repair_history"][0]["state"] == "merged")
    assert consumed["phase"] == ("integration" if changed_basis == "run" else "refresh_final")
    assert effect_state(scenario, effect)[:2] == ("confirmed", 1)
    assert consumed["budgets"] == before["budgets"]
    assert scenario.review_count == 2 and scenario.development_count == 1
    assert not any(row["kind"] == "acceptance_promoted" for row in scenario.kernel.history(effect.subject))
    assert len([row for row in scenario.writes()
                if row["operation"] == "merge" and row["intent"] == effect.effect_id]) == 1
    if changed_basis == "requirements":
        assert scenario.kernel.source_status()["blocked"] == "final_refresh"
        world = scenario.world()
        assert scenario.kernel.control("requeue", "reaccept-after-merge", subject=effect.subject, world=world) == "applied"
        fresh = scenario.kernel.status(effect.subject)
        assert fresh["generation"] == before["generation"] + 1 and fresh["budgets"] == before["budgets"]
        assert retained.read_text() == "Preserve work after the original merge\n"
    assert _run(scenario).kind == "approval"
    assert scenario.review_count == 3 and scenario.development_count == 1
    run = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert scenario.port.git.is_ancestor(latest.sha, run.sha)
    if changed_basis == "run":
        assert scenario.port.git.run("show", f"{run.sha}:after-merge.txt").stdout == "Work appended after the acknowledged-lost merge\n"
    else:
        with scenario.kernel.storage.transaction(write=False) as tx:
            attempt = tx.one("SELECT context_id FROM attempts WHERE run_id=? AND subject=? AND role='review' ORDER BY ordinal DESC LIMIT 1",
                             (run_id, effect.subject))
            context = json.loads(tx.get(attempt['context_id'], effect.subject))
            assert context['parent_revision'] == scenario.kernel.status(effect.subject)['parent_revision'] != before['parent_revision']
            assert any("Reaccept the current Parent requirements." in item.get('body', '') for item in context['requirements'])
    assert scenario.port.pr_by_number(effect.pr_number) == merged
    assert len([row for row in scenario.writes()
                if row["operation"] == "merge" and row["intent"] == effect.effect_id]) == 1
    displayed = scenario.kernel.status(effect.subject)["repair_history"][0]
    assert displayed["merge_sha"] == merged.merged.sha


@pytest.mark.parametrize("missing", ["ancestry", "checks-demand"])
def test_core_rejects_incomplete_repair_read_without_changing_retained_work(
    tmp_path, domain_scenario, missing,
):
    scenario = domain_scenario(tmp_path / "incomplete-child", DeliveryKind.FINAL_RUN,
                               reviews=("fail", "pass"), checks=("none",))
    _until(scenario, lambda state: state["phase"] == "repair_checks")
    ports = scenario.make_ports(scenario.kernel.operation_binding())
    request = scenario.kernel.begin_observation(scenario.delivery.key)
    delivery = scenario.kernel.accept_requirements(request, ports.source(request.parent_number))
    facts = ports.delivery(delivery.query)
    assert facts.repair is not None and facts.repair.checks.result == "none"
    child = (replace(facts.repair, ancestry_checked=None) if missing == "ancestry"
             else replace(facts.repair, checks_requested=False))
    before = scenario.kernel.status(scenario.delivery.key)
    writes = scenario.writes()
    with pytest.raises(ReadUnavailable) as caught:
        scenario.kernel.accept_delivery(delivery, replace(facts, repair=child))
    assert caught.value.code == "repair_observation_incomplete"
    assert scenario.kernel.status(scenario.delivery.key) == before
    assert scenario.writes() == writes


def test_repair_source_rewrite_is_preserved_as_an_identity_boundary(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "rewrite", DeliveryKind.FINAL_RUN,
                               reviews=("fail", "pass"), checks=("pending",))
    before = _until(scenario, lambda state: state["phase"] == "repair_checks")
    repair = before["repair_pr"]
    response = scenario.port.physical.push(repair["source_ref"], repair["published_sha"], scenario.initial.sha)
    assert response.returncode == 0
    assert _run(scenario).kind == "paused"
    after = scenario.kernel.status(scenario.delivery.key)
    assert after["repair_pr"] == repair and after["budgets"] == before["budgets"]
    assert "repair_pr_identity_changed" in after["gates"]
    assert len(_repairs(scenario)) == 1


def test_open_repair_pr_alone_cannot_prove_an_unknown_merge_did_not_happen(tmp_path, monkeypatch):
    from agent_run.contracts.write_authority import WriteAuthority
    from support.remote_effects import existing_pr

    port, git, push, _state, calls, pr = existing_pr(tmp_path, monkeypatch)
    effect = replace(push, operation="merge", effect_id="repair-merge-unknown", pr_number=pr.number,
        payload={"repair_id": "original-repair", "method": "merge",
                 "parents": [push.base.sha, push.after_sha], "commit_message": "merge: original repair"})
    # The merge could already have happened while the PR snapshot still says
    # OPEN. The target contains the original candidate, so non-application
    # cannot be proved. Preserve uncertainty instead of replaying the merge.
    contained = git.make_commit(push.candidate.tree, (push.base.sha, push.after_sha), "independently observed merge")
    git.run("push", "origin", contained.sha + ":" + push.target_ref)
    result = port.reconcile(effect)
    assert result.disposition == "unknown" and "merge_not_applied" not in result.raw
    again = port.execute(effect, WriteAuthority(lambda: pytest.fail("Unknown merge must not acquire another write")))
    assert again.disposition == "unknown"
    assert not any('"PUT"' in line and "/merge" in line for line in calls.read_text().splitlines())


@pytest.mark.parametrize('changed_basis', ['requirements', 'graph'])
def test_prepared_repair_cannot_write_after_its_source_contract_changes(
    tmp_path, monkeypatch, domain_scenario, changed_basis,
):
    from agent_run.errors import KernelError

    scenario = domain_scenario(tmp_path / 'source-contract', DeliveryKind.FINAL_RUN,
                               reviews=('fail', 'pass'), checks=('pass',))
    held = advance_to_effect(scenario, monkeypatch, 'merge')
    effect = held.effect
    before = scenario.kernel.status(effect.subject)
    writes = scenario.writes()
    retained = Path(scenario.delivery.checkout) / 'uncommitted-contract-work.txt'
    retained.write_text('Keep work while the original repair loses authority\n')
    parent = scenario.requirements.parent
    assert parent.sub_issue_numbers
    parent = (replace(parent, body=parent.body + '\nUpdated acceptance requirements.')
              if changed_basis == 'requirements' else replace(parent, sub_issue_numbers=()))
    scenario.requirements = replace(scenario.requirements, parent=parent)

    with pytest.raises(KernelError):
        held.authorize.prepare()()

    assert effect_state(scenario, effect)[1] == 0
    assert scenario.writes() == writes
    assert scenario.kernel.status(effect.subject)['budgets'] == before['budgets']
    assert retained.read_text() == 'Keep work while the original repair loses authority\n'
    assert not any(row['operation'] == 'merge' and row['intent'] == effect.effect_id for row in scenario.writes())
