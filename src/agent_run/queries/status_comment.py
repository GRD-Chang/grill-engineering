"""Concise Status from current committed facts and original publication proof."""
import json
from collections.abc import Sequence
from pathlib import Path
import re
import sqlite3
import time

from agent_run.storage.run_scope import run_statement
from agent_run.contracts.status_comment import StatusBinding, StatusProjection, StatusTarget
from agent_run.contracts.repository import same_repository
from agent_run.queries.read_facts import material, read_boundaries, recorded_review
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.routing import open_run
from agent_run.storage.tasks import run_is_current


def _run(tx: Transaction, database: Path) -> sqlite3.Row:
    run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', ()))
    if not isinstance(run["repository_root"], str) or not Path(run["repository_root"]).is_absolute():
        raise ValueError("Status Task workspace is invalid")
    if not run_is_current(tx):
        raise ValueError("Status Task binding changed")
    return run


def status_binding(database: Path, *, storage: Storage) -> StatusBinding:
    """Read only the selected Task's physical Status configuration."""
    with open_run(database, storage=storage).transaction(write=False) as tx:
        run = _run(tx, database.absolute())
        if run["source_sequence"] is None:
            raise ValueError("Status source is not configured")
        row = tx.one("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (run["id"],))
        inputs = json.loads(tx.get(row["material_id"], row["subject"]))
        root = Path(run["repository_root"])
        if (root.resolve() != root or not isinstance(inputs, dict)
                or not inputs.get("repository_remote") or not isinstance(inputs.get("runner_identity"), dict)):
            raise ValueError("Status transport binding is invalid")
        return StatusBinding(root, run["repository"], inputs["runner_identity"])


def status_subjects(database: Path, *, storage: Storage) -> list[str]:
    """Only the published Task can activate this channel, even before its first PR."""
    with open_run(database, storage=storage).transaction(write=False) as tx:
        _run(tx, database.absolute())
        return [str(row[0]) for row in tx.all(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? AND pr_number IS NOT NULL ORDER BY id', ()))]


def render_status_body(target: StatusTarget, kind: str, details: Sequence[str], actions: Sequence[str]) -> str:
    """Render already-observed facts without interpreting or granting actions."""
    literal_run = json.dumps(target.run_id, ensure_ascii=False)
    delimiter = "`" * (1 + max((len(part) for part in re.findall(r"`+", literal_run)), default=0))
    lines = ["## Agent Run Status", f"Run: {delimiter} {literal_run} {delimiter} / {kind} #{target.issue}", *details]
    lines.append("Next action:" if actions else "Next action: automatic / none")
    for action in dict.fromkeys(actions):
        # Inline code would remove legal backticks from the displayed selector.
        fence = "`" * max(3, 1 + max((len(part) for part in re.findall(r"`+", action)), default=0))
        lines.extend(("", fence, action, fence))
    body = "\n".join(lines)
    if len(body.encode()) > 8192:
        raise ValueError("Status projection exceeds limit")
    return body


def project_status(database: Path, subject: str, *, storage: Storage) -> StatusProjection:
    database = database.absolute()
    with open_run(database, storage=storage).transaction(write=False) as tx:
        run = _run(tx, database)
        job = tx.one("SELECT * FROM subjects WHERE id=? AND run_id=?", (subject, run["id"]))
        if any(type(job[key]) is not int or job[key] <= 0 for key in ("pr_number", "issue", "generation")):
            raise ValueError("Status publication number is invalid")
        publications = tx.all(*run_statement(tx, "SELECT * FROM effects WHERE run_id=? AND subject=? AND generation=? AND operation='pr_write' AND status='confirmed' ORDER BY rowid", (subject, job["generation"])))
        proofs = []
        for effect in publications:
            receipt = material(tx, effect["receipt_id"], subject)
            pr = receipt.get("pr") if isinstance(receipt, dict) else None
            if isinstance(pr, dict) and pr.get("number") == job["pr_number"]:
                if (not isinstance(pr.get("repository"), str) or not isinstance(pr.get("head_repository"), str)
                        or not same_repository(pr["repository"], run["repository"])
                        or not same_repository(pr["head_repository"], run["repository"])
                        or pr.get("actor") != effect["actor"] or not effect["actor"]
                        or not isinstance(pr.get("head"), dict) or pr["head"].get("sha") != effect["after_sha"]
                        or not isinstance(pr.get("base"), dict) or not isinstance(pr["base"].get("sha"), str)
                        or pr.get("source_ref") != job["source_ref"] or pr.get("target_ref") != job["target_ref"]):
                    raise ValueError("Status publication identity changed")
                proofs.append((effect, pr))
        if not proofs:
            raise ValueError("Status requires a confirmed original PR publication")
        original, first = proofs[0]
        if any(pr["actor"] != first["actor"] for _, pr in proofs):
            raise ValueError("Status publication actor changed")
        target = StatusTarget(run["repository"], run["id"], run["parent_issue"], subject, job["generation"], job["issue"],
                              job["pr_number"], original["actor"], original["id"], original["receipt_id"],
                              job["source_ref"], job["target_ref"])
        _, pr = proofs[-1]
        boundaries = read_boundaries(tx, time.time())
        boundary = next(item for item in boundaries["subjects"] if item["subject"] == subject)
        checks = "not_recorded"
        if job["checks_id"]:
            check = tx.one(*run_statement(tx, 'SELECT * FROM checks WHERE run_id=? AND id=? AND subject=?', (job["checks_id"], subject)))
            if check["pr_number"] == target.pr_number and check["head_sha"] == pr["head"]["sha"]:
                material(tx, check["material_id"], subject)
                checks = check["result"]
        gates = ", ".join(item["code"] for item in boundary["gates"]) or "none"
        hosts = tx.all(*run_statement(tx, 'SELECT status,usage_released,observation_id FROM hosts WHERE run_id=?', ()))
        host = "not_recorded" if not hosts else "exit_unproven"
        if hosts and all(row["status"] == "exited" and row["usage_released"] for row in hosts):
            if (run["executor"] is None and not tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE run_id=? LIMIT 1', ()))
                    and not tx.rows(*run_statement(tx, "SELECT owner FROM retired_executors WHERE run_id=? AND status!='exited' LIMIT 1", ()))):
                host = "exited"
        cleanup = boundary["delivery_cleanup"]
        lines = [f"Delivery: {job['phase']}; Run: {boundaries['status']}; gates: {gates}",
                 f"Candidate: `{job['candidate_sha'] or 'not_recorded'}`",
                 f"Recorded PR head: `{pr['head']['sha']}`; base: `{pr['base']['sha']}`",
                 f"Recorded Review: {recorded_review(tx, dict(run), dict(job))} (recorded candidate/base/scope; not a live remote check)",
                 f"Integration gate: {job['gate_mode'] or 'not_recorded'}; Required Checks: {checks}",
                 f"Cleanup: {cleanup['state'] if cleanup else 'not_recorded'}; Host exit: {host}"]
        actions = [item["next_action"] for item in boundaries["actions"] if item["subject"] in {None, subject}]
        remaining = boundaries["delivery_cleanup"]
        if remaining is not None:
            actions.extend(item["recovery_action"] for item in remaining["items"]
                           if item["kind"] == job["kind"] and item["branch"] == target.source_ref)
        return StatusProjection(target, render_status_body(target, job["kind"], lines, actions), pr["head"]["sha"])
