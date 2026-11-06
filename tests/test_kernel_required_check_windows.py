"""Real pending starts its checks window; later uncertainty preserves it."""
from support.core_operations import prepare_scripted_agent
import json

import pytest

from agent_run.model import Checks, DeliveryKind
from agent_run.core.kernel import Kernel
from support.offline import Scenario
from agent_run.core.operation_contract import ReadUnavailable


def test_published_ticket_checks_keep_their_window_across_read_failure_and_restart(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("pending",))
    now = [100.25]
    kernel = scenario.kernel
    kernel.clock = lambda: now[0]
    assert scenario.run().reason == "required_checks"
    subject = scenario.delivery.key
    before = kernel.status(subject)
    assert before["phase"] == "checks" and before["holds_integration"]
    make_ports = scenario.make_ports

    def unavailable(binding):
        ports = make_ports(binding)
        delivery = ports.delivery
        def read(request):
            if request.check_head is not None:
                raise ReadUnavailable("github_read_failed", "Original checks read unavailable")
            return delivery(request)
        ports.delivery = read
        return ports

    monkeypatch.setattr(scenario, "make_ports", unavailable)
    now[0] = 105.25
    for _ in range(4):
        step = prepare_scripted_agent(kernel, scenario.runner().run_once())
        assert step.call is None
        if step.kind == "waiting":
            break
        assert step.kind == "progress"  # Original optional issue-link may settle.
    assert step.reason == "required_checks"
    scenario.kernel = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    scenario.kernel.clock = lambda: now[0]
    runner = scenario.runner()
    now[0] = 700.25
    assert runner.run_once().reason == "required_checks"
    with kernel.storage.transaction(write=False) as tx:
        expected = tuple(tx.one("SELECT started_at,deadline,retry_count FROM waits WHERE subject=?", (subject,)))
        assert expected == (100.25, 2800.25, 3)
    now[0] = expected[1]
    step = runner.run_once()
    assert (step.kind, step.reason, step.call) == ("paused", "supervision_timeout", None)
    fresh = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    after = fresh.status(subject)
    assert after["budgets"] == before["budgets"]
    assert after["phase"] == "checks" and after["gates"] == ["supervision_timeout"]
    assert after["holds_integration"] is False
    assert after["pending_attempt"] is None and after["pending_write"] is None
    with fresh.storage.transaction(write=False) as tx:
        evidence = tx.one("SELECT evidence FROM gates WHERE subject=? AND code='supervision_timeout'", (subject,))[0]
        timeout = json.loads(tx.get(evidence, subject))
        assert (timeout["started_at"], timeout["deadline"], timeout["retry_count"]) == expected
        assert tuple(tx.one("SELECT started_at,deadline,retry_count FROM waits WHERE subject=?", (subject,))) == expected


@pytest.mark.parametrize("kind,initial,later,budget", [
    ("ticket", "pending", "unknown", 2700),
    ("ticket", "unknown", "pending", 600),
    ("ticket", "unknown", "read_error", 600),
    ("ticket", "read_error", "pending", 600),
    ("ticket", "mixed_pending", "pending", 600),
    ("final_run", "unknown", "pending", 600),
])
def test_checks_observations_own_their_original_window(monkeypatch, kind, initial, later, budget):
    """Persisted publication facts feed the real window consumer, without Git.

    The test above owns publication -> observation -> physical SQLite reopen.
    These cases distinguish pending checks from an unavailable observation.
    """
    from agent_run.model import Execution
    from support.run_scoped_kernel import effect_runs, ScopedKernel
    subject, head = "parent:1", "a" * 40
    execution = Execution("A", "same-owner", 1)
    with effect_runs(monkeypatch) as (db, kernels):
        kernel, now = kernels["A"], [100.25]
        kernel.clock = lambda: now[0]
        db.execute("UPDATE subjects SET kind=?,phase='checks',pr_number=17,publication_sha=? WHERE run_id='A'", (kind, head))
        db.execute("UPDATE runs SET integration_holder=? WHERE id='A'", (subject,))
        before = list(db.execute("SELECT * FROM budgets"))

        def observe(state):
            if state == "read_error":
                with kernel.storage.transaction(write=False) as tx:
                    token = kernel._operation_token(tx, subject, False)
                return kernel._wait_for_read(token, execution, "github_read_failed", "Checks unavailable")
            result = "fail" if state == "mixed_pending" else state
            details = (("A", "fail"), ("B", "pending")) if state == "mixed_pending" else (("required", result),)
            with kernel.storage.transaction() as tx:
                return kernel._checks_wait(tx, tx.one("SELECT * FROM runs WHERE id='A'"),
                    kernel._job(tx, subject), Checks(17, head, result, details))

        def window():
            return tuple(db.execute("SELECT started_at,deadline,retry_count FROM waits WHERE run_id='A'").fetchone())

        assert observe(initial).kind == "waiting"
        assert window() == (100.25, 100.25 + budget, 1)
        now[0] = 105.25
        assert observe(later).kind == "waiting"
        expected = (105.25, 2805.25, 1) if later == "pending" else (100.25, 100.25 + budget, 2)
        assert window() == expected
        assert db.execute("SELECT integration_holder FROM runs WHERE id='A'").fetchone()[0] == subject
        kernel = ScopedKernel(db, "A")
        kernel.clock = lambda: now[0]
        with kernel.storage.transaction(write=False) as tx:
            token = kernel._operation_token(tx, subject, False)
        now[0] = expected[1]
        assert kernel._guard_automatic_step(token, execution).reason == "supervision_timeout"
        assert window() == expected
        assert list(db.execute("SELECT * FROM budgets")) == before
        assert db.execute("SELECT integration_holder FROM runs WHERE id='A'").fetchone()[0] is None
        assert db.execute("SELECT phase,current_attempt FROM subjects WHERE run_id='A'").fetchone()[:] == ("checks", None)
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
        with kernel.storage.transaction(write=False) as tx:
            evidence = tx.one("SELECT evidence FROM gates WHERE run_id='A' AND code='supervision_timeout'")[0]
            timeout = json.loads(tx.get(evidence, subject))
            assert (timeout["started_at"], timeout["deadline"], timeout["retry_count"]) == expected
