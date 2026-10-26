"""Concise Status from current committed facts and original publication proof."""
import json
from pathlib import Path
import re
import sqlite3
import time

from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.contracts.repository import same_repository
from agent_run.contracts.task import TaskKey
from agent_run.model import Role, artifact, review_outcome
from agent_run.queries.read_facts import material, read_boundaries
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.tasks import read_task, task_path


def _run(tx: Transaction, database: Path) -> sqlite3.Row:
    run = tx.one("SELECT * FROM runs")
    if not isinstance(run["repository_root"], str) or not Path(run["repository_root"]).is_absolute():
        raise ValueError("Status Task workspace is invalid")
    task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
    if read_task(task_path(task)) != (run["id"], database):
        raise ValueError("Status Task binding changed")
    return run


def status_subjects(database: Path) -> list[str]:
    """Only the published Task can activate this channel, even before its first PR."""
    with Storage(database).transaction(write=False) as tx:
        _run(tx, database.absolute())
        return [str(row[0]) for row in tx.all("SELECT id FROM subjects WHERE pr_number IS NOT NULL ORDER BY id")]


def _recorded_review(tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> str:
    """Describe recorded applicability, never claim a fresh remote observation."""
    current = (tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (job["current_attempt"], job["id"]))
               if job["current_attempt"] else None)
    active = current is not None and current["role"] == "review"
    review = current if active else (tx.one("SELECT * FROM attempts WHERE id=? AND subject=?",
        (job["review_attempt"], job["id"])) if job["review_attempt"] else None)
    if review is None:
        return "not_recorded"
    snapshots = tx.rows("SELECT target_sha FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (job["id"],))
    base = snapshots[0][0] if snapshots else job["integration_target"] or job["base_sha"]
    revision = (tx.one("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))[0]
                if job["kind"] == "ticket" else run["observed_parent_revision"])
    if (review["role"] != "review" or review["generation"] != job["generation"]
            or review["revision"] != job["revision"] or review["revision"] != revision
            or review["parent_revision"] != run["parent_revision"] or review["parent_revision"] != run["observed_parent_revision"]
            or review["graph_revision"] != run["graph_revision"] or review["graph_revision"] != run["observed_graph_revision"]
            or review["base_sha"] != base or any(review[key] != job[key] for key in (
                "candidate_sha", "candidate_tree", "expected_tree", "completion_revision"))
            or review["candidate_sha"] is None or review["candidate_tree"] is None):
        return "stale"
    if active and review["status"] in {"allocated", "running", "returned", "continuing"}:
        return "pending"
    if review["status"] != "consumed" or review["result_id"] is None or review["outcome"] not in {"pass", "fail"}:
        return "unknown"
    result = artifact(Role.REVIEW, tx.get(review["result_id"], job["id"]))
    if review_outcome(result) != review["outcome"]:
        return "unknown"
    return "pass" if review["outcome"] == "pass" else "request_changes"


def project_status(database: Path, subject: str) -> StatusProjection:
    database = database.absolute()
    with Storage(database).transaction(write=False) as tx:
        run = _run(tx, database)
        job = tx.one("SELECT * FROM subjects WHERE id=? AND run_id=?", (subject, run["id"]))
        if any(type(job[key]) is not int or job[key] <= 0 for key in ("pr_number", "issue", "generation")):
            raise ValueError("Status publication number is invalid")
        publications = tx.all("SELECT * FROM effects WHERE subject=? AND generation=? "
                              "AND operation='pr_write' AND status='confirmed' ORDER BY rowid",
                              (subject, job["generation"]))
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
            check = tx.one("SELECT * FROM checks WHERE id=? AND subject=?", (job["checks_id"], subject))
            if check["pr_number"] == target.pr_number and check["head_sha"] == pr["head"]["sha"]:
                material(tx, check["material_id"], subject)
                checks = check["result"]
        gates = ", ".join(item["code"] for item in boundary["gates"]) or "none"
        hosts = tx.all("SELECT status,usage_released,observation_id FROM hosts")
        host = "not_recorded" if not hosts else "exit_unproven"
        if hosts and all(row["status"] == "exited" and row["usage_released"] for row in hosts):
            if (run["executor"] is None and not tx.rows("SELECT id FROM leases LIMIT 1")
                    and not tx.rows("SELECT owner FROM retired_executors WHERE status!='exited' LIMIT 1")):
                host = "exited"
        cleanup = boundary["delivery_cleanup"]
        literal_run = json.dumps(run["id"], ensure_ascii=False)
        delimiter = "`" * (1 + max((len(part) for part in re.findall(r"`+", literal_run)), default=0))
        lines = ["## Agent Run Status", f"Run: {delimiter} {literal_run} {delimiter} / {job['kind']} #{job['issue']}",
                 f"Delivery: {job['phase']}; Run: {boundaries['status']}; gates: {gates}",
                 f"Candidate: `{job['candidate_sha'] or 'not_recorded'}`",
                 f"Recorded PR head: `{pr['head']['sha']}`; base: `{pr['base']['sha']}`",
                 f"Recorded Review: {_recorded_review(tx, run, job)} (recorded candidate/base/scope; not a live remote check)",
                 f"Integration gate: {job['gate_mode'] or 'not_recorded'}; Required Checks: {checks}",
                 f"Cleanup: {cleanup['state'] if cleanup else 'not_recorded'}; Host exit: {host}"]
        actions = [item["next_action"] for item in boundaries["actions"] if item["subject"] in {None, subject}]
        remaining = boundaries["delivery_cleanup"]
        if remaining is not None:
            actions.extend(item["recovery_action"] for item in remaining["items"]
                           if item["kind"] == job["kind"] and item["branch"] == target.source_ref)
        lines.append("Next action:" if actions else "Next action: automatic / none")
        for action in dict.fromkeys(actions):
            # Render the authoritative command literally. Markdown inline code
            # otherwise removes legal backticks from the displayed selector.
            fence = "`" * max(3, 1 + max((len(part) for part in re.findall(r"`+", action)), default=0))
            lines.extend(("", fence, action, fence))
        body = "\n".join(lines)
        if len(body.encode()) > 8192:
            raise ValueError("Status projection exceeds limit")
        return StatusProjection(target, body)
