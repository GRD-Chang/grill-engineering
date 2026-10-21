"""Remote heads and original-intent readback, independent of local Git refs."""

from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, Effect, Kernel
from agent_run_kernel.offline import LostResponse, Scenario


def merge_intent(scenario: Scenario) -> Effect:
    for _ in range(100):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.effect is not None and step.effect.operation == "merge":
            return step.effect
        scenario.handle(step)
        assert step.kind not in {"paused", "complete", "waiting"}
    raise AssertionError("Ticket did not reach its authorized merge request")


def test_remote_source_advance_after_approval_prevents_merge_and_close(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    source = scenario.port.ref_commit(scenario.delivery.source_ref)
    scenario.kernel.control("approve", "approve-exact-source", subject=scenario.delivery.key, world=scenario.world())
    third = scenario.port.external_remote_append(scenario.delivery.source_ref, {"foreign.txt": "New remote-only work\n"}, "remote-source")
    assert scenario.port.ref_commit(scenario.delivery.source_ref) == source
    assert scenario.world().pr.head == third
    assert scenario.run().kind == "paused"
    assert scenario.port.remote_commit(scenario.delivery.source_ref) == third
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == target
    assert scenario.port.pull_request(scenario.delivery.key).state == "OPEN"
    assert not [write for write in scenario.port.writes() if write["operation"] in {"merge", "close"}]


@pytest.mark.parametrize("server_committed", [False, True])
def test_original_merge_readback_distinguishes_server_commit_from_partial_local_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_committed: bool,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    effect = merge_intent(scenario)
    before = scenario.port.remote_commit(effect.target_ref)
    assert scenario.kernel.dispatch(effect, scenario.execution, scenario.world())
    if server_committed:
        scenario.port.lose_response.add("merge")
        with pytest.raises(LostResponse):
            scenario.port.execute(effect, lambda: scenario.kernel.guard_effect(effect, scenario.execution, scenario.world()))
    else:
        def fail_before_remote_commit(*args: object) -> None:
            raise OSError("Synthetic server transaction failed after local mirror changed")
        monkeypatch.setattr(scenario.port, "_write_pr_in", fail_before_remote_commit)
        with pytest.raises(OSError):
            scenario.port.execute(effect, lambda: scenario.kernel.guard_effect(effect, scenario.execution, scenario.world()))
        assert scenario.port.remote_commit(effect.target_ref) == before
        assert scenario.port.ref_commit(effect.target_ref) != before
        assert scenario.port.pull_request(effect.subject).state == "OPEN"
    observed = scenario.port.reconcile(effect)
    assert observed.disposition == ("target" if server_committed else "unknown")
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    scenario.kernel.reconcile(effect, observed, scenario.execution, scenario.world())
    if server_committed:
        assert scenario.run().kind == "complete"
        assert len([write for write in scenario.port.writes() if write["operation"] == "merge"]) == 1
    else:
        assert scenario.kernel.status(effect.subject)["pending_write"]["id"] == effect.effect_id
        assert scenario.kernel.status(effect.subject)["holds_integration"] is True
        assert scenario.kernel.advance(effect.subject, scenario.execution, scenario.world()).kind == "reconcile"
