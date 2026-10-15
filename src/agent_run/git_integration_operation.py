"""Bound mechanical integration commands and preserve their failure evidence."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from agent_run.git_errors import GitError

if TYPE_CHECKING:
    from agent_run.git import GitRepository

INTEGRATION_COMMAND_TIMEOUT_SECONDS = 30.0


class IntegrationGitOperation:
    def __init__(self, git: GitRepository, checkout: Path, operation: str) -> None:
        self.git = git
        self.checkout = checkout
        self.operation = operation
        self._command: dict[str, Any] = {}

    def __enter__(self) -> IntegrationGitOperation:
        return self

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self._command = {"command": ["git", *arguments], "returncode": None,
                         "stdout": "", "stderr": ""}
        try:
            result = self.git._run_in(
                self.checkout, *arguments, timeout=INTEGRATION_COMMAND_TIMEOUT_SECONDS,
            )
        except GitError as error:
            self._command.update(error.evidence)
            raise
        except OSError as error:
            self._command["stderr"] = str(error)
            raise GitError(str(error)) from error
        self._command.update(returncode=result.returncode, stdout=result.stdout,
                             stderr=result.stderr)
        return result

    def command_evidence(self) -> dict[str, Any]:
        return dict(self._command)

    def restore_command(self, evidence: dict[str, Any]) -> None:
        self._command = evidence

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not isinstance(error, GitError):
            return
        if error.evidence.get("operation"):
            return
        evidence: dict[str, Any] = {
            "operation": self.operation, "checkout": str(self.checkout),
            "command": [], "returncode": None, "stdout": "", "stderr": "",
        } | self._command | error.evidence
        for key, reference in (("head_sha", "HEAD"), ("merge_head_sha", "MERGE_HEAD")):
            try:
                result = self.git._run_in(
                    self.checkout, "rev-parse", "--verify", reference, timeout=3.0,
                )
                evidence[key] = result.stdout.strip() if result.returncode == 0 else None
            except (GitError, OSError):
                evidence[key] = None
        # Escaped copies retain CR and other diagnostic characters across the
        # StateStore's credential-safe display normalization. JSON decoding
        # reconstructs the original command output without a binary payload.
        evidence["stdout_json"] = json.dumps(evidence["stdout"], ensure_ascii=False)
        evidence["stderr_json"] = json.dumps(evidence["stderr"], ensure_ascii=False)
        error.evidence = evidence
        command = shlex.join(evidence["command"])
        error.args = (f"{error} [operation={self.operation}; command={command}]",)
