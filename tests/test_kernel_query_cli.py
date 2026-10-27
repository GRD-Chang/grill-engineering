"""Public read-only consumers; fixtures use the real canonical Kernel store."""
from __future__ import annotations

from support.repository_fixture import create_kernel

from pathlib import Path
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.adapters.workspace import ManagedWorkspace

from agent_run.model import Blocker, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Policy, Profile, Requirements, RunSpec
from agent_run.core.kernel import Kernel
import agent_run.queries.run as query
from agent_run.cli.main import main
from support.offline import Scenario
from support.environment import durable_snapshot as _snapshot
from agent_run.core.operation_contract import DeliveryFacts
from agent_run.core.operation_runner import OperationRunner
from support.core_operations import CoreOperations, ReadOnlyPorts
from support.cli_fixtures import _default_deliveries, _fixture_source, _create


def test_missing_exact_query_is_actionable_and_does_not_create_storage(
    tmp_path: Path, capsys,
) -> None:
    missing = tmp_path / "absent-state"
    assert main(["status", "unknown-run", "--state-dir", str(missing), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "error"
    assert result["diagnostics"][0]["code"] == "run_locator_missing"
    assert not missing.exists()


def _operations(kernel: Kernel, execution, *, deliveries: tuple[Delivery, ...] | None = None) -> CoreOperations:
    deliveries = deliveries or _default_deliveries()

    class Ports(ReadOnlyPorts):
        def delivery(self, request):
            assert (request.source_ref, request.target_ref) in {(item.source_ref, item.target_ref) for item in deliveries}
            return self.facts

    first = deliveries[0]
    ports = Ports(_fixture_source(deliveries), first.source_ref, first.target_ref, DeliveryFacts(first.base, None))
    return CoreOperations(kernel, execution, lambda binding: ports)


def test_valid_large_queue_dependency_set_and_default_history_are_not_truncated(tmp_path: Path) -> None:
    base = Commit("base", "base-tree")
    independent = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "ticket-v1", base,
        f"refs/heads/ticket-{number}", "refs/heads/run") for number in range(2, 132))
    dependent = Delivery("ticket:999", DeliveryKind.TICKET, 999, "ticket-v1", base,
        "refs/heads/ticket-999", "refs/heads/run", dependencies=tuple(job.key for job in independent[:129]))
    kernel = _create(tmp_path, deliveries=(*independent, dependent))
    # Real local Stop/Resume actions produce 260 audit entries and place each
    # independent Ticket in FIFO order without synthetic SQL queue records.
    for job in independent:
        assert kernel.control("stop", "stop-" + job.key, subject=job.key) == "applied"
        assert kernel.control("resume", "resume-" + job.key, subject=job.key) == "applied"
    execution = kernel.claim_executor("queue-executor")
    assert _operations(kernel, execution, deliveries=(*independent, dependent)).once().call is not None
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "status", storage=kernel.storage).data
    assert document["scheduling"]["work_fifo"] == [job.key for job in independent[1:]]
    assert len(document["scheduling"]["work_fifo"]) == 129
    ticket = next(item for item in document["subjects"] if item["issue"] == 999)
    assert ticket["blocked_by"] == [job.issue for job in independent[:129]]
    history = query.read(kernel.storage.path, "history", storage=kernel.storage).data
    assert len(history["history"]["events"]) >= 260
    assert history["history"]["has_more"] is False
    first = query.read(kernel.storage.path, "history", limit=17, storage=kernel.storage).data
    assert len(first["history"]["events"]) == 17 and first["history"]["has_more"] is True
    assert _snapshot(tmp_path) == before


def _arguments(kernel: Kernel, command: str = "status", *extra: str) -> list[str]:
    return [command, kernel.storage.run_id, "--state-dir", str(kernel.storage.path), *extra]


def test_status_shows_ticket_progress_without_claiming_whole_delivery_complete(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path)
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--plain")) == 0
    output = capsys.readouterr().out
    assert "Completed 0 / 1 Tickets" in output
    assert "Status: Active" in output
    assert "Executor liveness is unconfirmed" in output
    assert _snapshot(tmp_path) == before


def test_parent_only_status_keeps_recorded_pass_evidence_and_hides_completed_allowances(
    tmp_path: Path, capsys,
) -> None:
    from io import StringIO
    from agent_run.cli.render import render
    from support.offline import GraphScenario

    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Original scope", ()), ()))
    # Both languages render the same canonical facts; no new Run or translated
    # Agent evidence is manufactured to exercise a different display channel.
    assert scenario.run().kind == "approval"
    document = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
    before = _snapshot(tmp_path)
    for language, progress, passed in (("zh", "无子任务，直接推进需求", "已记录验收结论: 通过"),
                                      ("en", "No Tickets; working directly on the requirement", "Recorded review: Passed")):
        for style in ("plain", "rich"):
            output = StringIO()
            render(replace(document, language=language), style=style, details=False, stream=output)
            value = output.getvalue()
            assert progress in value
            assert passed in value
            assert value.count("Known synthetic acceptance evidence") == 3
            assert "当前问题" not in value and "Current findings" not in value
    assert _snapshot(tmp_path) == before
    assert scenario.run(approve=True).kind == "complete"
    before = _snapshot(tmp_path)
    assert main(["status", scenario.spec.run_id, "--state-dir", str(scenario.kernel.storage.path), "--plain"]) == 0
    output = capsys.readouterr().out
    assert "累计用量" not in output
    assert "Known synthetic acceptance evidence" in output
    assert query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage).data["subjects"][0]["budgets"]
    history = query.read(scenario.kernel.storage.path, "history", storage=scenario.kernel.storage)
    assert len(history.data["subjects"][0]["budgets"]) == 3
    for language, label in (("zh", "累计用量:"), ("en", "Cumulative usage:")):
        for style in ("plain", "rich"):
            historical_output = StringIO()
            render(replace(history, language=language), style=style, details=True, stream=historical_output)
            assert historical_output.getvalue().count(label) == 3
            assert "W1 D 0/3" in historical_output.getvalue()
            assert "W1 D 1/10 R 1/10" in historical_output.getvalue()
    assert _snapshot(tmp_path) == before


def test_missing_progress_facts_are_not_called_parent_only(tmp_path: Path) -> None:
    from io import StringIO
    from agent_run.cli.render import render

    kernel = _create(tmp_path)
    document = query.read(kernel.storage.path, "status", storage=kernel.storage)
    # A partial display document cannot prove either delivery shape. This is a
    # renderer input, not a modification of the authoritative Run.
    output = StringIO()
    render(replace(document, data={**document.data, "subjects": []}), style="plain", details=False, stream=output)
    assert "Progress cannot be confirmed" in output.getvalue()
    assert "No Tickets" not in output.getvalue()


def _human(kernel: Kernel, message: str = "Need the original decision / 保留原文", *,
           deliveries: tuple[Delivery, ...] | None = None) -> None:
    execution = kernel.claim_executor("query-executor")
    operations = _operations(kernel, execution, deliveries=deliveries)
    call = operations.once().call
    assert call is not None
    kernel.receive(call, json.dumps({"result_kind": "human_blocker", "summary": None,
                                    "human_blockers": [message]}).encode())
    assert operations.once(agent_capacity=0).kind == "paused"


def test_latest_resume_distinguishes_application_from_the_target_start_and_ignores_sibling_progress(
    tmp_path: Path, capsys,
) -> None:
    from io import StringIO
    from agent_run.cli.render import render

    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "ticket-v1", base,
        f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    kernel = _create(tmp_path, deliveries=deliveries)
    _human(kernel, deliveries=deliveries)
    execution = kernel.claim_executor("query-executor")
    operations = _operations(kernel, execution, deliveries=deliveries)
    sibling = operations.once().call
    assert sibling is not None
    # Allocate the sibling before re-enqueueing Ticket 2, respecting the real
    # FIFO. Its physical start below still follows the target's Resume.
    assert kernel.control("resume", "resume-target-2", subject="ticket:2", message="Original answer") == "applied"
    assert kernel.dispatch_agent(sibling)
    # This is a recorded Core process-observation seam, not a real child process.
    sibling_process = {"pid": 23, "pgid": 23, "start_token": "query-sibling",
                       "host_pid": 10, "host_start_token": "query-host"}
    kernel.agent_started(sibling, sibling_process)
    kernel.observe_worker_activity(sibling, "sibling-turn", active=True, observed_at=101.0,
                                   evidence={"type": "turn.started"})
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 0
    document = json.loads(capsys.readouterr().out)
    latest = document["latest_resume"]
    assert latest["id"] == "resume-target-2" and latest["subject"] == "ticket:2"
    assert latest["status"] == "applied" and latest["resume"]["progress"] is None
    for style in ("plain", "rich"):
        stream = StringIO()
        render(query.read(kernel.storage.path, "status", storage=kernel.storage), style=style, details=False, stream=stream)
        assert "Resume: resume-target-2" in stream.getvalue()
        assert "applied; target progress not yet observed" in stream.getvalue()
    history = query.read(kernel.storage.path, "history", storage=kernel.storage).data
    assert history["latest_resume"] == latest
    assert [action for action in history["history"]["actions"] if action["kind"] == "resume"][-1] == latest
    assert _snapshot(tmp_path) == before

    kernel.agent_exited(sibling, sibling_process, 0)
    target = operations.once().call
    assert target is not None and kernel.dispatch_agent(target)
    target_process = {**sibling_process, "pid": 22, "pgid": 22, "start_token": "query-target"}
    kernel.agent_started(target, target_process)
    assert query.read(kernel.storage.path, "status", storage=kernel.storage).data["latest_resume"]["resume"]["progress"] is None
    kernel.observe_worker_activity(target, "target-turn", active=True, observed_at=102.0,
                                   evidence={"type": "turn.started"})
    before = _snapshot(tmp_path)
    latest = query.read(kernel.storage.path, "status", storage=kernel.storage).data["latest_resume"]
    assert latest["status"] == "applied"
    assert latest["resume"]["progress"]["resume_outcome"] == "started"
    assert latest["resume"]["progress"]["action_id"] == "resume-target-2"
    assert latest["resume"]["progress"]["work_subject"] == "ticket:2"
    assert latest["resume"]["progress"]["invocation_id"] == target.invocation_id
    assert latest["resume"]["progress"]["invocation_id"] != sibling.invocation_id
    assert _snapshot(tmp_path) == before
    kernel.agent_exited(target, target_process, 2, failure_kind="fatal")
    before = _snapshot(tmp_path)
    failed = query.read(kernel.storage.path, "status", storage=kernel.storage)
    assert failed.data["latest_resume"]["resume"]["progress"]["resume_outcome"] == "failed"
    assert failed.data["latest_resume"]["resume"]["progress"]["work_subject"] == "ticket:2"
    assert failed.data["latest_resume"]["resume"]["progress"]["reason_code"] == "execution_failed"
    stream = StringIO()
    render(failed, style="plain", details=False, stream=stream)
    assert "Target execution failed" in stream.getvalue()
    assert [action for action in query.read(kernel.storage.path, "history", storage=kernel.storage).data["history"]["actions"] if action["kind"] == "resume"][-1] == failed.data["latest_resume"]
    assert _snapshot(tmp_path) == before


def test_status_reports_the_original_worker_exit_wait_without_inventing_an_accepted_resume(
    tmp_path: Path,
) -> None:
    from io import StringIO
    from agent_run.errors import KernelError
    from agent_run.cli.render import render

    kernel = _create(tmp_path)
    execution = kernel.claim_executor("query-executor")
    call = _operations(kernel, execution).once().call
    assert call is not None and kernel.dispatch_agent(call)
    process = {"pid": 22, "pgid": 22, "start_token": "query-target",
               "host_pid": 10, "host_start_token": "query-host"}
    kernel.agent_started(call, process)
    assert kernel.control("stop", "stop-target-2", subject="ticket:2") == "applied"
    with pytest.raises(KernelError, match="worker_exit_unproven"):
        kernel.control("resume", "rejected-resume-2", subject="ticket:2")
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "status", storage=kernel.storage)
    assert document.data["latest_resume"] is None
    waits = document.data["resume_waits"]
    assert len(waits) == 1
    assert waits[0]["subject"] == "ticket:2" and waits[0]["issue"] == 2
    assert waits[0]["invocation"] == call.invocation_id
    assert waits[0]["reason"] == "worker_exit_unproven"
    stream = StringIO()
    render(document, style="plain", details=False, stream=stream)
    assert "Worker exit not yet confirmed: #2" in stream.getvalue()
    history = query.read(kernel.storage.path, "history", storage=kernel.storage).data
    assert [action for action in history["history"]["actions"] if action["kind"] == "resume"] == []
    assert not any(action["id"] == "rejected-resume-2" for action in history["history"]["actions"])
    assert _snapshot(tmp_path) == before
    kernel.agent_exited(call, process, -15)
    assert query.read(kernel.storage.path, "status", storage=kernel.storage).data["resume_waits"] == []


def test_current_status_and_one_history_page_do_not_load_unrelated_archived_snapshots(
    tmp_path: Path, capsys,
) -> None:
    kernel = _create(tmp_path)
    assert kernel.control("stop", "original-stop", subject="ticket:2") == "applied"
    assert kernel.control("resume", "original-resume", subject="ticket:2") == "applied"
    with kernel.storage.transaction() as tx:
        original = tx.one("SELECT * FROM history WHERE kind='control_applied' AND identity='original-resume'")
        # Resource-reader fixture: copy a valid historical status snapshot,
        # adding only legal JSON whitespace. These archived records create no
        # new Action, Gate, Worker or business authorization. Their total is
        # above a response's 32MiB budget but within the real database limit.
        snapshot = " " * (7 * 1024 * 1024) + original["status"]
        assert json.loads(snapshot) == json.loads(original["status"])
        for ordinal in range(5):
            tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at,phase,purpose,status,run_id) "
                       "VALUES (?,'boundary_update',?,NULL,?,?,?,?,?,?)",
                       (original["subject"], f"archived-boundary-{ordinal}", original["generation"],
                        original["created_at"], original["phase"], original["purpose"], snapshot, kernel.storage.run_id))
        after = tx.one("SELECT max(position) FROM history")[0]
    assert kernel.control("stop", "current-stop", subject="ticket:2") == "applied"
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["latest_resume"]["id"] == "original-resume"
    assert current["latest_resume"]["resume"]["progress"] is None
    assert current["operator_actions"][0]["reason"] == "operator_stopped"
    assert len(json.dumps(current).encode()) < 128 * 1024
    assert main(_arguments(kernel, "history", "--json", "--after", str(after), "--limit", "1")) == 0
    page = json.loads(capsys.readouterr().out)
    assert len(page["history"]["events"]) == 1 and page["history"]["events"][0]["position"] == after + 1
    assert page["history"]["has_more"] is True
    assert page["latest_resume"]["id"] == "original-resume"
    assert _snapshot(tmp_path) == before


def test_json_query_reads_actual_binding_and_raw_without_advancing_or_mutating(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _create(tmp_path)
    _human(kernel)
    before = _snapshot(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A query attempted to advance business work")

    monkeypatch.setattr(Kernel, "begin_observation", forbidden)
    monkeypatch.setattr(Kernel, "accept_requirements", forbidden)
    for method in ("__init__", "run_once", "observe", "authorize_agent", "worker_settled", "state"):
        monkeypatch.setattr(OperationRunner, method, forbidden)
    assert main(_arguments(kernel, "status", "--json")) == 0
    output = json.loads(capsys.readouterr().out)
    job = output["subjects"][0]
    assert output["focus_subject"] == "ticket:2"
    assert output["scheduling"]["concurrency"] == 3
    assert output["scheduling"]["integration_holder"] is None
    assert not {"worker", "multi_ticket", "progress", "review_budget", "agent_invocation"} & output.keys()
    assert output["run_id"] == "run-query"
    assert output["repository"] == "example/project"
    assert output["operator_actions"][0]["scope"] == "local"
    assert output["operator_actions"][0]["action"]["command"] == "resume"
    assert output["operator_actions"][0]["action"]["ticket_number"] == 2
    assert output["operator_actions"][0]["subject"] == job["subject"]
    pending = job["pending_attempt"]
    current = next(row for row in pending["invocations"] if row["id"] == pending["current_invocation"])
    assert current["model"] == "recorded-development-model"
    assert pending["result"]["human_blockers"] == ["Need the original decision / 保留原文"]
    invocation = pending["invocations"][0]
    assert invocation["model"] == "recorded-development-model" and invocation["effort"] == "high"
    assert invocation["outputs"][-1]["started_at"] is None and invocation["elapsed_seconds"] is None
    assert invocation["live_status"] == "unknown"
    used = {row["purpose"]: row["developments_used"] for row in job["budgets"] if row["current"]}
    assert used["requirements"] == 1
    assert _snapshot(tmp_path) == before


def test_local_human_boundary_does_not_present_its_sibling_as_globally_paused(tmp_path: Path) -> None:
    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "ticket-v1", base,
        f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    kernel = _create(tmp_path, deliveries=deliveries)
    _human(kernel, "Only Ticket 2 requires this decision", deliveries=deliveries)
    execution = kernel.claim_executor("query-executor")
    call = _operations(kernel, execution, deliveries=deliveries).once().call
    assert call is not None
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "status", storage=kernel.storage).data
    assert document["status"] == "active"
    assert len(document["operator_actions"]) == 1
    action = document["operator_actions"][0]
    assert action["scope"] == "local" and action["run_paused"] is False
    assert action["action"]["ticket_number"] == 2
    peer = next(job for job in document["subjects"] if job["issue"] == 3)
    assert any(lease["subject"] == peer["subject"] for lease in document["scheduling"]["active"])
    pending = peer["pending_attempt"]
    invocation = next(row for row in pending["invocations"] if row["id"] == pending["current_invocation"])
    assert invocation["id"] == call.invocation_id
    assert invocation["live_status"] == "unknown"
    assert _snapshot(tmp_path) == before


def test_stopped_run_shows_explicit_recovery_without_claiming_executor_exit(tmp_path: Path) -> None:
    kernel = _create(tmp_path)
    assert kernel.control("stop", "whole-run-stop") == "applied"
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "status", storage=kernel.storage).data
    assert document["status"] == "operator_stopped"
    action = document["operator_actions"][0]
    assert action["scope"] == "global" and action["run_paused"] is True
    assert action["action"]["command"] == "resume"
    assert action["action"]["ticket_number"] is None
    assert document["executor_control"]["live_status"] == "unknown"
    assert document["next_action"] == "agent-run resume --repo example/project -- run-query"
    assert _snapshot(tmp_path) == before


def test_parent_selector_works_in_a_repo_and_across_directories(tmp_path: Path, git_repo: Path) -> None:
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/project.git"],
                   cwd=git_repo, check=True, capture_output=True)
    workspace = ManagedWorkspace.for_repository("example/project")
    _create(workspace.state_root, repository_root=workspace.repository_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _snapshot(workspace.state_root)
    command = [sys.executable, "-m", "agent_run", "status", "--parent", "1",
               "--json"]
    for cwd, arguments in ((git_repo, []), (outside, ["--repo", "EXAMPLE/project"])):
        result = subprocess.run([*command, *arguments], cwd=cwd, text=True,
                                capture_output=True, check=False, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document["run_id"] == "run-query"
        assert document["repository"] == "example/project" and document["parent"]["number"] == 1
        assert result.stderr == ""
    missing = subprocess.run(command, cwd=outside, text=True, capture_output=True,
                             check=False, timeout=15)
    # Outside a checkout the managed registry still supplies one exact current
    # Task; cwd is not a second source of Run identity or a write permission.
    assert missing.returncode == 0, missing.stdout + missing.stderr
    selected = json.loads(missing.stdout)
    assert selected['run_id'] == 'run-query' and selected['parent']['number'] == 1
    assert selected['repository'] == 'example/project' and missing.stderr == ''
    assert _snapshot(workspace.state_root) == before


def test_parent_selector_rejects_multiple_runs_and_exact_id_remains_available(
    tmp_path: Path, capsys,
) -> None:
    one, proposed = _create(tmp_path, "run-one"), _create(tmp_path, "run-two")
    # One active Task has one complete current Run. A competing proposal reuses
    # the winner; it cannot create the duplicate old per-Run layout permitted.
    assert proposed.storage.run_id == one.storage.run_id == "run-one"
    before = _snapshot(tmp_path)
    selected = ["--repo", "EXAMPLE/project", "--parent", "1", "--state-dir", str(one.storage.path), "--json"]
    assert main(["status", *selected]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-one"
    assert main(_arguments(one, "status", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-one"
    assert main(["status", "run-two", "--state-dir", str(one.storage.path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_missing"
    assert _snapshot(tmp_path) == before


def test_runs_filters_repository_and_parent_without_creating_an_index(tmp_path: Path, capsys) -> None:
    _create(tmp_path, "selected", parent=10)
    _create(tmp_path, "another-parent", parent=20)
    _create(ManagedWorkspace.for_repository("other/project").state_root, "other-repository",
            repository="other/project", parent=10)
    before = _snapshot(tmp_path)
    assert main(["runs", "--repo", "example/project", "--parent", "10", "--data-root", str(tmp_path), "--json"]) == 0
    assert [item["run_id"] for item in json.loads(capsys.readouterr().out)["runs"]] == ["selected"]
    assert _snapshot(tmp_path) == before


def test_explicit_state_directory_selects_its_clone_without_scanning_other_roots(tmp_path: Path, capsys) -> None:
    catalog = tmp_path / "catalog"
    first = _create(catalog / "first")
    second = _create(catalog / "second")
    _human(first, "First clone decision")
    _human(second, "Explicit clone decision")
    before = _snapshot(tmp_path)
    assert main(["status", "run-query", "--data-root", str(catalog), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "unsupported_state"
    # An arbitrary catalog is not a registry. Explicit selection remains exact.
    assert main(_arguments(second, "status", "--data-root", str(catalog), "--json")) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["subjects"][0]["pending_attempt"]["result"]["human_blockers"] == ["Explicit clone decision"]
    assert _snapshot(tmp_path) == before


def test_unrepresentable_recorded_time_returns_diagnostic_without_modifying_storage(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path)
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE runs SET created_at=?", (1e300,))
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--plain")) == 2
    assert "incompatible_run_state" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("language,label", [("zh", "错误"), ("en", "Error")])
def test_parser_errors_use_personal_language_stderr_and_exit_two(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, language: str, label: str,
) -> None:
    config = tmp_path / "config"
    defaults = config / "agent-run" / "user-defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"language": language}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    before = _snapshot(tmp_path)
    assert main(["status", "--nonexistent-option"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and label in output.err and "--nonexistent-option" in output.err
    assert _snapshot(tmp_path) == before


def test_interrupted_read_returns_130_without_advancing_or_writing(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_run.cli.selection as cli_selection
    kernel = _create(tmp_path)
    before = _snapshot(tmp_path)

    def interrupted(_root, **_kwargs):
        raise KeyboardInterrupt

    from agent_run.storage.routing import RepositoryRoute
    monkeypatch.setattr(RepositoryRoute, "discover", interrupted)
    assert main(_arguments(kernel, "status", "--json")) == 130
    assert capsys.readouterr().out == ""
    assert _snapshot(tmp_path) == before


def test_missing_run_language_is_rejected_without_using_personal_defaults(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path)
    # Deliberately corrupted final-schema input, not a producer fallback.
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE runs SET language=NULL WHERE id=?", (kernel.storage.run_id,))
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["diagnostics"][0]["code"] == "recovery_materials_missing"
    assert "language" in error["diagnostics"][0]["message"]
    assert _snapshot(tmp_path) == before


def test_numeric_read_selector_is_exact_run_while_parent_is_explicit(tmp_path: Path, capsys) -> None:
    # Old _load_read_only_run calls load_exact_run for every positional string;
    # only lifecycle mutation selection treats a decimal selector as Parent.
    _create(tmp_path, "1", parent=7)
    _create(tmp_path, "parent-one", parent=1)
    before = _snapshot(tmp_path)
    assert main(["status", "1", "--data-root", str(tmp_path), "--json"]) == 0
    exact = json.loads(capsys.readouterr().out)
    assert exact["run_id"] == "1" and exact["parent"]["number"] == 7
    assert main(["status", "--repo", "example/project", "--parent", "1", "--data-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "parent-one"
    assert _snapshot(tmp_path) == before


def test_invalid_personal_defaults_do_not_override_or_block_existing_run(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _create(tmp_path / "run")
    _human(kernel)
    config = tmp_path / "config"
    defaults = config / "agent-run" / "user-defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text('{"profile":{"unknown":"invalid personal data"}}')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["focus_subject"] == "ticket:2"
    assert document["subjects"][0]["pending_attempt"]["invocations"][0]["outputs"][-1]["raw_id"] is not None
    invocation = document["subjects"][0]["pending_attempt"]["invocations"][0]
    assert invocation["model"] == "recorded-development-model"
    assert query.read(kernel.storage.path, "status", storage=kernel.storage).language == "en"
    assert _snapshot(tmp_path) == before


def test_machine_data_is_independent_of_run_language_and_history_details(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query.time, "time", lambda: 2_000_000_000.0)
    english = _create(tmp_path / "english", repository_root=tmp_path / "repository")
    chinese = _create(tmp_path / "chinese", language="zh", repository_root=tmp_path / "repository")
    documents = [query.read(item.storage.path, "status", storage=item.storage) for item in (english, chinese)]
    assert documents[0].language == "en" and documents[1].language == "zh"
    assert {key: value for key, value in documents[0].data.items() if key not in {"state_dir", "artifacts_dir"}} == {
        key: value for key, value in documents[1].data.items() if key not in {"state_dir", "artifacts_dir"}}
    from agent_run.queries.read_facts import read_header
    headers = []
    for kernel in (english, chinese):
        with kernel.storage.transaction(write=False) as tx:
            headers.append(read_header(tx, kernel.storage.path))
    # Location metadata belongs to the explicit header, not the typed public
    # RunStatus JSON. Preserve its path binding without restoring retired keys.
    assert headers[0]["state_dir"] == str(english.storage.path.parent)
    assert headers[1]["state_dir"] == str(chinese.storage.path.parent)
    assert all('state_dir' not in document.data and 'artifacts_dir' not in document.data for document in documents)
    assert main(_arguments(english, "history", "--json")) == 0
    ordinary = json.loads(capsys.readouterr().out)
    assert main(_arguments(english, "history", "--json", "--details")) == 0
    assert json.loads(capsys.readouterr().out) == ordinary


def test_history_keeps_raw_feedback_and_pages_without_losing_events(tmp_path: Path, capsys, monkeypatch) -> None:
    from agent_run.storage.run_scope import RunTransaction

    kernel = _create(tmp_path)
    _human(kernel)
    kernel.control("resume", "original-response", subject="ticket:2", message="保持原文\r\nDo exactly this.\n")
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "history", storage=kernel.storage)
    response = next(event for event in document.data["history"]["events"] if event["kind"] == "human_response")
    assert response["evidence"] == "保持原文\r\nDo exactly this.\n"
    action = next(row for row in document.data["history"]["actions"] if row["id"] == "original-response")
    assert action["status"] == "applied"
    assert action["payload"]["message"] == "保持原文\r\nDo exactly this.\n"
    assert action["receipt"]["applied"] is True
    assert action["id"] == "original-response"
    assert action["resume"]["started_at"] is None
    assert document.data["history"]["rounds"]["ticket_development"] == 1
    reads: set[str] = set()
    original_get = RunTransaction.get

    def recorded_get(tx, identity, subject):
        reads.add(identity)
        return original_get(tx, identity, subject)

    monkeypatch.setattr(RunTransaction, "get", recorded_get)
    query.read(kernel.storage.path, "status", storage=kernel.storage)
    assert response["material_id"] not in reads
    observed: list[int] = []
    after = 0
    for _ in range(20):
        reads.clear()
        page = query.read(kernel.storage.path, "history", after=after, limit=2, storage=kernel.storage).data
        assert (response["material_id"] in reads) == any(
            event["position"] == response["position"] for event in page["history"]["events"])
        observed.extend(event["position"] for event in page["history"]["events"])
        after = page["history"]["next_after"]
        if not page["history"]["has_more"]:
            break
    else:
        raise AssertionError("bounded history never reached its recorded end")
    assert observed == [event["position"] for event in document.data["history"]["events"]]
    assert len(observed) == len(set(observed))
    assert main(_arguments(kernel, "history", "--plain", "--details")) == 0
    assert "Do exactly this." in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


def test_corrupt_history_material_fails_only_its_read_scope_without_rewriting(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path)
    _human(kernel)
    kernel.control("resume", "record-response", subject="ticket:2", message="Original response")
    with kernel.storage.transaction() as tx:
        evidence = tx.one("SELECT material_id FROM history WHERE kind='human_response'")[0]
        tx.execute("UPDATE material SET digest=? WHERE id=?", ("0" * 64, evidence))
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 0
    capsys.readouterr()
    assert main(_arguments(kernel, "history", "--json")) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("terminal", [False, True])
def test_plain_and_tty_preserve_text_without_executing_terminal_controls(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, terminal: bool,
) -> None:
    kernel = _create(tmp_path)
    _human(kernel, "Need the original decision / 保留原文 \x1b[31mkeep-text\x1b[0m\u202e")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "200")
    assert main(_arguments(kernel, "status")) == 0
    output = capsys.readouterr().out
    assert "Need the original decision" in output and "保留原文" in output
    assert "--ticket 2" in output
    assert "recorded-development-model" in output and "high" in output
    assert "keep-text" in output and "\u202e" not in output
    assert "\x1b[31m" not in output


def test_public_cli_process_uses_only_native_readers_and_preserves_state(tmp_path: Path) -> None:
    kernel = _create(tmp_path)
    before = _snapshot(tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    completed = subprocess.run([sys.executable, "-m", 'agent_run', *_arguments(kernel, "status", "--json")],
                               text=True, capture_output=True, env=env, timeout=15, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["run_id"] == "run-query"
    assert completed.stderr == ""
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind,completed_phase", [(DeliveryKind.TICKET, "run_acceptance_pending"),
    (DeliveryKind.PARENT_ONLY, "completed"), (DeliveryKind.FINAL_RUN, "merged")])
def test_public_delivery_boundaries_match_observed_02_meanings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind, completed_phase: str,
) -> None:
    from agent_run.model import Issue, ParentIssue, Requirements
    from support.offline import GraphScenario

    # The former isolated Scenario had no original workspace ownership effect;
    # its three cleanup-pending failures remain in the 742c evidence. Use actual
    # source/workspace/Completion callers here. A completed Ticket in a full
    # Parent awaits Run Acceptance (02 controller.py), unlike an isolated Ticket
    # Run. This changed input is not an old/new same-input comparison.
    from support import offline
    original_create = offline.create_kernel

    def configured_create(path, spec, **kwargs):
        return original_create(path, replace(spec, language="en", repository="example/repository",
                                             repository_root=spec.repository_root), **kwargs)

    monkeypatch.setattr(offline, "create_kernel", configured_create)
    tickets = () if kind == DeliveryKind.PARENT_ONLY else (2,)
    source = Requirements(ParentIssue(1, "Full Parent", "Original requirements", tickets),
                          () if not tickets else (Issue(2, "Ticket", "Original task", "OPEN",
                                                        frozenset({"ready-for-agent"})),))
    scenario = GraphScenario(tmp_path / "scenario", source)
    scenario.spec = replace(scenario.spec, language="en", repository="example/repository")
    if kind == DeliveryKind.FINAL_RUN:
        assert scenario.run().kind == "complete"
        for _ in range(40):
            scenario.step()
            if scenario.kernel.source_status()["final_subject"] is not None:
                break
        else:
            raise AssertionError("Real Ticket Completion did not admit Final")
        scenario.select("final:1")
    if kind != DeliveryKind.TICKET:
        assert scenario.run().kind == "approval"
        before = _snapshot(tmp_path)
        approval = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage).data
        assert approval["phase"] == "ready_for_approval"
        assert approval["next_action"] == "agent-run approve --repo example/repository -- offline-run"
        focused = next(job for job in approval["subjects"] if job["subject"] == approval["focus_subject"])
        assert focused["kind"] != "ticket"
        assert any(budget["current"] and budget["purpose"] ==
                   ("integration" if focused["kind"] == "final_run" else "requirements")
                   for budget in focused["budgets"])
        assert _snapshot(tmp_path) == before
    assert scenario.run(approve=True).kind == "complete"
    before = _snapshot(tmp_path)
    document = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage).data
    assert document["phase"] == completed_phase
    focused = next((job for job in document["subjects"] if job["subject"] == document["focus_subject"]), None)
    assert focused is None or focused["kind"] != "ticket"
    assert (focused is not None and any(budget["current"] and budget["purpose"] == "integration"
            for budget in focused["budgets"])) == (kind == DeliveryKind.FINAL_RUN)
    assert document["next_action"] == ("agent-run status --repo example/repository -- offline-run"
                                       if kind == DeliveryKind.TICKET else "无")
    assert document["delivery_cleanup"]["status"] == "completed"
    assert all(subject["phase"] == "complete" for subject in document["subjects"])
    if kind != DeliveryKind.PARENT_ONLY:
        assert [record["subject"] for record in scenario.kernel.source_status()["completions"]] == ["ticket:2"]
    assert "progress" not in document
    assert document["phase"] == completed_phase
    history = query.read(scenario.kernel.storage.path, "history", storage=scenario.kernel.storage).data
    prefix = {DeliveryKind.TICKET: "ticket", DeliveryKind.PARENT_ONLY: "parent", DeliveryKind.FINAL_RUN: "run"}[kind]
    assert history["history"]["rounds"][prefix + "_publication"] == 1
    assert history["history"]["rounds"][prefix + "_review"] == 1
    assert history["history"]["events"] and history["history"]["events"][0]["created_at"]
    assert history["executor_control"]["live_status"] == "unknown"
    assert all(invocation["live_status"] != "running" for attempt in history["history"]["attempts"] for invocation in attempt["invocations"])
    assert _snapshot(tmp_path) == before
