"""Parent and repair observations share the same Run over real private Git."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from support.remote_effects import existing_pr


def _repair_delivery(tmp_path, monkeypatch):
    port, git, push, state, calls, pr = existing_pr(tmp_path, monkeypatch)
    candidate = git.make_commit(pr.head.tree, (pr.head.sha,), "Parent repair candidate")
    repair_ref = "refs/heads/parent-repair"
    git.run("push", "origin", candidate.sha + ":" + repair_ref)
    document = json.loads(state.read_text())
    repair = deepcopy(document["prs"][0])
    repair.update(number=18, title="Parent repair", body="Accurately reviewed Parent repair")
    repair["head"].update(ref=repair_ref.removeprefix("refs/heads/"), sha=candidate.sha)
    repair["base"].update(ref=push.source_ref.removeprefix("refs/heads/"), sha=pr.head.sha)
    document["prs"].append(repair)
    state.write_text(json.dumps(document))
    query = DeliveryRead(push.source_ref, push.target_ref, pr.number, (),
        repair=DeliveryRead(repair_ref, push.source_ref, repair["number"], ()))
    return port, git, query, calls, pr, candidate


@pytest.mark.parametrize("combined", [False, True], ids=["delivery", "source-and-delivery"])
def test_repair_and_final_pr_observe_their_own_target_checks(tmp_path, monkeypatch, combined):
    port, _, query, calls, final_pr, candidate = _repair_delivery(tmp_path, monkeypatch)
    start = len(calls.read_text().splitlines())
    facts = (port.complete_observation(query, port.source_observation(1, query)) if combined
             else port.delivery(query))
    assert facts.source == final_pr.head and facts.target == final_pr.base
    assert facts.pr.number == final_pr.number and facts.checks.pr_number == final_pr.number
    assert facts.repair.source == candidate and facts.repair.target == facts.source
    assert facts.repair.pr.number == 18 and facts.repair.checks.pr_number == 18
    assert facts.repair.checks.head_sha == candidate.sha and facts.repair.repair is None
    assert facts.checks.result == facts.repair.checks.result == "none"
    commands = [json.loads(line) for line in calls.read_text().splitlines()[start:]]
    for branch in ("main", "delivery"):
        assert ["api", f"repos/example/project/branches/{branch}/protection/required_status_checks"] in commands
    assert not any("--method" in command for command in commands)


@pytest.mark.parametrize("invalid", ["nested", "other-target", "checks-demand"])
def test_invalid_repair_demand_is_rejected_before_io(tmp_path, monkeypatch, invalid):
    port, _, query, calls, _, _ = _repair_delivery(tmp_path, monkeypatch)
    if invalid == "nested":
        repair = replace(query.repair, repair=query.repair)
    elif invalid == "other-target":
        repair = replace(query.repair, target_ref=query.target_ref)
    else:
        repair = replace(query.repair, read_checks=False, check_head="a" * 40)
    query = replace(query, repair=repair)
    start = calls.read_text()
    with pytest.raises(ReadUnavailable) as caught:
        port.delivery(query)
    assert caught.value.code == ("checks_request_invalid" if invalid == "checks-demand" else "repair_request_invalid")
    assert calls.read_text() == start


def test_repair_read_cannot_supply_another_run_version(tmp_path, monkeypatch):
    port, git, query, _, final_pr, _ = _repair_delivery(tmp_path, monkeypatch)
    later = git.make_commit(final_pr.head.tree, (final_pr.head.sha,), "Concurrent Run append")
    original = port._finish_delivery

    def finish(request, *args):
        if request.repair is not None:
            git.run("push", "origin", later.sha + ":" + query.source_ref)
        return original(request, *args)

    monkeypatch.setattr(port, "_finish_delivery", finish)
    with pytest.raises(ReadUnavailable, match="Repair and Run observations have not converged"):
        port.delivery(query)


@pytest.mark.parametrize("ref", ["source_ref", "target_ref"], ids=["run", "default"])
def test_primary_refs_are_rechecked_after_the_repair_observation(tmp_path, monkeypatch, ref):
    port, git, query, _, final_pr, _ = _repair_delivery(tmp_path, monkeypatch)
    before = final_pr.head if ref == "source_ref" else final_pr.base
    later = git.make_commit(before.tree, (before.sha,), "Concurrent primary append")
    original = port._finish_delivery

    def finish(request, *args):
        result = original(request, *args)
        if request.source_ref == query.repair.source_ref:
            git.run("push", "origin", later.sha + ":" + getattr(query, ref))
        return result

    monkeypatch.setattr(port, "_finish_delivery", finish)
    with pytest.raises(ReadUnavailable, match="Remote refs changed during delivery observation"):
        port.delivery(query)
