"""An original optional display intent cannot become an Abandon obligation."""

import json
from pathlib import Path

import pytest

from agent_run_kernel import Kernel
from test_kernel_executor_controls import (
    test_native_executor_consumes_the_original_control_action_with_fresh_source_and_resources as _control_case,
)


def test_original_executor_abandons_with_its_optional_link_still_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_control = Kernel.control_host
    original_runner = Kernel.operation_runner
    captured = []

    def runner_with_interrupted_link(kernel, execution, make_ports):
        def physical_ports(binding):
            ports = make_ports(binding)
            execute = ports.execute

            def interrupt_link(effect, authorize):
                if effect.operation == "issue_link":
                    authorize()
                    captured.append((kernel, effect))
                    raise OSError("Original optional display call interrupted before its write")
                return execute(effect, authorize)

            ports.execute = interrupt_link
            return ports

        return original_runner(kernel, execution, physical_ports)

    def control_with_original_link(kernel, request, kind, action_id, **arguments):
        if kind == "abandon":
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

    monkeypatch.setattr(Kernel, "operation_runner", runner_with_interrupted_link)
    monkeypatch.setattr(Kernel, "control_host", control_with_original_link)
    _control_case(tmp_path, monkeypatch, "abandon")
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
