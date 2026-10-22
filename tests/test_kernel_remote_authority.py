"""Remote heads and original-intent readback, independent of local Git refs."""

from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, Effect, Kernel
from agent_run_kernel.offline import LostResponse, Scenario
from test_kernel_remote_effects import EffectBoundary, effect_boundary


def merge_intent(scenario: Scenario, monkeypatch: pytest.MonkeyPatch):
    with effect_boundary(scenario, monkeypatch, "merge"):
        with pytest.raises(EffectBoundary) as boundary:
            scenario.run()
    return boundary.value


def test_remote_source_advance_after_approval_prevents_merge_and_close(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    source = scenario.port.ref_commit(scenario.delivery.source_ref)
    original_pr = scenario.port.pull_request(scenario.delivery.key)
    original_counts = (scenario.development_count, scenario.review_count, scenario.publication_count)
    scenario.kernel.control("approve", "approve-exact-source", subject=scenario.delivery.key, world=scenario.world())
    third = scenario.port.external_remote_append(scenario.delivery.source_ref, {"foreign.txt": "New remote-only work\n"}, "remote-source")
    assert scenario.port.ref_commit(scenario.delivery.source_ref) == source
    assert scenario.world().pr.head == third
    assert scenario.run().kind == "approval"
    updated = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert updated is not None and scenario.port.git.is_ancestor(third.sha, updated.sha)
    assert scenario.port.git.run("show", updated.sha + ":foreign.txt").stdout == "New remote-only work\n"
    assert scenario.development_count == original_counts[0]
    assert scenario.review_count == original_counts[1] + 1
    assert scenario.publication_count == original_counts[2] + 1
    assert scenario.kernel.status(scenario.delivery.key)["resync"]["used"] == 1
    assert scenario.port.pull_request(scenario.delivery.key).number == original_pr.number
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == target
    assert scenario.port.pull_request(scenario.delivery.key).state == "OPEN"
    assert not [write for write in scenario.port.writes() if write["operation"] in {"merge", "close"}]


@pytest.mark.parametrize("server_committed", [False, True])
def test_original_merge_readback_distinguishes_server_commit_from_partial_local_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_committed: bool,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    boundary = merge_intent(scenario, monkeypatch)
    effect = boundary.effect
    before = scenario.port.remote_commit(effect.target_ref)
    if server_committed:
        scenario.port.lose_response.add("merge")
        with pytest.raises(LostResponse):
            scenario.port.execute(effect, boundary.authorize)
    else:
        def fail_before_remote_commit(*args: object) -> None:
            raise OSError("Synthetic server transaction failed after local mirror changed")
        monkeypatch.setattr(scenario.port, "_write_pr_in", fail_before_remote_commit)
        with pytest.raises(OSError):
            scenario.port.execute(effect, boundary.authorize)
        assert scenario.port.remote_commit(effect.target_ref) == before
        assert scenario.port.ref_commit(effect.target_ref) != before
        assert scenario.port.pull_request(effect.subject).state == "OPEN"
    observed = scenario.port.reconcile(effect)
    assert observed.disposition == ("target" if server_committed else "unknown")
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    scenario.runner().run_once()
    if server_committed:
        assert scenario.run().kind == "complete"
        assert len([write for write in scenario.port.writes() if write["operation"] == "merge"]) == 1
    else:
        assert scenario.kernel.status(effect.subject)["pending_write"]["id"] == effect.effect_id
        assert scenario.kernel.status(effect.subject)["holds_integration"] is True
        with effect_boundary(scenario, monkeypatch, "merge"):
            with pytest.raises(EffectBoundary) as pending:
                scenario.runner().run_once()
        assert pending.value.phase == "reconcile" and pending.value.effect == effect
