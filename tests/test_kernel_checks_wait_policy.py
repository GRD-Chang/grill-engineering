"""User-configured CI waits retain one frozen, absolute Core deadline."""
import json
from dataclasses import replace

from agent_run.cli.main import main
from agent_run.config.creation import freeze_creation
from agent_run.config.defaults import UserDefaultsError, UserDefaultsStore
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec


import pytest

from agent_run.contracts.policy_values import checkpoint_policy, resolve_delivery_policy
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.errors import DeliveryPolicyError, KernelError
from agent_run.model import Issue, ParentIssue, Policy, Requirements, wire_json
from agent_run.queries.run import read
from support.offline import GraphScenario, Scenario
from support.core_operations import CoreOperations


def test_public_checks_wait_setting_freezes_creation_and_explicit_precedence(tmp_path, capsys):
    defaults = UserDefaultsStore()
    assert main(["settings", "configure", "--required-checks-wait-seconds", "2h", "--json"]) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["policy"]["required_checks_wait_seconds"] == 7200
    base = Commit("base", "tree")
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "v1", base,
                        "refs/heads/source", "refs/heads/main")
    spec = RunSpec("run", "example/project", 1, "parent", "graph", (delivery,))
    frozen = freeze_creation(spec, defaults)
    assert frozen.policy.required_checks_wait_seconds == 7200
    defaults.configure(policy={"required_checks_wait_seconds": "3h"})
    assert frozen.policy.required_checks_wait_seconds == 7200
    overridden = freeze_creation(spec, defaults, policy_overrides={"required_checks_wait_seconds": "90m"})
    assert overridden.policy.required_checks_wait_seconds == 5400
    assert main(["settings", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["required_checks_wait_seconds"] == 10800


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), 604801, "0s", "8d", "forever"])
def test_invalid_checks_wait_does_not_replace_personal_defaults(value):
    defaults = UserDefaultsStore()
    defaults.configure(policy={"required_checks_wait_seconds": "2h"})
    original = defaults.path.read_bytes()
    with pytest.raises(UserDefaultsError):
        defaults.configure(policy={"required_checks_wait_seconds": value})
    assert defaults.path.read_bytes() == original
    with pytest.raises(DeliveryPolicyError):
        Policy(required_checks_wait_seconds=value)


def test_duration_boundary_and_explicit_checkpoint_preserve_other_allowances():
    frozen = Policy(required_checks_wait_seconds=0.5)
    assert frozen.snapshot()["required_checks_wait_seconds"] == 0.5
    maximum = resolve_delivery_policy(command_overrides={"required_checks_wait_seconds": "7d"})
    assert maximum.required_checks_wait_seconds == 604800
    with pytest.raises(DeliveryPolicyError):
        Policy(required_checks_wait_seconds="2h")
    changed = checkpoint_policy(frozen, {"required_checks_wait_seconds": "2h"})
    assert changed.required_checks_wait_seconds == 7200
    assert changed.invocation_deadlines == frozen.invocation_deadlines
    assert {k: v for k, v in changed.snapshot().items() if k != "required_checks_wait_seconds"} == {
        k: v for k, v in frozen.snapshot().items() if k != "required_checks_wait_seconds"}


@pytest.mark.parametrize("language", ["en", "zh"])
def test_cli_help_and_human_settings_expose_same_wait_field(language, capsys):
    assert main(["settings", "configure", "--language", language]) == 0
    capsys.readouterr()
    assert main(["run", "--help"]) == 0
    help_output = capsys.readouterr().out
    assert "--required-checks-wait-seconds" in help_output and "7d" in help_output
    assert main(["settings", "show"]) == 0
    assert "2700" in capsys.readouterr().out


def test_long_checks_window_survives_unknown_restart_and_late_success(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("pending",),
                        policy=Policy(required_checks_wait_seconds=7200))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.clock = lambda: 100.25
    assert scenario.run().reason == "required_checks"
    original = read(kernel.storage.path, "status").data["supervision_windows"][0]
    assert (original["started_at"], original["deadline"]) == (100.25, 7300.25)
    before = kernel.status(subject)
    role_count = len(scenario.role_calls)
    writes = scenario.port.writes()
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    ports = operations.runner.ports
    source = ports.source

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "synthetic outage")

    monkeypatch.setattr(ports, "source", unavailable)
    UserDefaultsStore().configure(policy={"required_checks_wait_seconds": "3h"})
    kernel.clock = lambda: 2801.25  # Beyond the old fixed 45 minutes.
    assert operations.once().kind == "waiting"
    retained = read(kernel.storage.path, "status").data["supervision_windows"][0]
    assert (retained["scope_id"], retained["deadline"]) == (original["scope_id"], 7300.25)
    reopened = Kernel(kernel.storage.path)
    reopened.clock = lambda: 7300.25
    operations = CoreOperations(reopened, scenario.execution, lambda binding: ports)
    assert operations.once().reason == "supervision_timeout"
    timeout = read(kernel.storage.path, "status").data["supervision_windows"][0]["timeout"]
    assert timeout["budget_seconds"] == 7200
    assert reopened.status(subject)["budgets"] == before["budgets"]
    assert scenario.port.writes() == writes

    # Missing field in an old frozen Policy means the former default, never the
    # new personal settings. Decode a wire value; do not alter any stored data.
    with kernel.storage.transaction(write=False) as tx:
        old_policy = json.loads(tx.one("SELECT policy FROM runs")[0])
    del old_policy["required_checks_wait_seconds"]
    assert reopened._decode_policy(wire_json(old_policy)).required_checks_wait_seconds == 2700
    with pytest.raises(KernelError, match="policy_snapshot_invalid"):
        reopened._decode_policy(wire_json({**old_policy, "unknown": 1}))

    # Plain explicit Resume reads the successful original head; it neither
    # starts another Agent nor republishes the already completed PR.
    with pytest.raises(KernelError, match="policy_override_boundary"):
        reopened.control("resume", "no-timeout-policy-change", subject=subject,
                         policy_overrides={"required_checks_wait_seconds": "3h", "ticket_ci_development_limit": 9})
    assert reopened.status(subject)["gates"] == ["supervision_timeout"]
    reopened.control("resume", "ci-finished-later", subject=subject)
    scenario.kernel = reopened
    monkeypatch.setattr(ports, "source", source)
    scenario.checks[:] = ["pass"]
    assert scenario.run().kind == "complete"
    assert len(scenario.role_calls) == role_count
    assert reopened.status(subject)["budgets"] == before["budgets"]
    assert sum(w["operation"] == "pr" for w in scenario.port.writes()) == sum(w["operation"] == "pr" for w in writes)


def test_sibling_progress_does_not_renew_wait_and_stop_cannot_start_repair(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Work", (2, 3)), tuple(
        Issue(n, f"Ticket {n}", "Work", "OPEN", frozenset({"ready-for-agent"})) for n in (2, 3)))
    scenario = GraphScenario(tmp_path / "graph", source,
                             policy=Policy(required_checks_wait_seconds=120, concurrency=2))
    scenario.checks[:] = ["pending"]
    scenario.kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    first = read(scenario.kernel.storage.path, "status").data["supervision_windows"][0]
    scenario.kernel.clock = lambda: 110
    for _ in range(60):
        scenario.step()
        if scenario.kernel.status("ticket:3")["phase"] == "integration":
            break
    else:
        raise AssertionError("Sibling did not finish independent development and review")
    windows = read(scenario.kernel.storage.path, "status").data["supervision_windows"]
    assert len(windows) == 1  # The first Ticket correctly owns the single integration seat.
    assert (windows[0]["scope_id"], windows[0]["deadline"]) == (first["scope_id"], 220)
    assert scenario.kernel.status("ticket:2")["holds_integration"]
    assert not scenario.kernel.status("ticket:3")["holds_integration"]

    # Finish the first PR, then the second PR gets its own full interval.
    make_ports = scenario.make_ports
    def selected(binding):
        ports = make_ports(binding)
        delivery = ports.delivery
        def physical(request):
            facts = delivery(request)
            if request.source_ref == "refs/heads/ticket-3" and facts.checks is not None:
                return replace(facts, checks=replace(facts.checks, result="pending", details=(("required", "pending"),)))
            return facts
        ports.delivery = physical
        return ports
    scenario.make_ports = selected
    scenario.checks[:] = ["pass"]
    scenario.kernel.clock = lambda: 120
    for _ in range(100):
        scenario.step()
        windows = read(scenario.kernel.storage.path, "status").data["supervision_windows"]
        if any(w["subject"] == "ticket:3" and w["kind"] == "required_checks" for w in windows):
            break
    else:
        raise AssertionError("Second PR did not start its own pending checks window")
    second, = [w for w in windows if w["subject"] == "ticket:3"]
    assert (second["started_at"], second["deadline"]) == (120, 240)
    assert second["scope_id"] != first["scope_id"]
    roles = len(scenario.role_calls)
    writes = scenario.port.writes()
    scenario.kernel.control("stop", "stop-pending-ci")
    scenario.checks[:] = ["fail"]
    with pytest.raises(KernelError, match="executor_fenced"):
        scenario.step()
    assert len(scenario.role_calls) == roles and scenario.port.writes() == writes


def test_budget_checkpoint_explicit_wait_override_only_changes_future_windows(tmp_path):
    scenario = Scenario(tmp_path / "checkpoint", DeliveryKind.PARENT_ONLY, reviews=("fail",),
                        checks=("pending",), policy=Policy(parent_rounds=1, required_checks_wait_seconds=60))
    kernel, subject = scenario.kernel, scenario.delivery.key
    paused = scenario.run()
    assert (paused.kind, paused.reason) == ("paused", "development_budget")
    assert kernel.status(subject)["gates"] == ["review_budget_exhausted"]
    with kernel.storage.transaction(write=False) as tx:
        invocations = [dict(r) for r in tx.all("SELECT * FROM invocations ORDER BY id")]
        frozen = tx.one("SELECT policy FROM runs")[0]
    UserDefaultsStore().configure(policy={"required_checks_wait_seconds": "3h"})
    overrides = {"required_checks_wait_seconds": "2h"}
    assert kernel.control("resume", "explicit-checkpoint", subject=subject, policy_overrides=overrides) == "applied"
    assert kernel.control("resume", "explicit-checkpoint", subject=subject, policy_overrides=overrides) == "applied"
    with kernel.storage.transaction(write=False) as tx:
        assert [dict(r) for r in tx.all("SELECT * FROM invocations ORDER BY id")] == invocations
        assert tx.one("SELECT policy FROM runs")[0] == frozen
    scenario.reviews[:] = ["pass"]
    kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    window, = read(kernel.storage.path, "status").data["supervision_windows"]
    assert (window["started_at"], window["deadline"]) == (100, 7300)
    assert kernel.status(subject)["budgets"]["requirements"]["window"] == 2


def test_expired_checks_resume_changes_only_future_wait_and_serializes_actions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from support.environment import durable_snapshot as _snapshot

    scenario = Scenario(tmp_path / "resume-ci", DeliveryKind.TICKET, checks=("pending",),
                        policy=Policy(required_checks_wait_seconds=3))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    before = _snapshot(tmp_path)
    with pytest.raises(KernelError, match="policy_override_boundary"):
        kernel.control("resume", "not-expired", subject=subject,
                       policy_overrides={"required_checks_wait_seconds": "2h"})
    assert _snapshot(tmp_path) == before
    kernel.clock = lambda: 103
    assert scenario.step().reason == "supervision_timeout"
    budget = kernel.status(subject)["budgets"]
    roles = len(scenario.role_calls)
    publications = sum(w["operation"] == "pr" for w in scenario.port.writes())
    with kernel.storage.transaction(write=False) as tx:
        gate = tx.one("SELECT * FROM gates WHERE subject=? AND code='supervision_timeout'", (subject,))
        original_timeout_id = gate["evidence"]
        original_timeout = tx.get(original_timeout_id, subject)
        old_scope = json.loads(tx.get(json.loads(original_timeout)["scope_id"], subject))
        frozen_run = tx.one("SELECT policy FROM runs")[0]
    for invalid in ({"required_checks_wait_seconds": 0}, {"required_checks_wait_seconds": None},
                    {"required_checks_wait_seconds": "2h", "ticket_ci_development_limit": 9}):
        before = _snapshot(tmp_path)
        with pytest.raises((DeliveryPolicyError, KernelError)):
            kernel.control("resume", "invalid-timeout-change", subject=subject, policy_overrides=invalid)
        assert _snapshot(tmp_path) == before

    barrier = Barrier(2)
    overrides = {"required_checks_wait_seconds": "2h"}
    def resume(action):
        barrier.wait(timeout=5)
        try:
            return action, kernel.control("resume", action, subject=subject, policy_overrides=overrides)
        except KernelError as error:
            return action, error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume, ["new-ci-window-a", "new-ci-window-b"]))
    assert sorted(result for _, result in results) == ["applied", "policy_override_boundary"]
    winner = next(action for action, result in results if result == "applied")
    before = _snapshot(tmp_path)
    assert kernel.control("resume", winner, subject=subject, policy_overrides=overrides) == "applied"
    assert _snapshot(tmp_path) == before
    with pytest.raises(KernelError, match="action_identity_changed"):
        kernel.control("resume", winner, subject=subject,
                       policy_overrides={"required_checks_wait_seconds": "3h"})
    assert _snapshot(tmp_path) == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.get(original_timeout_id, subject) == original_timeout
        assert tx.one("SELECT policy FROM runs")[0] == frozen_run
        assert not tx.rows("SELECT * FROM waits WHERE subject=?", (subject,))
        assert len(tx.rows("SELECT * FROM actions WHERE id IN ('new-ci-window-a','new-ci-window-b')")) == 1
    kernel.clock = lambda: 200
    assert scenario.run().reason == "required_checks"
    window, = read(kernel.storage.path, "status").data["supervision_windows"]
    assert (window["started_at"], window["deadline"]) == (200, 7400)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.get(original_timeout_id, subject) == original_timeout
        new_scope = json.loads(tx.get(window["scope_id"], subject))
    assert new_scope == old_scope
    assert kernel.status(subject)["budgets"] == budget
    assert len(scenario.role_calls) == roles
    assert sum(w["operation"] == "pr" for w in scenario.port.writes()) == publications


def test_other_supervision_timeout_cannot_change_checks_policy(tmp_path, monkeypatch):
    from support.core_operations import delivery_run, source_operations, unavailable
    from support.environment import durable_snapshot as _snapshot

    kernel, source = delivery_run(tmp_path / "run.sqlite3")
    operations = source_operations(kernel, source, kernel.claim_executor("read-outage"))
    monkeypatch.setattr(operations.runner.ports, "source", unavailable)
    kernel.clock = lambda: 100
    assert operations.once().reason == "github_convergence"
    kernel.clock = lambda: 700
    expired = operations.once()
    assert expired.reason == "supervision_timeout"
    before = _snapshot(tmp_path)
    with pytest.raises(KernelError, match="policy_override_boundary"):
        kernel.control("resume", "unrelated-source-timeout", subject=expired.subject,
                       policy_overrides={"required_checks_wait_seconds": "2h"})
    assert _snapshot(tmp_path) == before
