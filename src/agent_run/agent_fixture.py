from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.artifacts import (
    AcceptanceArtifact,
    PublicationArtifact,
    parse_publication_wire_result,
)
from agent_run.worker_sandbox import WorkerSandboxError
from agent_run.worker_credentials import InitialCredentialUnavailable


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
        failures = value.get("initial_credential_failures", [])
        if not isinstance(failures, list) or not all(
            _is_initial_credential_failure(message) for message in failures
        ):
            raise ValueError("initial_credential_failures must contain safe failures")
        self.initial_credential_failures = list(failures)
        by_role = value.get("initial_credential_failures_by_role", {})
        if not isinstance(by_role, dict) or not all(
            isinstance(role, str)
            and isinstance(messages, list)
            and all(_is_initial_credential_failure(message) for message in messages)
            for role, messages in by_role.items()
        ):
            raise ValueError(
                "initial_credential_failures_by_role must map roles to safe failures"
            )
        self.initial_credential_failures_by_role = {
            role: list(messages) for role, messages in by_role.items()
        }

    def develop(
        self, request: dict[str, Any]
    ) -> DevelopmentResult | HumanBlockerResult:
        self._maybe_fail_initial_credential("developments")
        step = self._next("developments")
        event = request.get("_invocation_event")
        notify = event if callable(event) else None
        expected = step.get("expected_thread_id")
        if expected != request.get("thread_id"):
            raise ValueError("scripted Development Thread expectation failed")
        if notify is not None:
            notify(
                "started",
                requested_thread_id=request.get("thread_id"),
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
        self._wait_for_invocation_gate("developments", request)
        no_thread = step.get("no_thread", False)
        if not isinstance(no_thread, bool):
            raise ValueError("scripted Development no_thread must be a boolean")
        if no_thread:
            error = "scripted Development did not report a Thread ID"
            if notify is not None:
                notify("failed", attempt_count=1, error=error)
            raise ValueError(error)
        try:
            thread_id = _string(step, "thread_id")
        except ValueError as error:
            if notify is not None:
                notify("failed", attempt_count=1, error=str(error))
            raise
        if notify is not None:
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
        deletions = step.get("delete_files", [])
        if not isinstance(deletions, list) or not all(
            isinstance(item, str) for item in deletions
        ):
            raise ValueError("delete_files must contain paths")
        for relative in deletions:
            target = (checkout / relative).resolve()
            if checkout not in target.parents:
                raise ValueError("scripted file deletion escapes checkout")
            if target.exists():
                if not target.is_file():
                    raise ValueError("scripted file deletion requires a file")
                target.unlink()
        configured_error = step.get("error_after_writes")
        if isinstance(configured_error, str):
            if notify is not None:
                notify("failed", attempt_count=1, error=configured_error)
            raise ValueError(configured_error)
        if step.get("keyboard_interrupt_after_writes") is True:
            raise KeyboardInterrupt
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
        self._maybe_fail_initial_credential("publications", request)
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
        configured_error = step.pop("error", None)
        no_thread = step.pop("no_thread", False)
        if not isinstance(no_thread, bool):
            raise ValueError("scripted Publication no_thread must be a boolean")
        thread_id = step.pop("thread_id", None) or requested_thread or "fixture-publication"
        event = request.get("_invocation_event")
        if callable(event):
            event(
                "started",
                requested_thread_id=requested_thread,
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            self._wait_for_invocation_gate("publications", request)
            if no_thread:
                error = "scripted Publication did not report a Thread ID"
                event("failed", attempt_count=1, error=error)
                raise ValueError(error)
            event("thread_started", reported_thread_id=thread_id, attempt_count=1)
        if isinstance(configured_error, str):
            if callable(event):
                event("failed", attempt_count=1, error=configured_error)
            raise ValueError(configured_error)
        if configured_error is not None:
            raise ValueError("scripted Publication error must be a string")
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
        self._maybe_fail_initial_credential(name, request)
        if request.get("acceptance_scope") != "run":
            return self._legacy_review(name, request)
        artifact, thread_id = self._output_attempts(
            name,
            request,
            default_thread="fixture-reviewer",
            decode=_review_artifact,
            validate=AcceptanceArtifact.parse,
        )
        return ReviewResult(thread_id=thread_id, artifact=artifact)

    def _legacy_review(
        self, name: str, request: dict[str, Any]
    ) -> ReviewResult:
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
        if notify is not None:
            notify(
                "started",
                requested_thread_id=request.get("thread_id"),
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
        self._wait_for_invocation_gate(name, request)
        no_thread = step.get("no_thread", False)
        if not isinstance(no_thread, bool):
            raise ValueError("scripted Fresh Acceptance no_thread must be a boolean")
        if no_thread:
            error = "scripted Fresh Acceptance did not report a Thread ID"
            if notify is not None:
                notify("failed", attempt_count=1, error=error)
            raise ValueError(error)
        try:
            thread_id = _string(step, "thread_id")
        except ValueError as error:
            if notify is not None:
                notify("failed", attempt_count=1, error=str(error))
            raise
        if notify is not None:
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
        return ReviewResult(thread_id=thread_id, artifact=artifact)

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail_initial_credential("run_publications")
        result, thread_id = self._output_attempts(
            "run_publications",
            request,
            default_thread="fixture-run-publication",
            decode=_publication_result,
            validate=lambda value: _validate_publication(
                value, delivery_run=str(request.get("run_id", "fixture"))
            ),
        )
        result["_thread_id"] = thread_id
        return result

    def _output_attempts(
        self,
        name: str,
        request: dict[str, Any],
        *,
        default_thread: str,
        decode: Callable[[dict[str, Any]], dict[str, Any]],
        validate: Callable[[dict[str, Any]], object],
    ) -> tuple[dict[str, Any], str]:
        event = request.get("_invocation_event")
        notify = event if callable(event) else lambda _kind, **_facts: None
        current_thread = request.get("thread_id")
        if current_thread is not None and not isinstance(current_thread, str):
            raise ValueError("agent fixture request thread_id must be a string")
        invocation_started = False

        def start_invocation() -> None:
            nonlocal invocation_started
            if invocation_started:
                return
            notify(
                "started",
                requested_thread_id=request.get("thread_id"),
                attempt_count=0,
                invocation_mode=request.get("_invocation_mode"),
            )
            invocation_started = True

        def report_thread(thread_id: str, attempt: int) -> None:
            start_invocation()
            notify(
                "thread_started",
                reported_thread_id=thread_id,
                attempt_count=attempt,
            )

        start_invocation()
        self._wait_for_invocation_gate(name, request, attempt=1)
        currentness = request.get("_currentness_check")
        for attempt in range(1, 4):
            if attempt > 1 and callable(currentness) and not currentness():
                message = "Fixture currentness changed before Output Repair"
                start_invocation()
                notify("failed", attempt_count=attempt - 1, error=message)
                raise ValueError(message)
            if attempt > 1:
                self._wait_for_invocation_gate(name, request, attempt=attempt)
            try:
                step = self._next(name)
                has_expected_thread = "expected_thread_id" in step
                expected_thread = step.pop("expected_thread_id", None)
                if expected_thread is not None and not isinstance(expected_thread, str):
                    raise ValueError("expected_thread_id must be a string or null")
                if has_expected_thread and expected_thread != current_thread:
                    raise ValueError("agent fixture expected a different Thread ID")
                configured_thread = step.pop("thread_id", None)
                if configured_thread is not None and (
                    not isinstance(configured_thread, str)
                    or not configured_thread.strip()
                ):
                    raise ValueError("thread_id must be a non-empty string")
                no_thread = step.pop("no_thread", False)
                if not isinstance(no_thread, bool):
                    raise ValueError("no_thread must be a boolean")
                if no_thread:
                    raise ValueError("scripted Run Invocation did not report a Thread ID")
                configured_error = step.pop("error", None)
                if isinstance(configured_error, str):
                    raise ValueError(configured_error)
                if configured_error is not None:
                    raise ValueError("error must be a string")
            except ValueError as error:
                start_invocation()
                notify("failed", attempt_count=attempt, error=str(error))
                raise
            reported_thread = configured_thread or current_thread or default_thread
            if current_thread is not None and reported_thread != current_thread:
                message = "agent fixture resume reported a different Thread ID"
                report_thread(reported_thread, attempt)
                notify("failed", attempt_count=attempt, error=message)
                raise ValueError(message)
            try:
                result = decode(step)
                validate(result)
            except ValueError as error:
                report_thread(reported_thread, attempt)
                if attempt < 3:
                    current_thread = reported_thread
                    continue
                notify("failed", attempt_count=attempt, error=str(error))
                raise
            report_thread(reported_thread, attempt)
            current_thread = reported_thread
            notify(
                "completed",
                reported_thread_id=current_thread,
                attempt_count=attempt,
            )
            return result, current_thread
        raise AssertionError("unreachable")

    def _maybe_fail_initial_credential(
        self, role: str, request: dict[str, Any] | None = None
    ) -> None:
        scope = (
            None
            if request is None
            else (
                "run_repair"
                if request.get("candidate_acceptance") is True
                else request.get("acceptance_scope")
            )
        )
        scoped_role = f"{role}:{scope}" if isinstance(scope, str) else None
        failures = (
            self.initial_credential_failures_by_role.get(scoped_role)
            if scoped_role is not None
            else None
        )
        if failures is None:
            failures = self.initial_credential_failures_by_role.get(role)
        if failures:
            _raise_initial_credential_failure(failures.pop(0))
        if self.initial_credential_failures:
            _raise_initial_credential_failure(self.initial_credential_failures.pop(0))

    def _wait_for_invocation_gate(
        self, role: str, request: dict[str, Any], *, attempt: int = 1
    ) -> None:
        configured = self.data.get("invocation_gate")
        if configured is None:
            configured = self.data.get("agent_gate")
        if configured is None and "pause_file" in self.data:
            configured = {
                "pause_file": self.data.get("pause_file"),
                "release_file": self.data.get("release_file"),
            }
        if configured is None:
            return
        if not isinstance(configured, dict):
            raise ValueError("invocation_gate must be an object")
        configured_attempt = configured.get("attempt", 1)
        if (
            not isinstance(configured_attempt, int)
            or isinstance(configured_attempt, bool)
            or configured_attempt <= 0
        ):
            raise ValueError("invocation_gate.attempt must be a positive integer")
        if configured_attempt != attempt:
            return

        selected_role = configured.get("role")
        role_alias = {
            "developments": "development",
            "development": "developments",
            "reviews": "review",
            "run_reviews": "review",
            "review": "reviews",
            "publications": "publication",
            "run_publications": "publication",
            "publication": "publications",
        }.get(role)
        if selected_role is not None and selected_role not in {
            role,
            role_alias,
            request.get("acceptance_scope"),
            request.get("repair_scope"),
        }:
            return
        started_file = configured.get("started_file") or configured.get("ready_file")
        if started_file is None:
            started_file = configured.get("pause_file")
        release_file = configured.get("release_file") or configured.get(
            "continue_file"
        )
        if not isinstance(started_file, str) or not started_file:
            raise ValueError("invocation_gate.started_file must be a non-empty path")
        if not isinstance(release_file, str) or not release_file:
            raise ValueError("invocation_gate.release_file must be a non-empty path")

        started_path = Path(started_file)
        release_path = Path(release_file)
        started_path.parent.mkdir(parents=True, exist_ok=True)
        started_path.touch()
        timeout = configured.get("timeout_seconds", 30)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            timeout_value = float("nan")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout_value)
            or timeout_value <= 0
            or timeout_value > 300
        ):
            raise ValueError(
                "invocation_gate.timeout_seconds must be finite and in (0, 300]"
            )
        deadline = time.monotonic() + timeout_value
        while not release_path.exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("invocation_gate timed out waiting for release")
            time.sleep(min(0.02, remaining))

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


def _is_initial_credential_failure(value: object) -> bool:
    if isinstance(value, str):
        return True
    if not isinstance(value, dict) or not isinstance(value.get("message"), str):
        return False
    http_status = value.get("http_status")
    return http_status is None or (
        type(http_status) is int and 100 <= http_status <= 599
    )


def _raise_initial_credential_failure(value: object) -> None:
    if isinstance(value, str):
        raise InitialCredentialUnavailable("credential_unavailable")
    assert isinstance(value, dict)
    raise InitialCredentialUnavailable(
        "credential_unavailable", http_status=value.get("http_status")
    )


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


def _review_artifact(data: dict[str, Any]) -> dict[str, Any]:
    artifact = data.get("artifact")
    if isinstance(artifact, dict):
        return dict(artifact)
    artifact = dict(data)
    artifact.pop("thread_id", None)
    artifact.pop("expected_thread_id", None)
    return artifact


def _publication_result(data: dict[str, Any]) -> dict[str, Any]:
    blockers = _human_blockers(data)
    if blockers is not None:
        return {
            "result_kind": "human_blocker",
            "commit_message": None,
            "pr_title": None,
            "pr_body_markdown": None,
            "human_blockers": list(blockers),
        }
    return _publication_wire(data)


def _validate_publication(data: dict[str, Any], *, delivery_run: str) -> None:
    normalized = parse_publication_wire_result(data)
    if normalized["result_kind"] == "publication":
        PublicationArtifact.parse(normalized, delivery_run=delivery_run)


def _publication_wire(data: dict[str, Any]) -> dict[str, Any]:
    if "result_kind" in data or "invalid" in data:
        return data
    result = dict(data)
    result["result_kind"] = "publication"
    result["human_blockers"] = None
    return result
