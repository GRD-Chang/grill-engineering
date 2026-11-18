"""Persist exact Required Checks and enter CI repair on the same subject budget."""
from dataclasses import asdict
import sqlite3
from typing import Any, Mapping

from agent_run.core.delivery_state import DeliveryState
from agent_run.model import Checks, wire_json
from agent_run.storage.sqlite import Transaction


def record(tx: Transaction, run: sqlite3.Row, job: sqlite3.Row | Mapping[str, Any],
           checks: Checks | None, *, repair_id: str | None = None) -> str | None:
    if checks is None or checks.pr_number != job['pr_number'] or checks.head_sha != job['publication_sha']:
        return None
    material = tx.put(job['id'], 'required_checks', wire_json(asdict(checks)))
    tx.execute('INSERT OR IGNORE INTO checks(id,subject,pr_number,head_sha,result,material_id,run_id) VALUES (?,?,?,?,?,?,?)',
               (material, job['id'], checks.pr_number, checks.head_sha, checks.result, material, run['id']))
    if repair_id is None:
        tx.execute('UPDATE subjects SET checks_id=? WHERE run_id=? AND id=?', (material, run['id'], job['id']))
    else:
        tx.execute('UPDATE run_repairs SET checks_id=? WHERE run_id=? AND id=?', (material, run['id'], repair_id))
    return material


def consume(tx: Transaction, state: DeliveryState, run: sqlite3.Row,
            job: sqlite3.Row | Mapping[str, Any], checks: Checks, material: str) -> bool:
    """After convergence, retain evidence and fail into CI without renewing budgets."""
    tx.execute("DELETE FROM waits WHERE run_id=? AND subject=? AND kind='required_checks'", (run['id'], job['id']))
    if checks.result != 'fail':
        return True
    tx.execute("UPDATE subjects SET phase='development',purpose='ci',repair_source='required_checks',ci_failure=?,last_failure=?,grant_id=NULL,integrated_sha=NULL,integrated_tree=NULL,version=version+1 WHERE run_id=? AND id=?",
               (material, material, run['id'], job['id']))
    state.release_integration(tx, job['id'])
    state.event(tx, job['id'], 'ci_failed', material, material)
    return False
