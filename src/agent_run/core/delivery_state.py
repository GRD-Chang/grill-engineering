"""Shared delivery transaction operations, without scheduling or I/O authority."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from agent_run.errors import KernelError
from agent_run.model import World, wire_json
from agent_run.storage.sqlite import Transaction
from agent_run.storage.run_scope import run_statement, transaction_scope


@dataclass(frozen=True, slots=True)
class DeliveryState:
    now: float

    def gate(self, tx: Transaction, subject: str, code: str, *, purpose: str | None = None, evidence: str | None = None) -> None:
        tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO gates(subject,code,purpose,evidence,run_id) VALUES (?,?,?,?,?)',
            (subject, code, purpose, evidence), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        # The existing release guard still fences any unsettled formal write.
        self.release_integration(tx, subject)
        self.event(tx, subject, "paused", code, evidence)

    def event(self, tx: Transaction, subject: str, kind: str, event_id: str, material: str | None = None, *, generation: int | None = None) -> None:
        job = tx.one(*run_statement(tx, 'SELECT generation,phase,purpose,current_attempt FROM subjects WHERE id=? AND run_id=?',
                  (subject,), run_last=True))
        if generation is None:
            generation = job["generation"]
        status = None
        if generation == job["generation"]:
            run = tx.one(*run_statement(tx, 'SELECT stopped,blocked FROM runs WHERE id=?',
                      (), run_last=True))
            status = wire_json({"stopped": bool(run["stopped"]), "blocked": run["blocked"],
                                "gates": [row["code"] for row in tx.rows(*run_statement(tx, "SELECT code FROM gates WHERE subject=? AND run_id=? ORDER BY code", (subject,), run_last=True))],
                                "attempt_status": None if job["current_attempt"] is None else
                                tx.one(*run_statement(tx, 'SELECT status FROM attempts WHERE id=? AND run_id=?',
                                    (job["current_attempt"],), run_last=True))[0]}).decode()
        tx.execute(*run_statement(tx, 'INSERT INTO history(subject,kind,identity,material_id,generation,created_at,phase,purpose,status,run_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (subject, kind, event_id, material, generation, self.now,
                    job["phase"] if status is not None else None, job["purpose"] if status is not None else None, status), run_last=True))

    def release_integration(self, tx: Transaction, subject: str) -> None:
        if not tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link') AND run_id=? LIMIT 1", (subject,), run_last=True)):
            tx.execute(*run_statement(tx, 'UPDATE runs SET integration_holder=NULL WHERE integration_holder=? AND id=?',
                (subject,), run_last=True))
            tx.execute(*run_statement(tx, 'DELETE FROM integration_queue WHERE subject=? AND run_id=?',
                (subject,), run_last=True))

    def observe_integration_snapshot(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        """Only an external change after a completed window spends allowance."""
        subject = job["id"]
        target = world.target
        if target is None:
            raise KernelError("target_branch_missing")
        if world.source is None and job["pr_number"] is not None:
            self.gate(tx, subject, "published_head_changed")
            return False
        source_sha = job["source_sha"] if world.source is None else world.source.sha
        previous = tx.rows(*run_statement(tx, 'SELECT * FROM integration_snapshots WHERE subject=? AND run_id=? ORDER BY window DESC LIMIT 1',
                       (subject,), run_last=True))
        allowance = tx.one(*run_statement(tx, 'SELECT * FROM resync_allowances WHERE subject=? AND current=1 AND run_id=?',
                        (subject,), run_last=True))
        snapshot = previous[0] if previous else None
        first = snapshot is None or snapshot["source_ref"] != job["source_ref"] or snapshot["target_ref"] != job["target_ref"]
        source_changed = snapshot is not None and not first and snapshot["source_sha"] != source_sha
        target_changed = snapshot is not None and not first and snapshot["target_sha"] != target.sha
        if snapshot is not None and not first:
            for changed, before, after in ((source_changed, snapshot["source_sha"], source_sha),
                                           (target_changed, snapshot["target_sha"], target.sha)):
                if changed and (before, after) not in world.ancestry:
                    self.gate(tx, subject, "non_append_integration_history")
                    return False
        charged = bool(snapshot is not None and not first and (source_changed or target_changed) and not snapshot["open"])
        if charged and allowance["used"] >= allowance["allowance"]:
            self.gate(tx, subject, "integration_resync_exhausted")
            return False
        if first or charged:
            window = 1 if snapshot is None else snapshot["window"] + 1
            tx.execute(*run_statement(tx, 'INSERT INTO integration_snapshots(subject,window,allowance_generation,source_ref,target_ref,source_sha,target_sha,open,run_id) VALUES (?,?,?,?,?,?,?,1,?)',
                (subject, window, allowance["generation"], job["source_ref"], job["target_ref"], source_sha, target.sha), run_last=True))
            if charged:
                tx.execute(*run_statement(tx, 'UPDATE resync_allowances SET used=used+1 WHERE subject=? AND current=1 AND run_id=?',
                    (subject,), run_last=True))
        elif snapshot is not None and (source_changed or target_changed):
            tx.execute(*run_statement(tx, 'UPDATE integration_snapshots SET source_sha=?,target_sha=? WHERE subject=? AND window=? AND run_id=?',
                (source_sha, target.sha, subject, snapshot["window"]), run_last=True))
        if world.source is not None and source_sha != job["source_sha"]:
            tx.execute(*run_statement(tx, 'UPDATE subjects SET source_sha=? WHERE id=? AND run_id=?',
                (source_sha, subject), run_last=True))
        if first or source_changed or target_changed:
            evidence = tx.put(subject, "integration_snapshot", wire_json({"source_sha": source_sha, "target_sha": target.sha, "charged": charged}))
            self.event(tx, subject, "integration_snapshot", str(allowance["generation"]), evidence)
        return True

    def update_boundary(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        tx.execute(*run_statement(tx, "UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,version=version+1 WHERE id=? AND run_id=?",
            (job["id"],), run_last=True))
        if run["final_action"]:
            tx.execute(*run_statement(tx, "UPDATE actions SET status='updating' WHERE id=? AND run_id=?",
                (run["final_action"],), run_last=True))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
        self.event(tx, job["id"], "boundary_update", job["candidate_sha"] or job["id"])

    def record_source_write(self, tx: Transaction, subject: str, ref: str, head: str) -> None:
        tx.execute(*run_statement(tx, 'UPDATE integration_snapshots SET source_sha=? WHERE subject=? AND source_ref=? AND window=(SELECT max(i.window) FROM integration_snapshots i WHERE i.subject=? AND i.run_id=integration_snapshots.run_id) AND run_id=?',
            (head, subject, ref, subject), run_last=True))
