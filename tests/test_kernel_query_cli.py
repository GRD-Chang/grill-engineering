"""Public read-only consumers; fixtures use the real canonical Kernel store."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, Profile, RunSpec, World
from agent_run_kernel import query
from agent_run_kernel.query_cli import main
from agent_run_kernel.offline import Scenario


def test_missing_exact_query_is_actionable_and_does_not_create_storage(
    tmp_path: Path, capsys,
) -> None:
    missing = tmp_path / "absent-state"
    assert main(["status", "unknown-run", "--state-dir", str(missing), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "error"
    assert result["diagnostics"][0]["code"] == "run_locator_missing"
    assert not missing.exists()


def _create(root: Path, run_id: str = "run-query", *, language: str | None = "en",
            repository: str = "example/project", parent: int = 1,
            repository_root: Path | None = None, deliveries: tuple[Delivery, ...] | None = None) -> Kernel:
    base = Commit("base", "base-tree")
    spec = RunSpec(run_id, repository, parent, "parent-v1", "graph-v1",
                   deliveries or (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                             "refs/heads/ticket", "refs/heads/run"),),
                   Policy(development_profile=Profile("recorded-development-model", "high")),
                   language=language, repository_root=str(repository_root or root / "checkout"))
    return Kernel.create(root / run_id / "kernel.sqlite3", spec)


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
    world = World("parent-v1", "graph-v1", "ticket-v1", base)
    assert kernel.advance(independent[0].key, execution, world).call is not None
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "status").data
    assert document["scheduling"]["work_fifo"] == [job.key for job in independent[1:]]
    assert len(document["scheduling"]["work_fifo"]) == 129
    ticket = next(item for item in document["multi_ticket"]["tickets"] if item["ticket_number"] == 999)
    assert ticket["blocked_by"] == [job.issue for job in independent[:129]]
    history = query.read(kernel.storage.path, "history").data
    assert len(history["timeline"]) >= 260
    assert history["history_page"]["has_more"] is False
    first = query.read(kernel.storage.path, "history", limit=17).data
    assert len(first["timeline"]) == 17 and first["history_page"]["has_more"] is True
    assert _snapshot(tmp_path) == before


def _snapshot(root: Path) -> dict[str, bytes]:
    # SQLite SHM is coordination, not durable business state. Retain all WAL
    # bytes if present; a read is not allowed to append durable frames.
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.name.endswith("-shm") and
            not (path.name.endswith("-wal") and path.stat().st_size == 0)}


def _arguments(kernel: Kernel, command: str = "status", *extra: str) -> list[str]:
    return [command, kernel.storage.path.parent.name, "--state-dir", str(kernel.storage.path), *extra]


def _human(kernel: Kernel, message: str = "Need the original decision / 保留原文") -> None:
    execution = kernel.claim_executor("query-executor")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    call = kernel.advance("ticket:2", execution, world).call
    assert call is not None
    kernel.receive(call, json.dumps({"result_kind": "human_blocker", "summary": None,
                                    "human_blockers": [message]}).encode())
    assert kernel.advance("ticket:2", execution, world).kind == "paused"


def test_json_query_reads_actual_binding_and_raw_without_advancing_or_mutating(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _create(tmp_path)
    _human(kernel)
    before = _snapshot(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A query attempted to advance business work")

    monkeypatch.setattr(Kernel, "advance", forbidden)
    assert main(_arguments(kernel, "status", "--json")) == 0
    output = json.loads(capsys.readouterr().out)
    job = output["subjects"][0]
    assert output["run_id"] == "run-query"
    assert output["repository"] == "example/project"
    assert output["operator_actions"][0]["scope"] == "local"
    assert output["operator_actions"][0]["action"]["command"] == "resume"
    assert output["operator_actions"][0]["action"]["ticket_number"] == 2
    assert output["operator_actions"][0]["trigger_invocation"]["model"] == "recorded-development-model"
    pending = job["pending_attempt"]
    assert pending["result"]["human_blockers"] == ["Need the original decision / 保留原文"]
    invocation = pending["invocations"][0]
    assert invocation["model"] == "recorded-development-model" and invocation["effort"] == "high"
    assert invocation["started_at"] is None and invocation["elapsed_seconds"] is None
    assert invocation["live_status"] == "unknown"
    used = {row["purpose"]: row["developments_used"] for row in job["budgets"] if row["current"]}
    assert used["requirements"] == 1
    assert _snapshot(tmp_path) == before


def test_parent_selector_rejects_multiple_runs_and_exact_id_remains_available(
    tmp_path: Path, capsys,
) -> None:
    one, two = _create(tmp_path, "run-one"), _create(tmp_path, "run-two")
    before = _snapshot(tmp_path)
    selected = ["--repo", "EXAMPLE/project", "--parent", "1", "--data-root", str(tmp_path), "--json"]
    assert main(["status", *selected]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["diagnostics"][0]["code"] == "run_locator_conflict"
    assert {item["run_id"] for item in error["candidates"]} == {"run-one", "run-two"}
    assert main(_arguments(one, "status", "--data-root", str(tmp_path), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-one"
    assert main(_arguments(two, "status", "--data-root", str(tmp_path), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-two"
    assert _snapshot(tmp_path) == before


def test_runs_filters_repository_and_parent_without_creating_an_index(tmp_path: Path, capsys) -> None:
    _create(tmp_path, "selected", parent=10)
    _create(tmp_path, "another-parent", parent=20)
    _create(tmp_path, "other-repository", repository="other/project", parent=10)
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
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "run_locator_conflict"
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
    from agent_run_kernel import query_cli
    kernel = _create(tmp_path)
    before = _snapshot(tmp_path)

    def interrupted(_root):
        raise KeyboardInterrupt

    monkeypatch.setattr(query_cli, "discover", interrupted)
    assert main(_arguments(kernel, "status", "--json")) == 130
    assert capsys.readouterr().out == ""
    assert _snapshot(tmp_path) == before


def test_missing_run_language_is_rejected_without_using_personal_defaults(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path, language=None)
    before = _snapshot(tmp_path)
    assert main(_arguments(kernel, "status", "--json")) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["diagnostics"][0]["code"] == "recovery_materials_missing"
    assert "language" in error["diagnostics"][0]["message"]
    assert _snapshot(tmp_path) == before


def test_machine_data_is_independent_of_run_language_and_history_details(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query.time, "time", lambda: 2_000_000_000.0)
    english = _create(tmp_path / "english", repository_root=tmp_path / "repository")
    chinese = _create(tmp_path / "chinese", language="zh", repository_root=tmp_path / "repository")
    documents = [query.read(item.storage.path, "status") for item in (english, chinese)]
    assert documents[0].language == "en" and documents[1].language == "zh"
    assert documents[0].data == documents[1].data
    assert main(_arguments(english, "history", "--json")) == 0
    ordinary = json.loads(capsys.readouterr().out)
    assert main(_arguments(english, "history", "--json", "--details")) == 0
    assert json.loads(capsys.readouterr().out) == ordinary


def test_history_keeps_raw_feedback_and_pages_without_losing_events(tmp_path: Path, capsys) -> None:
    kernel = _create(tmp_path)
    _human(kernel)
    kernel.control("resume", "original-response", subject="ticket:2", message="保持原文\r\nDo exactly this.\n")
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, "history")
    response = next(event for event in document.data["timeline"] if event["kind"] == "human_response")
    assert response["evidence"] == "保持原文\r\nDo exactly this.\n"
    action = next(row for row in document.data["actions"] if row["id"] == "original-response")
    assert action["status"] == "applied"
    assert action["payload"]["message"] == "保持原文\r\nDo exactly this.\n"
    assert action["receipt"]["applied"] is True
    assert document.data["resume_audit"][0]["action_id"] == "original-response"
    assert document.data["agent_resumes"][0]["started_at"] is None
    assert document.data["summary"]["rounds"]["ticket_development"] == 1
    observed: list[int] = []
    after = 0
    for _ in range(20):
        page = query.read(kernel.storage.path, "history", after=after, limit=2).data
        observed.extend(event["position"] for event in page["timeline"])
        after = page["history_page"]["next_after"]
        if not page["history_page"]["has_more"]:
            break
    else:
        raise AssertionError("bounded history never reached its recorded end")
    assert observed == [event["position"] for event in document.data["timeline"]]
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
    completed = subprocess.run([sys.executable, "-m", "agent_run_kernel.query_cli", *_arguments(kernel, "status", "--json")],
                               text=True, capture_output=True, env=env, timeout=15, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["run_id"] == "run-query"
    assert completed.stderr == ""
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind,completed_phase", [(DeliveryKind.TICKET, "ticket_completed"),
    (DeliveryKind.PARENT_ONLY, "completed"), (DeliveryKind.FINAL_RUN, "merged")])
def test_public_delivery_boundaries_match_observed_02_meanings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind, completed_phase: str,
) -> None:
    # The old 013 actual observations retain all public status/history outputs.
    # These candidate regression oracles preserve their concrete boundary
    # meanings; a separate same-input capture must establish full equivalence.
    original_create = Kernel.create

    def configured_create(path, spec, **kwargs):
        return original_create(path, replace(spec, language="en", repository_root=str(tmp_path.resolve())), **kwargs)

    monkeypatch.setattr(Kernel, "create", staticmethod(configured_create))
    scenario = Scenario(tmp_path / "scenario", kind)
    if kind != DeliveryKind.TICKET:
        assert scenario.run().kind == "approval"
        before = _snapshot(tmp_path)
        approval = query.read(scenario.kernel.storage.path, "status").data
        assert approval["phase"] == "ready_for_approval"
        assert approval["next_action"] == "agent-run approve 1 --repo example/repository"
        assert approval["active_ticket"] is None
        assert approval["review_budget"] is not None
        assert _snapshot(tmp_path) == before
    assert scenario.run(approve=True).kind == "complete"
    before = _snapshot(tmp_path)
    document = query.read(scenario.kernel.storage.path, "status").data
    assert document["phase"] == completed_phase
    assert document["active_ticket"] is None
    assert (document["review_budget"] is not None) == (kind == DeliveryKind.FINAL_RUN)
    assert document["next_action"] == ("agent-run run 1" if kind == DeliveryKind.TICKET else "无")
    assert document["progress"]["status"] == document["status"]
    assert document["progress"]["phase"] == completed_phase
    history = query.read(scenario.kernel.storage.path, "history").data
    prefix = {DeliveryKind.TICKET: "ticket", DeliveryKind.PARENT_ONLY: "parent", DeliveryKind.FINAL_RUN: "run"}[kind]
    assert history["summary"]["rounds"][prefix + "_publication"] == 1
    assert history["summary"]["rounds"][prefix + "_review"] == 1
    assert history["events"] and history["events"][0]["at"]
    assert history["scheduler"]["activity"] == "unknown"
    assert history["execution_activity"] != "running"
    assert _snapshot(tmp_path) == before
