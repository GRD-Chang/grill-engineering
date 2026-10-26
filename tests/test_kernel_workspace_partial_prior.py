"""Partial native workspace writes retain their intent and resource witnesses."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.model import Issue, ParentIssue, Requirements
from agent_run.adapters.git_objects import LocalGit
from support.offline import GraphScenario
from test_kernel_workspace_allocation import workspace_intent
from support.effect_boundary import EffectBoundary, effect_boundary
from support.core_operations import CoreOperations




@pytest.mark.parametrize("control", ["abandon", "source-change"])
def test_partial_workspace_prior_keeps_original_intent_and_resource_facts(tmp_path, monkeypatch, control):
    scenario = GraphScenario(tmp_path / "scenario", Requirements(
        ParentIssue(1, "Parent", "Original requirement", (2,)),
        (Issue(2, "Ticket", "Implement the request", "OPEN", frozenset({"ready-for-agent"})),)))
    kernel = scenario.kernel
    original = workspace_intent(scenario, monkeypatch).effect
    checkout = Path(original.payload["checkout"])
    run = LocalGit.run
    intercepted = []

    def interrupted(git, *arguments, **kwargs):
        if arguments[:2] == ("worktree", "add") and str(checkout) in arguments:
            intercepted.append(arguments)
            raise OSError("Synthetic interruption after the authorized ref write")
        return run(git, *arguments, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(LocalGit, "run", interrupted)
        first = scenario.runner().run_once()
    assert first.reason == "original_write_readback_required" and len(intercepted) == 1
    assert not checkout.exists()
    ref = scenario.port.git.run("rev-parse", original.source_ref).stdout.strip()
    assert ref == original.before_sha
    ports = scenario.make_ports(scenario.runner().binding)
    assert ports.reconcile(original).disposition == "prior"
    resources = kernel.generation_resources()
    resource = next(r for r in resources if r.subject == original.subject and r.kind == "development")
    assert (resource.source_ref, resource.checkout, resource.head_sha) == (original.source_ref, str(checkout), ref)
    with kernel.storage.transaction(write=False) as tx:
        before = dict(tx.one("SELECT * FROM effects WHERE id=?", (original.effect_id,)))
        assert (before["status"], before["attempts"]) == ("sent", 1)

    if control == "abandon":
        kernel.control("abandon", "retain-partial-workspace", resource_inspections=ports.inspect(resources))
        event = "abandoned_business_intent_not_applied"
    else:
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, body="A legitimately changed requirement"))
        event = "stale_intent_not_applied"
    settled = scenario.runner().run_once()
    assert settled.kind == "progress"
    with kernel.storage.transaction(write=False) as tx:
        after = dict(tx.one("SELECT * FROM effects WHERE id=?", (original.effect_id,)))
        assert (after["status"], after["attempts"], after["payload_id"]) == ("retired", 1, before["payload_id"])
        assert tx.rows("SELECT position FROM history WHERE identity=? AND kind=?", (original.effect_id, event))
        raw = [json.loads(r[0]) for r in tx.rows("SELECT m.body FROM history h JOIN material m ON m.id=h.material_id "
            "WHERE h.kind='effect_readback' AND h.identity=?", (original.effect_id,))]
        assert raw[-1]["result"]["disposition"] == "prior" and raw[-1]["dispatch_serial"] == 1
        assert not tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND state='complete'", (original.subject,))
        if control == "abandon":
            assert tx.one("SELECT status FROM actions WHERE id='retain-partial-workspace'")[0] == "pending"
    assert resource in kernel.generation_resources()
    assert scenario.port.git.run("rev-parse", original.source_ref).stdout.strip() == ref
    assert not checkout.exists()

    if control == "abandon":
        # Retiring the partial business intent does not itself retire a resource.
        # The same witness must reach the later explicit cleanup operation.
        with effect_boundary(scenario, monkeypatch, "abandon_worktree"):
            with pytest.raises(EffectBoundary) as boundary:
                CoreOperations(kernel, scenario.execution, scenario.make_ports).until(lambda step: False, limit=16)
        cleanup = boundary.value.effect
        assert cleanup.payload["resource"]["source_ref"] == original.source_ref
        assert cleanup.payload["resource"]["checkout"] == str(checkout)
        assert cleanup.payload["resource"]["head_sha"] == ref
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT status FROM actions WHERE id='retain-partial-workspace'")[0] == "pending"
    else:
        # With no Attempt, the changed source may be accepted normally. A
        # newly authorized workspace may proceed; the retired intent is kept.
        next_step = scenario.runner().run_once()
        assert next_step.kind == "progress" and next_step.call is None
    assert resource in kernel.generation_resources()
    assert scenario.port.git.run("rev-parse", original.source_ref).stdout.strip() == ref
