from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.worker_sandbox import WorkerDeadlineExceeded

CAPACITY = "Selected model is at capacity. Please try a different model."


class Events:
    def __init__(self, recovery_state: dict[str, Any] | None = None) -> None:
        self.recovery_state = recovery_state or {}
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, **facts: Any) -> None:
        self.events.append((kind, facts))

    def recovery_allowed(self) -> bool:
        return True


def worker_fixture(
    monkeypatch: Any,
    outcomes: list[Any],
    success_payload: dict[str, Any] | None = None,
    *,
    report_thread: bool = True,
    failure_event: bool = True,
    machine_info: object = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run(
        arguments: list[str], *, on_stdout_line: Any = None, **options: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"arguments": arguments, **options})
        line = json.dumps({"type": "thread.started", "thread_id": "original-thread"})
        if not report_thread:
            line = ""
        if on_stdout_line and line:
            on_stdout_line(line)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome not in {"success", "invalid", "success-nonzero", "missing"}:
            return subprocess.CompletedProcess(
                arguments,
                -9 if outcome == "signal" else 1,
                line
                + (
                    "\n"
                    + json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": outcome,
                                **(
                                    {"codex_error_info": machine_info}
                                    if machine_info is not None
                                    else {}
                                ),
                            },
                        }
                    )
                    if failure_event
                    else ""
                ),
                "" if failure_event else outcome,
            )
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        if outcome == "missing":
            return subprocess.CompletedProcess(arguments, 0, line, "")
        output.write_text(
            json.dumps(
                {"invalid": True}
                if outcome == "invalid"
                else success_payload
                or {
                    "result_kind": "development",
                    "summary": "Completed original work",
                    "human_blockers": None,
                }
            )
        )
        return subprocess.CompletedProcess(
            arguments, 1 if outcome == "success-nonzero" else 0, line, ""
        )

    monkeypatch.setattr("agent_run.codex.run_worker_process", run)
    return calls


@pytest.mark.parametrize(
    "failures",
    [[CAPACITY, CAPACITY, "unexpected exit"], ["unexpected exit", CAPACITY, CAPACITY]],
)
def test_capacity_then_ordinary_continue_original_work(
    tmp_path: Path, monkeypatch: Any, failures: list[str]
) -> None:
    calls = worker_fixture(monkeypatch, [*failures, "success"])
    waits: list[float] = []
    monkeypatch.setattr("agent_run.codex.time.sleep", waits.append)
    events = Events()
    result = CodexCliBackend(credential_provider=lambda: "reader").develop(
        {
            "checkout": str(tmp_path),
            "_invocation_event": events,
            "_currentness_check": lambda: True,
        }
    )
    assert result.thread_id == "original-thread"
    assert len(calls) == 4
    assert sum(waits) == 60
    assert all("resume" in call["arguments"] for call in calls[1:])
    assert len({call["prompt"] for call in calls}) == 1
    assert [
        facts["recovery_kind"]
        for kind, facts in events.events
        if kind == "recovery_waiting"
    ] == ["capacity" if error == CAPACITY else "ordinary" for error in failures]


def test_ordinary_recovery_remains_spent_on_manual_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, ["unexpected exit", "success"])
    events = Events({"ordinary_recovery_used": True, "output_attempt": 1})
    with pytest.raises(CodexProcessError, match="unexpected exit"):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "thread_id": "original-thread",
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1


def test_interrupted_last_json_repair_preserves_step(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, ["unexpected exit", "success"])
    events = Events({"output_attempt": 3, "validation_error": "summary missing"})
    CodexCliBackend(credential_provider=lambda: "reader").develop(
        {
            "checkout": str(tmp_path),
            "thread_id": "original-thread",
            "_invocation_event": events,
            "_currentness_check": lambda: True,
        }
    )
    assert len(calls) == 2
    assert all("summary missing" in call["prompt"] for call in calls)
    assert all("不要修改文件" in call["prompt"] for call in calls)
    assert all(
        call["arguments"][call["arguments"].index(str(tmp_path)) - 1] == "--ro-bind"
        for call in calls
    )
    assert [
        facts["output_attempt"]
        for kind, facts in events.events
        if kind == "output_step"
    ] == [3, 3]


@pytest.mark.parametrize("outcome", ["unexpected exit", "missing"])
def test_only_one_ordinary_recovery(
    tmp_path: Path, monkeypatch: Any, outcome: str
) -> None:
    calls = worker_fixture(monkeypatch, [outcome, outcome, "success"])
    events = Events()
    with pytest.raises(CodexProcessError):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 2
    assert events.events[-1][0] == "failed"


def test_valid_artifact_on_nonzero_exit_is_adopted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, ["success-nonzero"])
    events = Events()
    result = CodexCliBackend(credential_provider=lambda: "reader").develop(
        {
            "checkout": str(tmp_path),
            "_invocation_event": events,
            "_currentness_check": lambda: True,
        }
    )
    assert result.thread_id == "original-thread"
    assert len(calls) == 1
    assert not any(kind == "recovery_waiting" for kind, _ in events.events)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_explicit_interruption_never_recovers(
    tmp_path: Path, monkeypatch: Any, error: BaseException
) -> None:
    calls = worker_fixture(monkeypatch, [error, "success"])
    with pytest.raises(type(error)):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": Events(),
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1


def test_invalid_last_json_repair_does_not_receive_more_chances(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, ["invalid", "success"])
    events = Events({"output_attempt": 3, "validation_error": "original schema error"})
    with pytest.raises(CodexProcessError):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "thread_id": "original-thread",
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1
    assert events.events[-1][1]["execution_interrupted"] is False


def test_capacity_wait_stops_when_ownership_changes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, [CAPACITY, "success"])
    events = Events()
    authorized = [True]
    events.recovery_allowed = lambda: authorized[0]  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agent_run.codex.time.sleep", lambda _seconds: authorized.__setitem__(0, False)
    )
    with pytest.raises(CodexProcessError, match="authorization changed"):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1


def test_thread_mismatch_never_recovers(tmp_path: Path, monkeypatch: Any) -> None:
    calls = worker_fixture(monkeypatch, ["success"])
    with pytest.raises(CodexProcessError, match="different Thread"):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "thread_id": "different-thread",
                "_invocation_event": Events(),
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1


def test_recovery_receives_new_full_original_deadline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    clock = [0.0]
    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: clock[0])
    calls = worker_fixture(
        monkeypatch, [WorkerDeadlineExceeded("timed out"), "success"]
    )
    events = Events()

    def record(kind: str, **facts: Any) -> None:
        events(kind, **facts)
        if kind == "recovery_waiting":
            clock[0] = 20.0

    record.recovery_state = {}  # type: ignore[attr-defined]
    record.recovery_allowed = lambda: True  # type: ignore[attr-defined]
    CodexCliBackend(credential_provider=lambda: "reader").develop(
        {
            "checkout": str(tmp_path),
            "_invocation_event": record,
            "_currentness_check": lambda: True,
            "_invocation_deadline_seconds": 10,
        }
    )
    assert [call["timeout"] for call in calls] == [10.0, 10.0]
    assert [call["deadline_at_monotonic"] for call in calls] == [10.0, 30.0]


@pytest.mark.parametrize("role", ["review", "publication", "run_publication"])
def test_readonly_roles_continue_same_work(
    tmp_path: Path, monkeypatch: Any, role: str
) -> None:
    acceptance = {
        "checks": {
            "e2e": {
                "status": "pass",
                "evidence": "操作或命令：运行候选公开流程；退出码：0；结果：候选通过端到端复验。",
                "findings": [],
            },
            "standards": {
                "status": "pass",
                "evidence": "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。",
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": "已核对的验收标准：请求中的全部验收标准；覆盖结论：候选完整覆盖。",
                "findings": [],
            },
        }
    }
    publication = {
        "result_kind": "publication",
        "commit_message": "fix(run): recover interrupted work",
        "pr_title": "fix(run): recover interrupted work",
        "pr_body_markdown": "## What Problem This Solves\n\nInterrupted work resumes.\n\n## Why This Change Was Made\n\nPreserve original work.\n\n## User Impact\n\nContinue in same Thread.\n\n## Evidence\n\nFresh Acceptance passed.",
        "human_blockers": None,
    }
    calls = worker_fixture(
        monkeypatch,
        ["unexpected exit", "success"],
        acceptance if role == "review" else publication,
    )
    backend = CodexCliBackend(credential_provider=lambda: "reader")
    result = getattr(backend, role)(
        {
            "checkout": str(tmp_path),
            "acceptance_artifact": acceptance,
            "_invocation_event": Events(),
            "_currentness_check": lambda: True,
            "_execution_binding": {
                "model": "original-model",
                "reasoning_effort": "high",
            },
        }
    )
    assert (
        result["_thread_id"] if isinstance(result, dict) else result.thread_id
    ) == "original-thread"
    assert len(calls) == 2
    assert "resume" in calls[1]["arguments"]
    assert calls[0]["prompt"] == calls[1]["prompt"]
    for call in calls:
        arguments = call["arguments"]
        assert arguments[arguments.index(str(tmp_path)) - 1] == "--ro-bind"
        assert arguments[arguments.index("--model") + 1] == "original-model"
        assert 'model_reasoning_effort="high"' in arguments


def test_capacity_text_on_stderr_does_not_grant_unlimited_recovery(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(
        monkeypatch, [CAPACITY, CAPACITY, "success"], failure_event=False
    )
    events = Events()
    with pytest.raises(CodexProcessError):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 2
    assert [
        facts["recovery_kind"]
        for kind, facts in events.events
        if kind == "recovery_waiting"
    ] == ["ordinary"]


def test_failure_without_thread_cannot_automatically_create_one(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(
        monkeypatch, ["unexpected exit", "success"], report_thread=False
    )
    with pytest.raises(CodexProcessError):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": Events(),
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1


def test_signal_exit_preserves_diagnostics_and_continues(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = worker_fixture(monkeypatch, ["signal", "success"])
    events = Events()
    CodexCliBackend(credential_provider=lambda: "reader").develop(
        {
            "checkout": str(tmp_path),
            "_invocation_event": events,
            "_currentness_check": lambda: True,
        }
    )
    assert len(calls) == 2
    failure = next(facts for kind, facts in events.events if kind == "recovery_waiting")
    assert failure["return_code"] == -9
    assert failure["signal"] == 9


@pytest.mark.parametrize(
    ("machine_info", "message", "capacity"),
    [
        ("server_overloaded", "Temporarily overloaded", True),
        ("usage_limit_reached", CAPACITY, False),
        ("future_error_type", CAPACITY, False),
        ({"unknown_error": {"detail": "bounded diagnostic"}}, CAPACITY, False),
    ],
)
def test_machine_failure_type_takes_precedence_over_message(
    tmp_path: Path, monkeypatch: Any, machine_info: object, message: str, capacity: bool
) -> None:
    calls = worker_fixture(
        monkeypatch, [message, message, "success"], machine_info=machine_info
    )
    monkeypatch.setattr("agent_run.codex.time.sleep", lambda _seconds: None)
    events = Events()
    backend = CodexCliBackend(credential_provider=lambda: "reader")
    request = {
        "checkout": str(tmp_path),
        "_invocation_event": events,
        "_currentness_check": lambda: True,
    }
    if capacity:
        backend.develop(request)
        assert len(calls) == 3
    else:
        with pytest.raises(CodexProcessError):
            backend.develop(request)
        assert len(calls) == 2
    failures = [
        facts for kind, facts in events.events if kind in {"recovery_waiting", "failed"}
    ]
    assert all(facts["machine_error"] for facts in failures)
    assert failures[0]["recovery_kind"] == ("capacity" if capacity else "ordinary")


@pytest.mark.parametrize(
    "payload", [b"x" * (1024 * 1024 + 1), b"\xff"], ids=["oversized", "invalid-utf8"]
)
def test_normal_unusable_output_does_not_restart_work(
    tmp_path: Path, monkeypatch: Any, payload: bytes
) -> None:
    calls = []

    def run(
        arguments: list[str], *, on_stdout_line: Any = None, **_options: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        line = json.dumps({"type": "thread.started", "thread_id": "original-thread"})
        if on_stdout_line:
            on_stdout_line(line)
        Path(arguments[arguments.index("--output-last-message") + 1]).write_bytes(
            payload
        )
        return subprocess.CompletedProcess(arguments, 0, line, "")

    monkeypatch.setattr("agent_run.codex.run_worker_process", run)
    events = Events()
    with pytest.raises(CodexProcessError):
        CodexCliBackend(credential_provider=lambda: "reader").develop(
            {
                "checkout": str(tmp_path),
                "_invocation_event": events,
                "_currentness_check": lambda: True,
            }
        )
    assert len(calls) == 1
    assert events.events[-1][1]["execution_interrupted"] is False
