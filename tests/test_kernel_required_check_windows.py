"""Real pending starts its checks window; later uncertainty preserves it."""
from support.core_operations import prepare_scripted_agent
from agent_run.core.operation_runner import OperationRunner
from dataclasses import replace
import json

import pytest

from agent_run.model import Checks, DeliveryKind
from agent_run.core.kernel import Kernel
from support.offline import Scenario
from agent_run.core.operation_contract import ReadUnavailable


@pytest.mark.parametrize("kind,initial,later,budget", [
    (DeliveryKind.TICKET, "pending", "unknown", 2700),
    (DeliveryKind.TICKET, "pending", "read_error", 2700),
    (DeliveryKind.TICKET, "unknown", "pending", 600),
    (DeliveryKind.TICKET, "unknown", "read_error", 600),
    (DeliveryKind.TICKET, "read_error", "pending", 600),
    (DeliveryKind.TICKET, "mixed_pending", "pending", 600),
    (DeliveryKind.FINAL_RUN, "unknown", "pending", 600),
])
def test_checks_keep_the_first_absolute_window_across_observation_and_restart(
    tmp_path, kind, initial, later, budget,
):
    scenario = Scenario(tmp_path / "scenario", kind, checks=("pending",))
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    observed = [initial]
    make_ports = scenario.make_ports

    def selected(binding):
        ports = make_ports(binding)
        delivery = ports.delivery

        def physical(request):
            facts = delivery(request)
            if request.check_head is not None and facts.pr is not None:
                if observed[0] == "read_error":
                    raise ReadUnavailable("github_read_failed", "The original checks read is unavailable")
                result = "fail" if observed[0] == "mixed_pending" else observed[0]
                details = (("A", "fail"), ("B", "pending")) if observed[0] == "mixed_pending" else (("required", result),)
                facts = replace(facts, checks=Checks(facts.pr.number, request.check_head, result, details))
            return facts

        ports.delivery = physical
        return ports

    scenario.make_ports = selected
    assert scenario.run().kind == "waiting"
    subject, kernel = scenario.delivery.key, scenario.kernel
    before = kernel.status(subject)
    assert before["phase"] == "checks" and before["pending_attempt"] is None
    if kind == DeliveryKind.TICKET:
        assert before["holds_integration"] is True
    assert before["purpose"] != "ci"

    def window():
        with kernel.storage.transaction(write=False) as tx:
            rows = tx.rows("SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?", (kernel.storage.run_id, subject))
            assert len(rows) == 1
            return tuple(rows[0])

    assert window() == (100.25, 100.25 + budget, 1)
    observed[0] = later
    now[0] = 105.25
    for _ in range(4):
        step = prepare_scripted_agent(kernel, scenario.runner().run_once())
        assert step.call is None
        if step.kind == "waiting":
            break
        assert step.kind == "progress"  # An original issue-link may settle first.
        assert window() == (100.25, 100.25 + budget, 1)
    else:
        raise AssertionError("Checks did not return to their original bounded wait")
    # The first actual pending observation replaces convergence with checks;
    # an existing checks window survives the reverse transition unchanged.
    expected_window = ((105.25, 2805.25, 1) if later == "pending"
                       else (100.25, 100.25 + budget, 2))
    assert window() == expected_window
    assert kernel.status(subject)["phase"] == before["phase"] == "checks"
    assert kernel.status(subject)["holds_integration"] == before["holds_integration"]
    assert kernel.status(subject)["budgets"] == before["budgets"]
    assert kernel.status(subject)["pending_attempt"] is None
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    reopened.clock = lambda: now[0]
    runner = OperationRunner(reopened, scenario.execution, selected)
    if expected_window[1] > 700.25:
        now[0] = 700.25
        step = prepare_scripted_agent(kernel, runner.run_once())
        assert (step.kind, step.reason, step.call) == ("waiting", "required_checks", None)
        expected_window = (*expected_window[:2], expected_window[2] + 1)
        assert window() == expected_window
        assert reopened.status(subject)["budgets"] == before["budgets"]
        assert reopened.status(subject)["holds_integration"] == before["holds_integration"]
    now[0] = expected_window[1]
    step = prepare_scripted_agent(kernel, runner.run_once())
    assert (step.kind, step.reason, step.call) == ("paused", "supervision_timeout", None)
    assert window() == expected_window
    # A local supervision gate safely releases the integration seat only when
    # no original formal write is pending. It does not renew the CI window.
    fresh = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    fresh.clock = lambda: now[0]
    for selected_kernel in (kernel, reopened, fresh):
        after = selected_kernel.status(subject)
        assert after["budgets"] == before["budgets"]
        assert after["phase"] == "checks" and after["gates"] == ["supervision_timeout"]
        assert after["holds_integration"] is False
        assert after["pending_attempt"] is None and after["pending_write"] is None
        with selected_kernel.storage.transaction(write=False) as tx:
            run_id = selected_kernel.storage.run_id
            assert tx.one('SELECT integration_holder FROM runs WHERE id=?', (run_id,))[0] is None
            assert not tx.rows("SELECT id FROM effects WHERE run_id=? AND subject=? AND status IN ('prepared','sent','observed')", (run_id, subject))
            row = tx.one("SELECT evidence FROM gates WHERE run_id=? AND subject=? AND code='supervision_timeout'", (run_id, subject))
            evidence = json.loads(tx.get(row[0], subject))
            assert (evidence['started_at'], evidence['deadline'], evidence['retry_count']) == expected_window
            assert tuple(tx.one('SELECT started_at,deadline,retry_count FROM waits WHERE run_id=? AND subject=?', (run_id, subject))) == expected_window
