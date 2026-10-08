"""Explicit retry-window renewal for Parent and Final Run publication."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.operator_gate import operator_gate_subjects
from agent_run.publication_operation_retry import require_publication_operation_retry


def resume_parent_publication_retry(state: dict[str, Any]) -> bool:
    paused = [
        (location, subject) for location, subject in operator_gate_subjects(state)
        if location in {"parent", "run_repair", "run_publication"}
        and subject.get("phase") == "publication_pending"
    ]
    if not paused:
        return False
    if len(paused) != 1:
        raise ValueError("multiple publication pauses require an unambiguous resume target")
    location, subject = paused[0]
    retry = require_publication_operation_retry(subject.get("publication_operation_retry"))
    if retry["attempts"] != retry["limit"]:
        raise ValueError("publication retry window is not exhausted")
    if location == "run_publication":
        if not isinstance(subject.get("artifact"), dict):
            raise ValueError("Final Run publication retry is missing its stored artifact")
        phase, status = "waiting_external", "run_publication_pending"
    else:
        if not isinstance(subject.get("publication_sha"), str):
            raise ValueError("Change publication retry is missing its publication SHA")
        phase = subject.get("execution_resume_phase", "publishing")
        if phase not in {"publishing", "merging", "merged"}:
            raise ValueError("Change publication retry has no recoverable phase")
        status = "parent_delivery_pending" if location == "parent" else "run_acceptance_pending"
    history = subject.setdefault("publication_operation_retry_history", [])
    if not isinstance(history, list):
        raise ValueError("publication retry history must be a list")
    history.append(deepcopy(retry))
    subject.pop("publication_operation_retry", None)
    subject.pop("last_publication_error", None)
    subject.pop("execution_resume_phase", None)
    subject["phase"] = phase
    state.update(status=status, terminal_kind=None, diagnostics=[])
    return True
