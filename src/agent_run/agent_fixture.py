from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.artifacts import AcceptanceArtifact
from agent_run.worker_sandbox import WorkerSandboxError


class FixtureAgentBackend:
    """Deterministic worker used by cross-process black-box tests."""

    def __init__(self, path: Path) -> None:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("agent fixture root must be an object")
        self.data = value
        self.positions = {
            "developments": 0,
            "publications": 0,
            "reviews": 0,
            "run_reviews": 0,
            "run_publications": 0,
        }

    def develop(
        self, request: dict[str, Any]
    ) -> DevelopmentResult | HumanBlockerResult:
        step = self._next("developments")
        event = request.get("_invocation_event")
        notify = event if callable(event) else None
        expected = step.get("expected_thread_id")
        if expected != request.get("thread_id"):
            raise ValueError("scripted Development Thread expectation failed")
        thread_id = _string(step, "thread_id")
        if notify is not None:
            notify(
                "started",
                requested_thread_id=request.get("thread_id"),
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            notify("thread_started", reported_thread_id=thread_id, attempt_count=1)
        checkout = Path(_string(request, "checkout")).resolve()
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if request.get("head_sha") != actual_head:
            raise ValueError(
                "scripted Development Brief head does not match checkout"
            )
        expected_files = step.get("expected_files", {})
        if not isinstance(expected_files, dict):
            raise ValueError("expected_files must be an object")
        for relative, expected_content in expected_files.items():
            if not isinstance(relative, str) or not isinstance(
                expected_content, str
            ):
                raise ValueError("expected file assertions must be strings")
            target = (checkout / relative).resolve()
            if checkout not in target.parents:
                raise ValueError("expected file assertion escapes checkout")
            if (
                not target.is_file()
                or target.read_text(encoding="utf-8") != expected_content
            ):
                raise ValueError(
                    f"scripted expected file did not survive: {relative}"
                )
        absent_files = step.get("absent_files", [])
        if not isinstance(absent_files, list) or not all(
            isinstance(item, str) for item in absent_files
        ):
            raise ValueError("absent_files must contain paths")
        for relative in absent_files:
            target = (checkout / relative).resolve()
            if checkout not in target.parents:
                raise ValueError("absent file assertion escapes checkout")
            if target.exists():
                raise ValueError(
                    f"scripted expected file is still present: {relative}"
                )
        writes = step.get("write_files", {})
        if not isinstance(writes, dict):
            raise ValueError("write_files must be an object")
        for relative, content in writes.items():
            if not isinstance(relative, str) or not isinstance(content, str):
                raise ValueError("scripted file writes must be strings")
            target = (checkout / relative).resolve()
            if checkout not in target.parents:
                raise ValueError("scripted file write escapes checkout")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        configured_error = step.get("error_after_writes")
        if isinstance(configured_error, str):
            if notify is not None:
                notify("failed", attempt_count=1, error=configured_error)
            raise ValueError(configured_error)
        sandbox_error = step.get("sandbox_error_after_writes")
        if isinstance(sandbox_error, str):
            if notify is not None:
                notify("failed", attempt_count=1, error=sandbox_error)
            raise WorkerSandboxError(sandbox_error)
        blockers = _human_blockers(step)
        if blockers is not None:
            if notify is not None:
                notify("completed", reported_thread_id=thread_id, attempt_count=1)
            return HumanBlockerResult(
                thread_id=thread_id,
                human_blockers=blockers,
            )
        try:
            summary = _string(step, "summary")
        except ValueError as error:
            if notify is not None:
                notify("failed", attempt_count=1, error=str(error))
            raise
        if notify is not None:
            notify("completed", reported_thread_id=thread_id, attempt_count=1)
        return DevelopmentResult(thread_id=thread_id, summary=summary)

    def publication(
        self, request: dict[str, Any]
    ) -> dict[str, Any] | HumanBlockerResult:
        step = self._next("publications")
        expected_history = step.pop("expected_human_response_history", None)
        if expected_history is not None and expected_history != request.get(
            "human_response_history"
        ):
            raise ValueError("scripted Publication response history mismatch")
        has_expected_thread = "expected_thread_id" in step
        expected_thread = step.pop("expected_thread_id", None)
        requested_thread = request.get("thread_id")
        if has_expected_thread and expected_thread != requested_thread:
            raise ValueError(
                "agent fixture publication expected a different Thread ID"
            )
        thread_id = step.pop("thread_id", None) or requested_thread or "fixture-publication"
        event = request.get("_invocation_event")
        if callable(event):
            event(
                "started",
                requested_thread_id=requested_thread,
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            event("thread_started", reported_thread_id=thread_id, attempt_count=1)
        blockers = _human_blockers(step)
        if blockers is not None:
            if callable(event):
                event("completed", reported_thread_id=thread_id, attempt_count=1)
            return HumanBlockerResult(
                thread_id=str(thread_id),
                human_blockers=blockers,
            )
        if "invalid" in step:
            error = "scripted invalid Publication Artifact"
            if callable(event):
                event("failed", attempt_count=1, error=error)
            raise ValueError(error)
        if callable(event):
            event("completed", reported_thread_id=thread_id, attempt_count=1)
        return _publication_wire(step)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        name = (
            "run_reviews"
            if request.get("acceptance_scope") == "run"
            and isinstance(self.data.get("run_reviews"), list)
            else "reviews"
        )
        step = self._next(name)
        has_expected_thread = "expected_thread_id" in step
        expected_thread = step.pop("expected_thread_id", None)
        if has_expected_thread and expected_thread != request.get("thread_id"):
            raise ValueError("scripted Fresh Acceptance Thread expectation failed")
        expected_history = step.pop("expected_human_response_history", None)
        if expected_history is not None and expected_history != request.get(
            "human_response_history"
        ):
            raise ValueError("scripted Fresh Acceptance response history mismatch")
        event = request.get("_invocation_event")
        notify = event if callable(event) else None
        artifact = step.get("artifact")
        if not isinstance(artifact, dict):
            artifact = dict(step)
            artifact.pop("thread_id", None)
        thread_id = _string(step, "thread_id")
        if notify is not None:
            notify(
                "started",
                requested_thread_id=request.get("thread_id"),
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            notify("thread_started", reported_thread_id=thread_id, attempt_count=1)
        configured_error = step.pop("error", None)
        if isinstance(configured_error, str):
            if notify is not None:
                notify("failed", attempt_count=1, error=configured_error)
            raise ValueError(configured_error)
        if configured_error is not None:
            raise ValueError("scripted Fresh Acceptance error must be a string")
        try:
            AcceptanceArtifact.parse(artifact)
        except ValueError as error:
            if notify is not None:
                notify("failed", attempt_count=1, error=str(error))
            raise
        if notify is not None:
            notify("completed", reported_thread_id=thread_id, attempt_count=1)
        return ReviewResult(
            thread_id=thread_id,
            artifact=artifact,
        )

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        step = self._next("run_publications")
        has_expected_thread = "expected_thread_id" in step
        expected_thread = step.pop("expected_thread_id", None)
        requested_thread = request.get("thread_id")
        if has_expected_thread and expected_thread != requested_thread:
            raise ValueError(
                "agent fixture run publication expected a different Thread ID"
            )
        thread_id = step.pop("thread_id", None) or requested_thread or "fixture-run-publication"
        event = request.get("_invocation_event")
        if callable(event):
            event(
                "started",
                requested_thread_id=requested_thread,
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            event("thread_started", reported_thread_id=thread_id, attempt_count=1)
            event("completed", reported_thread_id=thread_id, attempt_count=1)
        result = _publication_wire(step)
        result["_thread_id"] = thread_id
        return result

    def _next(self, name: str) -> dict[str, Any]:
        values = self.data.get(name)
        if not isinstance(values, list):
            raise ValueError(f"agent fixture {name} must be a list")
        position = self.positions[name]
        if position >= len(values):
            raise ValueError(f"agent fixture exhausted {name}")
        self.positions[name] = position + 1
        value = values[position]
        if not isinstance(value, dict):
            raise ValueError(f"agent fixture {name} item must be an object")
        return dict(value)


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _human_blockers(data: dict[str, Any]) -> tuple[str, ...] | None:
    value = data.get("human_blockers")
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("human_blockers must contain non-empty strings")
    return tuple(value)


def _publication_wire(data: dict[str, Any]) -> dict[str, Any]:
    if "result_kind" in data or "invalid" in data:
        return data
    result = dict(data)
    result["result_kind"] = "publication"
    result["human_blockers"] = None
    return result
