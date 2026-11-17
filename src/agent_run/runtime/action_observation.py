"""Observe this control request without granting or restarting any work."""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, TYPE_CHECKING

from agent_run.runtime import run_selection
from agent_run.storage.run_scope import run_statement

if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel
    from agent_run.storage.routing import RepositoryRoute


def observe_request(parsed: argparse.Namespace, receipt: dict[str, Any], *,
                    repository_route: RepositoryRoute | None = None) -> dict[str, Any]:
    """Approval waits for delivery; Resume waits for execution of its intent.

    The caller may leave this observation without cancelling the accepted
    Action. A deadline never changes authority or implies successful delivery.
    """
    if parsed.command not in {"approve", "resume"}:
        return receipt
    receipt.update(requested_action=parsed.command, requested_action_succeeded=False)
    if receipt["result"] == "attached":
        receipt.update(requested_action_applied=False, request_outcome="not_applied")
        return receipt
    if receipt.get("request_outcome") == "already_completed":
        receipt["requested_action_succeeded"] = True
        return receipt
    selection = run_selection.control_selection(parsed, language=getattr(parsed, "_run_language", "zh"))
    routes = (repository_route,) if repository_route is not None else run_selection.managed_repository_routes(selection)
    route, location = run_selection.repository_target(selection, routes)
    if location.run_id != receipt["run_id"]:
        raise ValueError("Control observation Run changed")
    from agent_run.runtime.task import selected_kernel
    kernel = selected_kernel(route, location.run_id)
    return _wait(kernel, receipt, parsed)


def _sample(kernel: Kernel, receipt: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Return applied, final operation and post-application execution proof."""
    with kernel.storage.transaction(write=False) as tx:
        action = tx.one(*run_statement(tx, "SELECT * FROM actions WHERE run_id=? AND id=?", (receipt["action_id"],)))
        receipt["status"] = "action_" + action["status"]
        owner = action["subject"]
        if owner is None:
            owner = tx.one(*run_statement(tx, "SELECT subject FROM material WHERE run_id=? AND id=?", (action["receipt_id"],)))[0]
        saved = None if action["receipt_id"] is None else json.loads(tx.get(action["receipt_id"], owner))
        applied = isinstance(saved, dict) and saved.get("applied") is True and saved.get("action") == action["id"]
        final = action["kind"] == "approve"
        if action["subject"] is not None:
            subject = tx.one(*run_statement(tx, "SELECT kind,phase,grant_id FROM subjects WHERE run_id=? AND id=?", (action["subject"],)))
            final = final or (subject["kind"] != "ticket" and subject["grant_id"] is not None
                              and subject["phase"] in {"merge", "completion_comment", "close", "complete"})
        host = tx.rows(*run_statement(tx, "SELECT owner,epoch,usage_ready FROM hosts WHERE run_id=? AND action_id=? LIMIT 1",
                                     (receipt.get("host_action_id", action["id"]),)))
        ready = False
        if host and host[0]["usage_ready"]:
            proofs = tx.rows(*run_statement(tx,
                "SELECT h.subject,h.material_id FROM history h JOIN history c ON c.run_id=h.run_id "
                "AND c.identity=h.identity AND c.kind='control_applied' "
                "WHERE h.run_id=? AND c.material_id=? AND h.kind='resume_execution' "
                "AND h.identity=? AND h.position>c.position LIMIT 1", (action['receipt_id'], action['id'])))
            if proofs:
                proof = json.loads(tx.get(proofs[0]['material_id'], proofs[0]['subject']))
                ready = proof == {'action': action['id'], 'receipt': action['receipt_id'], 'execution': {
                    'run_id': kernel.storage.run_id, 'owner': host[0]['owner'], 'epoch': host[0]['epoch']}}
    return applied, final, ready


def _wait(kernel: Kernel, receipt: dict[str, Any], parsed: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + 600
    observe_at = time.monotonic() + 1
    receipt["request_outcome"] = "unconfirmed"
    while True:
        applied, final, ready = _sample(kernel, receipt)
        receipt["requested_action_applied"] = applied
        if receipt["status"] in {"action_failed", "action_superseded", "action_updating"}:
            receipt["request_outcome"] = receipt["status"].removeprefix("action_")
            return receipt
        if final:
            completed = kernel.completed_final_action(parsed.command)
            if completed is not None and completed["action_id"] == receipt["action_id"]:
                receipt.update(request_outcome="delivered", requested_action_succeeded=True,
                               delivery_completed=True)
                return receipt
        elif applied and ready:
            receipt.update(request_outcome="resumed", requested_action_succeeded=True, executor_started=True)
            return receipt
        if time.monotonic() >= deadline:
            receipt["request_outcome"] = "observation_timeout"
            return receipt
        if receipt["host"] == "unknown":
            return receipt  # An uncertain launch is never retried by observation.
        if time.monotonic() >= observe_at:
            from agent_run.runtime.host_control import manager_for
            from agent_run.runtime.foreground_host import ForegroundHost
            from agent_run.runtime.handoff import reconcile_handoff
            from agent_run.runtime.package_lease import default_runner_lock_path
            manager = manager_for(kernel, parsed, os.environ)
            action_id = kernel.active_host_action()
            if action_id is None:
                # A settled Host may have just committed completion; sample it
                # once more before reporting an interrupted request.
                completed = kernel.completed_final_action(parsed.command) if final else None
                if completed is not None and completed["action_id"] == receipt["action_id"]:
                    continue
                receipt["request_outcome"] = "executor_exited"
                return receipt
            observed = (manager.observe(kernel, kernel.host_request(action_id), default_runner_lock_path(dict(os.environ)))
                        if isinstance(manager, ForegroundHost) else reconcile_handoff(kernel, action_id, manager))
            receipt["host"] = observed.disposition
            observe_at = time.monotonic() + 1
        time.sleep(.1)
