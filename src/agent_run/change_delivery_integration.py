from __future__ import annotations

"""One integration gate before CI review and before publication."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.ci_recovery_budget import ci_review_required
from agent_run.parent_only_integration import prepare_parent_integration
from agent_run.run_repair_integration import needs_repair_integration, prepare_repair_integration
from agent_run.ticket_integration import prepare_ticket_integration
from agent_run.ticket_queue import acquire as acquire_ticket_slot

if TYPE_CHECKING:
    from agent_run.change_delivery import ChangeDeliveryEngine


def prepare_delivery_integration(
    engine: ChangeDeliveryEngine, state: dict[str, Any],
    job: dict[str, Any], checkout: Path, *, before_review: bool = False,
) -> bool:
    """Keep demand work fixed; review CI repairs only after final integration.

    A false result hands the changed durable phase back to orchestration.
    Ordinary Ticket integration preserves demand acceptance. Parent adapters
    retain their complete acceptance scopes; Parent-only integration work uses
    its cumulative allowance independently of demand and CI.
    """
    label = engine.contract.label
    if label.startswith("ticket-"):
        ci_repair = job.get("attempt_kind") == "ci_repair"
        if before_review and not ci_repair:
            return True
        if not acquire_ticket_slot(state, job):
            engine.save(state)
            return False
        engine.save(state)
        if not prepare_ticket_integration(engine, state, job, checkout):
            return False
        if ci_repair:
            acceptance = job.get("acceptance_record")
            current_review = isinstance(acceptance, dict) and engine.adapter.acceptance_is_current(
                state, job, acceptance
            )
            engine._bind_ticket_ci_integration(
                state, job,
                invalidate_review=(
                    # Before Review, missing acceptance may be unfinished work
                    # on this exact head. Keep its pending Attempt; snapshot or
                    # head changes already invalidate it during integration.
                    not before_review
                    and ci_review_required(job, is_ticket=True) and not current_review
                ),
            )
            if not before_review and job["phase"] == "candidate":
                return False
        return True
    if label == "parent-only":
        if before_review and not isinstance(job.get("parent_integration"), dict):
            return True
        return prepare_parent_integration(engine, state, job, checkout)
    if label.startswith("run-repair-"):
        if not isinstance(job.get("integrated_sha"), str) and needs_repair_integration(engine, job):
            return prepare_repair_integration(engine, state, job, checkout)
    return True
