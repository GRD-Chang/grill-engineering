from __future__ import annotations

"""Bounded waiting for CI evidence that does not match its observation."""

from typing import Any

from agent_run.external_supervision import (
    ensure_supervision_window,
    wait_for_github_convergence,
)


def supervise_check_evidence_mismatch(
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
        code="github_check_evidence_observation_pending",
        message="Required Check failure evidence does not match its current PR/head observation",
        waiting_for=waiting_for,
    )
    ensure_supervision_window(state)
