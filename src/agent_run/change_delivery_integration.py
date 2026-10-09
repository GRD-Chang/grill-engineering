from __future__ import annotations

"""Integration after development and its applicable review."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    """Keep Ticket development and CI review before integration.

    A false result hands the changed durable phase back to orchestration.
    Ordinary Ticket integration preserves demand acceptance. Parent adapters
    retain their complete acceptance scopes; Parent-only integration work uses
    its cumulative allowance independently of demand and CI.
    """
    label = engine.contract.label
    if label.startswith("ticket-"):
        if before_review:
            return True
        if not acquire_ticket_slot(state, job):
            engine.save(state)
            return False
        engine.save(state)
        if not prepare_ticket_integration(engine, state, job, checkout):
            return False
        return True
    if label == "parent-only":
        if not before_review and job.get("attempt_kind") == "ci_repair":
            # CI Development–Review has finished. New combination findings
            # belong to integration, while the original CI evidence stays saved.
            job["attempt_kind"] = "integration"
            engine.save(state)
        if before_review and (job.get("attempt_kind") == "ci_repair" or not isinstance(job.get("parent_integration"), dict)):
            return True
        return prepare_parent_integration(engine, state, job, checkout)
    if label.startswith("run-repair-"):
        if before_review and job.get("attempt_kind") == "ci_repair":
            return True
        if not before_review and job.get("attempt_kind") == "ci_repair":
            # Run Repair ordinary quota is the Parent integration allowance.
            job["attempt_kind"] = "ordinary"
            engine.save(state)
        if not isinstance(job.get("integrated_sha"), str) and needs_repair_integration(engine, job):
            return prepare_repair_integration(engine, state, job, checkout)
    return True
