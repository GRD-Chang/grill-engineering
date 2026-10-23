"""Public delivery audits consume recorded outbox facts without sending or repair."""
from pathlib import Path
import io
import json

import pytest

from agent_run import notification_outbox as outbox
from agent_run.notification_event_facts import event_envelope
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, RunSpec
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.query_cli import main


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.name.endswith("-shm")
            and not (path.name.endswith("-wal") and path.stat().st_size == 0)}


def _run(root: Path, *, enabled: bool = True, run_id: str = "notification-query", frozen: bool = True) -> Kernel:
    defaults = UserDefaultsStore()
    defaults.configure(language="en", notifications={"enabled": enabled, "open_id": "ou_frozen",
        "profile": "frozen-profile", "app_id": "cli_frozen"})
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "requirement", Commit("base", "tree"),
                        "refs/heads/parent", "refs/heads/main")
    spec = RunSpec(run_id, "example/project", 1, "parent", "graph", (delivery,),
                   language="en", repository_root=str(root / "checkout"))
    if frozen:
        spec = freeze_creation(spec, defaults)
    return Kernel.create(root / spec.run_id / "kernel.sqlite3", spec)


def _journal(kernel: Kernel, run_id: str = "notification-query") -> None:
    state = {"run_id": run_id, "repository": "example/project", "language": "en",
             "parent": {"number": 1, "title": "Parent"}}
    with outbox.transaction(kernel.storage.path.parent, run_id) as document:
        for identity, outcome in (("queued", "pending"), ("retry", "unknown"), ("inflight", "sending"),
                                  ("delivered", "success"), ("failed", "failed"), ("cancelled", "unknown")):
            event = event_envelope(state, "run_start", identity, "Original recorded evidence", important=True)
            outbox.enqueue(document, event, 1000.0)
            item = document["pending"][-1]
            if outcome != "pending":
                item["attempts"] = 1
                item["attempt_history"].append({"number": 1, "started_at": 1001.0, "outcome": "sending"})
                if outcome == "sending":
                    item["outcome"] = "sending"
                    item["owner"] = {"pid": 23, "token": "historical-start-token"}
                else:
                    outbox.finish_attempt(item, {"outcome": outcome,
                        "reason": "Original delivery reply" if outcome != "success" else None}, 1002.0)
            if identity in {"delivered", "failed", "cancelled"}:
                if identity == "failed":
                    for number in (2, 3):
                        item["attempts"] = number
                        item["attempt_history"].append({"number": number, "started_at": 1000.0 + number * 3,
                                                        "outcome": "sending"})
                        outbox.finish_attempt(item, {"outcome": "failed", "reason": "Original delivery reply"},
                                              1001.0 + number * 3)
                    item.update(exhausted=True, exhaustion_reason="max_attempts")
                if identity == "cancelled":
                    item.update(cancelled=True, cancel_reason="obsolete_progress")
                outbox.retire(document, item)


def test_notification_boundary_reader_keeps_ticket_completion_distinct_from_the_original_final_close(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent_run_kernel import Issue, ParentIssue, Requirements
    from support.offline import GraphScenario
    from agent_run_kernel import query

    scenario = GraphScenario(tmp_path / "delivery", Requirements(
        ParentIssue(1, "Full Parent", "Original requirements", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    assert scenario.run().kind == "complete"
    assert query.read(scenario.kernel.storage.path, "status").data["status"] == "run_acceptance_pending"
    monkeypatch.setattr(query, "read_notifications", lambda *_a, **_kw: pytest.fail("Boundary facts must not read a journal"))
    with scenario.kernel.storage.transaction(write=False) as tx:
        ticket_only = query.notification_boundaries(tx, scenario.kernel.storage.path)
    assert ticket_only["is_terminal"] is False
    assert ticket_only["status"] == "run_acceptance_pending"
    assert ticket_only["closures"] == []
    for _ in range(40):
        scenario.step()
        final = scenario.kernel.source_status()["final_subject"]
        if final is not None:
            break
    else:
        raise AssertionError("Actual Ticket Completion did not admit Final")
    scenario.select(final)  # Observer focus only; every operation still uses Core's global selector.
    assert scenario.run().kind == "approval"
    with scenario.kernel.storage.transaction(write=False) as tx:
        approval = query.notification_boundaries(tx, scenario.kernel.storage.path)
    assert approval["is_terminal"] is False and approval["closures"] == []
    assert scenario.run(approve=True).kind == "complete"
    before = _snapshot(tmp_path)
    with scenario.kernel.storage.transaction(write=False) as tx:
        completed = query.notification_boundaries(tx, scenario.kernel.storage.path)
        effect = tx.one("SELECT id,receipt_id,pr_number FROM effects WHERE subject=? AND operation='close' AND status='confirmed'", (final,))
        receipt = json.loads(tx.get(effect["receipt_id"], final))
    assert completed["status"] == "completed" and completed["is_terminal"] is True
    assert completed["abandonment"] is None
    assert completed["delivery_cleanup"]["status"] == "completed"
    assert completed["closures"] == [{"subject": final, "generation": scenario.kernel.status(final)["generation"],
        "effect_id": effect["id"], "receipt_id": effect["receipt_id"], "receipt": receipt, "pr_number": effect["pr_number"]}]
    assert _snapshot(tmp_path) == before


def test_notification_boundary_reader_returns_the_original_abandon_action_until_terminal_cleanup(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent_run_kernel import query
    from agent_run_kernel.local_effects import LocalEffects
    from support.offline import Scenario

    scenario = Scenario(tmp_path / "abandon", DeliveryKind.PARENT_ONLY)
    inspections = LocalEffects(scenario.port.git).inspect_resources(scenario.kernel.generation_resources())
    assert scenario.kernel.control("abandon", "original-abandon", resource_inspections=inspections) == "pending"
    monkeypatch.setattr(query, "read_notifications", lambda *_a, **_kw: pytest.fail("Boundary facts must not read a journal"))
    with scenario.kernel.storage.transaction(write=False) as tx:
        pending = query.notification_boundaries(tx, scenario.kernel.storage.path)
    assert pending["status"] == "abandonment_pending" and pending["is_terminal"] is False
    assert pending["abandonment"] == {"action": "original-abandon", "status": "pending"}
    assert pending["actions"] == [] and pending["closures"] == []
    assert scenario.run().kind == "complete"
    before = _snapshot(tmp_path)
    with scenario.kernel.storage.transaction(write=False) as tx:
        completed = query.notification_boundaries(tx, scenario.kernel.storage.path)
    assert completed["status"] == "abandoned" and completed["is_terminal"] is True
    assert completed["abandonment"] == {"action": "original-abandon", "status": "complete"}
    assert completed["closures"] == []
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("command", ["status", "history"])
def test_public_notification_query_keeps_pending_retry_and_delivery_facts_without_mutation(
    tmp_path: Path, monkeypatch, capsys, command: str,
) -> None:
    kernel = _run(tmp_path)
    _journal(kernel)
    expected = outbox.read_notifications(kernel.storage.path.parent, "notification-query")
    before = _snapshot(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A notification query attempted sending, locking or repair")

    from agent_run_kernel.operation_runner import OperationRunner
    monkeypatch.setattr(Kernel, "operation_runner", forbidden)
    monkeypatch.setattr(Kernel, "observe_control", forbidden)
    for method in ("__init__", "run_once", "observe", "authorize_agent", "worker_settled", "state"):
        monkeypatch.setattr(OperationRunner, method, forbidden)
    monkeypatch.setattr(outbox, "transaction", forbidden)
    monkeypatch.setattr(outbox, "settle", forbidden)
    monkeypatch.setattr("agent_run_kernel.notifications.NativeNotifications", forbidden)
    monkeypatch.setattr("agent_run.notification_transport.send", forbidden)
    assert main([command, "notification-query", "--state-dir", str(kernel.storage.path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    notifications = output["notifications"]
    assert isinstance(notifications, dict)
    assert notifications["pending"] == expected["pending"]
    assert notifications["records"] == expected["records"]
    assert [item["outcome"] for item in notifications["pending"]] == ["pending", "unknown", "sending"]
    assert [item["outcome"] for item in notifications["records"]] == ["success", "failed", "unknown"]
    assert notifications["unconfirmed_count"] == 4
    assert notifications["configuration"]["open_id"] == "ou_frozen"
    assert notifications["configuration_source"] == "run_snapshot"
    assert notifications["journal_binding"] == {"status": "unknown", "reason": "not_recorded"}
    assert notifications["journal_state"] == "available"
    assert "notifications" not in output["unavailable_facts"]
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("journal", ["missing", "invalid_json", "invalid_record"])
def test_public_notification_diagnostics_do_not_hide_the_run_or_repair_the_journal(
    tmp_path: Path, capsys, journal: str,
) -> None:
    kernel = _run(tmp_path)
    if journal != "missing":
        path = outbox.notification_path(kernel.storage.path.parent, "notification-query")
        path.parent.mkdir()
        path.write_text("{broken" if journal == "invalid_json" else '{"records":[23],"pending":[]}')
    before = _snapshot(tmp_path)
    assert main(["status", "notification-query", "--state-dir", str(kernel.storage.path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    facts = document["notifications"]
    assert document["run_id"] == "notification-query"
    assert facts["journal_state"] == ("missing" if journal == "missing" else "unavailable")
    assert facts["unconfirmed_count"] is None
    if journal != "missing":
        assert facts["unavailable_reason"]
    assert "notifications" in document["unavailable_facts"]
    assert main(["status", "notification-query", "--state-dir", str(kernel.storage.path)]) == 0
    displayed = capsys.readouterr().out
    if journal == "missing":
        assert "Feishu delivery journal not recorded" in displayed
    else:
        assert "Feishu notifications unavailable:" in displayed
    assert _snapshot(tmp_path) == before


def test_public_notification_configuration_uses_the_run_snapshot_and_keeps_missing_inputs_unknown(
    tmp_path: Path, capsys,
) -> None:
    disabled = _run(tmp_path / "disabled", enabled=False)
    unknown = _run(tmp_path / "unknown", frozen=False)
    UserDefaultsStore().configure(notifications={"enabled": True, "open_id": "ou_changed"})
    before = _snapshot(tmp_path)
    for kernel, configured in ((disabled, True), (unknown, False)):
        assert main(["history", "notification-query", "--state-dir", str(kernel.storage.path), "--json"]) == 0
        document = json.loads(capsys.readouterr().out)
        facts = document["notifications"]
        if configured:
            assert facts["configuration"]["enabled"] is False
            assert facts["configuration"]["open_id"] == "ou_frozen"
        else:
            assert facts["configuration"] is None
            assert facts["configuration_source"] == "unavailable"
            assert "notifications.configuration" in document["unavailable_facts"]
        assert facts["journal_binding"]["status"] == "unknown"
        assert facts["journal_state"] == "missing"
        assert main(["history", "notification-query", "--state-dir", str(kernel.storage.path)]) == 0
        displayed = capsys.readouterr().out
        assert ("Feishu notifications disabled" if configured else "Feishu notification configuration not recorded") in displayed
    assert _snapshot(tmp_path) == before


def test_public_notification_warning_and_json_preserve_current_delivery_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "notification-public-audit"
    kernel = _run(tmp_path / "native", run_id=run_id)
    _journal(kernel, run_id)
    # Producer-owned payloads must survive querying byte for byte in JSON;
    # their dispositions below are independently fixed by the synthetic story.
    with outbox.transaction(kernel.storage.path.parent, run_id) as document:
        document["unavailable_reason"] = "Original sender failure evidence"
        expected_pending = json.loads(json.dumps(document["pending"]))
        expected_records = json.loads(json.dumps(document["records"]))
    assert [item["outcome"] for item in expected_pending] == ["pending", "unknown", "sending"]
    assert [item["attempts"] for item in expected_pending] == [0, 1, 1]
    assert [item["outcome"] for item in expected_records] == ["success", "failed", "unknown"]
    assert [item["attempts"] for item in expected_records] == [1, 3, 1]
    assert expected_records[1]["exhausted"] is True
    assert expected_records[2]["cancelled"] is True

    def forbidden(*_args, **_kwargs):
        pytest.fail("Read-only notification queries must not send")

    monkeypatch.setattr("subprocess.run", forbidden)
    from contextlib import redirect_stdout
    before = _snapshot(tmp_path)
    for command in ("status", "history"):
        for style in ("json", "plain", "rich"):
            output = io.StringIO()
            if style == "rich":
                output.isatty = lambda: True
                monkeypatch.setenv("TERM", "xterm")
                monkeypatch.delenv("NO_COLOR", raising=False)
            with redirect_stdout(output):
                assert main([command, run_id, "--state-dir", str(kernel.storage.path)] + (["--json"] if style == "json" else [])) == 0
            if style == "json":
                facts = json.loads(output.getvalue())["notifications"]
                assert facts["unconfirmed_count"] == 4
                assert facts["pending"] == expected_pending
                assert facts["records"] == expected_records
            else:
                warning = "Feishu notifications not confirmed delivered: 4;"
                assert warning in output.getvalue()
                reason = "Feishu notifications unavailable: Original sender failure evidence"
                assert reason in output.getvalue()
                assert "binding not recorded" in output.getvalue()
    assert _snapshot(tmp_path) == before
