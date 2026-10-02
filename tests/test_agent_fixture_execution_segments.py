from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.credential_lifecycle import InitialCredentialUnavailable
from agent_run.worker_sandbox import WorkerSandboxError
from support.cli_payloads import final_run_agents


ROLES = ("developments", "publications", "reviews", "run_reviews", "run_publications")


def _backend(tmp_path: Path, data: dict[str, Any]) -> FixtureAgentBackend:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return FixtureAgentBackend(path)


def _invoke(backend: FixtureAgentBackend, role: str, request: dict[str, Any]) -> object:
    if role == "developments":
        checkout = Path(request["checkout"])
        request["head_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout,
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        return backend.develop(request)
    if role == "publications":
        return backend.publication(request)
    if role == "run_publications":
        return backend.run_publication(request)
    request["acceptance_scope"] = "run" if role == "run_reviews" else "ticket"
    return backend.review(request)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("fails", [False, True])
def test_fixture_execution_excludes_preparation_gate_and_closes_before_result(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    role: str, fails: bool,
) -> None:
    data: dict[str, Any] = final_run_agents()
    if fails:
        step = data[role][0]
        step["error_after_writes" if role == "developments" else "error"] = "worker failed"
    ready, release = tmp_path / "ready", tmp_path / "release"
    data["invocation_gate"] = {
        "started_file": str(ready), "release_file": str(release),
        "timeout_seconds": 30,
    }
    now = [0.0]
    events: list[tuple[str, float]] = []

    def record(kind: str, **_facts: Any) -> None:
        events.append((kind, now[0]))

    def wait(_seconds: float) -> None:
        assert ready.exists()
        assert [kind for kind, _ in events] == ["started"]
        now[0] = 20.0
        release.touch()

    monkeypatch.setattr("agent_run.agent_fixture.time.monotonic", lambda: now[0])
    monkeypatch.setattr("agent_run.agent_fixture.time.sleep", wait)
    backend = _backend(tmp_path, data)
    request = {"checkout": str(git_repo), "_invocation_event": record}
    if fails:
        with pytest.raises(ValueError, match="worker failed"):
            _invoke(backend, role, request)
    else:
        _invoke(backend, role, request)

    kinds = [kind for kind, _ in events]
    assert events[0] == ("started", 0.0)
    assert events[1] == ("execution_started", 20.0)
    assert kinds.count("execution_started") == kinds.count("execution_finished") == 1
    assert kinds[-2:] == ["execution_finished", "failed" if fails else "completed"]
    if role == "developments":
        assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "done\n"


@pytest.mark.parametrize("role", ["run_reviews", "run_publications"])
@pytest.mark.parametrize("current", [False, True])
def test_fixture_output_repair_has_distinct_execution_segments(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    role: str, current: bool,
) -> None:
    data: dict[str, Any] = final_run_agents()
    data[role] = [{"invalid": True}, *data[role]]
    # Let the successful repair continue the first attempt's fixture Thread.
    data[role][1].pop("thread_id", None)
    backend = _backend(tmp_path, data)
    events: list[str] = []

    def record(kind: str, **_facts: Any) -> None:
        events.append(kind)

    def gate(_role: str, _request: dict[str, Any], *, attempt: int = 1) -> None:
        if attempt == 2:
            assert events[-1] == "execution_finished"
            events.append("repair_gate")

    def currentness() -> bool:
        assert events[-1] == "execution_finished"
        return current

    monkeypatch.setattr(backend, "_wait_for_invocation_gate", gate)
    request = {
        "checkout": str(git_repo), "_invocation_event": record,
        "_currentness_check": currentness,
    }
    if current:
        _invoke(backend, role, request)
        assert events.count("execution_started") == events.count("execution_finished") == 2
        gate_index = events.index("repair_gate")
        assert events[gate_index - 1:gate_index + 2] == [
            "execution_finished", "repair_gate", "execution_started",
        ]
        assert events[-2:] == ["execution_finished", "completed"]
    else:
        with pytest.raises(ValueError, match="currentness changed"):
            _invoke(backend, role, request)
        assert events.count("execution_started") == events.count("execution_finished") == 1
        assert events[-2:] == ["execution_finished", "failed"]


@pytest.mark.parametrize(
    ("failure", "value", "error"),
    [
        ("keyboard_interrupt_after_writes", True, KeyboardInterrupt),
        ("sandbox_error_after_writes", "sandbox stopped", WorkerSandboxError),
        ("write_files", {"../escape": "bad"}, ValueError),
    ],
)
def test_fixture_synchronous_exception_closes_execution_segment(
    tmp_path: Path, git_repo: Path, failure: str, value: object,
    error: type[BaseException],
) -> None:
    data: dict[str, Any] = final_run_agents()
    data["developments"][0][failure] = value
    events: list[str] = []
    with pytest.raises(error):
        _invoke(_backend(tmp_path, data), "developments", {
            "checkout": str(git_repo),
            "_invocation_event": lambda kind, **_facts: events.append(kind),
        })
    assert events.count("execution_started") == events.count("execution_finished") == 1
    assert "completed" not in events


@pytest.mark.parametrize("role", ROLES)
def test_fixture_credential_failure_does_not_start_execution(
    tmp_path: Path, git_repo: Path, role: str,
) -> None:
    data: dict[str, Any] = final_run_agents()
    data["initial_credential_failures"] = ["credentials unavailable"]
    events: list[str] = []
    with pytest.raises(InitialCredentialUnavailable):
        _invoke(_backend(tmp_path, data), role, {
            "checkout": str(git_repo),
            "_invocation_event": lambda kind, **_facts: events.append(kind),
        })
    assert events == []
