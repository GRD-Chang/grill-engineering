from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_run.agents import DevelopmentResult, ReviewResult


class FixtureAgentBackend:
    """Deterministic worker used by cross-process black-box tests."""

    def __init__(self, path: Path) -> None:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("agent fixture root must be an object")
        self.data = value
        self.positions = {"developments": 0, "publications": 0, "reviews": 0}

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        step = self._next("developments")
        expected = step.get("expected_thread_id")
        if expected != request.get("thread_id"):
            raise ValueError("scripted Development Thread expectation failed")
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
        return DevelopmentResult(
            thread_id=_string(step, "thread_id"),
            summary=_string(step, "summary"),
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._next("publications")

    def review(self, request: dict[str, Any]) -> ReviewResult:
        step = self._next("reviews")
        artifact = step.get("artifact")
        if not isinstance(artifact, dict):
            artifact = dict(step)
            artifact.pop("thread_id", None)
        return ReviewResult(
            thread_id=_string(step, "thread_id"),
            artifact=artifact,
        )

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
