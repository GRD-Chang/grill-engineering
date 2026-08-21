from __future__ import annotations

"""Fail-closed classification for Required Check failure evidence."""

import tomllib
from pathlib import Path
from typing import Any

from agent_run.external_supervision import (
    ensure_supervision_window,
    wait_for_github_convergence,
)


def annotate_configured_code_failures(
    checks: list[dict[str, Any]],
    repository_root: Path,
    *,
    expected_head_sha: str | None = None,
) -> list[dict[str, Any]]:
    """Bind GitHub job-step facts to repository-owned repairability policy."""

    configured = _configured_code_steps(repository_root)
    annotated: list[dict[str, Any]] = []
    for raw in checks:
        check = dict(raw)
        check.pop("repairability", None)
        if _job_proves_code_failure(check, configured, expected_head_sha):
            check["repairability"] = "code_failure"
        annotated.append(check)
    return annotated


def _configured_code_steps(
    repository_root: Path,
) -> frozenset[tuple[str, str, str]]:
    path = repository_root / "pyproject.toml"
    if not path.is_file():
        return frozenset()
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    tool = document.get("tool")
    agent_run = tool.get("agent-run") if isinstance(tool, dict) else None
    required = (
        agent_run.get("required-checks") if isinstance(agent_run, dict) else None
    )
    values = (
        required.get("code-failure-steps") if isinstance(required, dict) else None
    )
    if values is None:
        return frozenset()
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.count("::") == 2 for value in values
    ):
        raise ValueError(
            "tool.agent-run.required-checks.code-failure-steps must contain "
            "workflow::name::step strings"
        )
    return frozenset(tuple(value.split("::", 2)) for value in values)


def _job_proves_code_failure(
    check: dict[str, Any],
    configured: frozenset[tuple[str, str, str]],
    expected_head_sha: str | None,
) -> bool:
    workflow = check.get("workflow")
    name = check.get("name")
    job = check.get("job")
    if (
        not isinstance(workflow, str)
        or not isinstance(name, str)
        or not isinstance(expected_head_sha, str)
        or not isinstance(job, dict)
        or job.get("workflow_name") != workflow
        or job.get("name") != name
        or job.get("head_sha") != expected_head_sha
        or job.get("status") != "completed"
        or job.get("conclusion") != "failure"
    ):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    failed_steps: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            return False
        conclusion = step.get("conclusion")
        if conclusion in {"success", "skipped"}:
            if step.get("status") != "completed":
                return False
            continue
        step_name = step.get("name")
        if (
            conclusion != "failure"
            or step.get("status") != "completed"
            or not isinstance(step_name, str)
            or (workflow, name, step_name) not in configured
        ):
            return False
        failed_steps.append(step_name)
    return bool(failed_steps)


def is_explicitly_repairable_code_failure(evidence: object) -> bool:
    """Return true only when every reported failure is a code-test failure."""

    if not isinstance(evidence, dict):
        return False
    checks = evidence.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    for check in checks:
        if not isinstance(check, dict):
            return False
        if str(check.get("bucket", "")).lower() != "fail":
            return False
        if str(check.get("state", "")).upper() != "FAILURE":
            return False
        if check.get("repairability") != "code_failure":
            return False
        if not all(
            isinstance(check.get(key), str) and str(check[key]).strip()
            for key in ("name", "workflow", "link")
        ):
            return False
    return True


def supervise_unrepairable_check_failure(
    state: dict[str, Any],
    phase_owner: dict[str, Any],
    *,
    phase: str,
    waiting_for: str,
) -> None:
    """Persist supervision without creating a Repair or consuming its budget."""

    phase_owner["phase"] = phase
    wait_for_github_convergence(
        state,
        code="github_check_failure_not_repairable",
        message="Required Check evidence does not prove a repairable code failure",
        waiting_for=waiting_for,
    )
    ensure_supervision_window(state)
