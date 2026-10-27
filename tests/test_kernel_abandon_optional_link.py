"""An original optional display intent cannot become an Abandon obligation."""

import json
from pathlib import Path

import pytest

from agent_run.core.kernel import Kernel
from agent_run.runtime.executor import _make_ports
from agent_run.model import Execution
from support.core_operations import CoreOperations
from support.control_scenarios import prepared_control


def test_original_executor_abandons_with_its_optional_link_still_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_control = Kernel.control_host
    captured = []

    def control_with_original_link(kernel, request, kind, action_id, **arguments):
        if kind == "abandon":
            execution = Execution(request.run_id, request.owner, request.epoch)
            operations = CoreOperations(kernel, execution, _make_ports)

            def interrupt_link(effect, authorize):
                assert effect.operation == "issue_link" and effect.subject == "ticket:3"
                authorize.prepare()()
                captured.append((kernel, effect))
                raise OSError("Original optional display call interrupted before its write")

            with monkeypatch.context() as patch:
                patch.setattr(operations.runner.ports, "execute", interrupt_link)
                interrupted = operations.once(agent_capacity=0)
            assert interrupted.call is None and interrupted.reason == "original_write_readback_required"
            original_kernel, optional = captured[0]
            assert original_kernel is kernel and len(captured) == 1
            assert kernel.status(optional.subject)["issue_link"]["state"] == "indeterminate"
        result = original_control(kernel, request, kind, action_id, **arguments)
        if kind == "abandon":
            with kernel.storage.transaction(write=False) as tx:
                action = tx.one("SELECT * FROM actions WHERE id=?", (action_id,))
                receipt = json.loads(tx.get(action["receipt_id"], action["subject"] or "ticket:3"))
                assert optional.effect_id in receipt["abandonment"]["unsettled"]
        return result

    monkeypatch.setattr(Kernel, "control_host", control_with_original_link)
    scenario, request, original, state, calls, operations, world = prepared_control(tmp_path, monkeypatch, "abandon")
    kernel, subject = scenario.kernel, scenario.delivery.key
    resources = kernel.generation_resources()
    inspections = operations.runner.ports.inspect(resources)
    assert kernel.control_host(request, "abandon", "abandon-original", resource_inspections=inspections) == "pending"
    for _ in range(30):
        step = operations.once()
        assert step.call is None and step.kind in {"progress", "complete"}
        if step.kind == "complete":
            break
    else:
        raise AssertionError("The original Action's finite effects did not complete")
    assert all(not Path(resource.checkout).exists() for resource in resources)
    actual = json.loads(state.read_text())
    assert actual["prs"][0]["state"] == "closed"
    assert len(actual["comments"]) == len(actual["events"]) == 1
    observed_calls = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("POST" in call for call in observed_calls) == sum("PATCH" in call for call in observed_calls) == 1
    assert any(call[:2] == ["api", "graphql"] for call in observed_calls)
    assert len(captured) == 1
    kernel, original = captured[0]
    assert kernel.source_status()["abandonment"]["status"] == "complete"
    assert kernel.status(original.subject)["issue_link"]["state"] == "indeterminate"
    with kernel.storage.transaction(write=False) as tx:
        effects = tx.rows("SELECT id,status,receipt_id,attempts FROM effects WHERE operation='issue_link'")
        assert len(effects) == 1
        assert tuple(effects[0]) == (original.effect_id, "sent", None, 1)
        assert not tx.rows("SELECT subject FROM gates WHERE code='execution_failed'")
    calls = [json.loads(line) for line in (tmp_path / "transport" / "calls.jsonl").read_text().splitlines()]
    assert not any(call[:2] == ["api", "graphql"] and "POST" in call for call in calls)
