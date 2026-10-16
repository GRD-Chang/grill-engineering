"""准确 Repair 代次的额度查询与增量保存；未读旧代次保留原关系。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from agent_run.sqlite_mutations import _same
from agent_run.sqlite_query_rows import bounded_query
from agent_run.work_projection import RepairBudgetProjection, RepairBudgetScope

FIELD = "repair_integration_budgets"


def current_generations(job: Mapping[str, Any]) -> frozenset[str]:
    repair = job.get("repair_job")
    if not isinstance(repair, Mapping):
        return frozenset()
    generation = repair.get("repair_generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("Current Repair budget requires a positive generation")
    return frozenset({str(generation)})


def load_budgets(
    c: sqlite3.Connection, run_id: str, job_id: str, *, generations: frozenset[str] | None = None,
) -> dict[str, Any]:
    from agent_run.sqlite_change_job_queries import _load_owner

    rows: Iterable[tuple[Any, ...]]
    if generations is None:
        rows = bounded_query(c,
            "SELECT repair_generation,budget_job_id FROM work_job_repair_budgets "
            "WHERE run_id=? AND job_id=? ORDER BY repair_generation", (run_id, job_id),
        )
    else:
        selected = []
        for generation in sorted(generations):
            row = bounded_query(c,
                "SELECT repair_generation,budget_job_id FROM work_job_repair_budgets "
                "WHERE run_id=? AND job_id=? AND repair_generation=?", (run_id, job_id, int(generation)),
            ).fetchone()
            if row is None:
                raise ValueError("Missing exact Repair integration budget")
            selected.append(row)
        rows = selected
    result = {}
    for generation, identity in rows:
        if type(generation) is not int or generation < 1 or identity != f"{job_id}:repair-budget:{generation}":
            raise ValueError("Invalid Repair integration budget identity")
        result[str(generation)] = _load_owner(c, run_id, identity)
    return result


def save_budgets(
    c: sqlite3.Connection, run_id: str, job_id: str, job: Mapping[str, Any],
    baseline: Mapping[str, Any], *, owner_id: str,
) -> None:
    from agent_run.sqlite_change_jobs import _save_owner

    windows, previous = job.get(FIELD, {}), baseline.get(FIELD, {})
    scope = windows.scope if isinstance(windows, RepairBudgetProjection) else None
    previous_scope = previous.scope if isinstance(previous, RepairBudgetProjection) else None
    if scope is not None and (scope.run_id != run_id or scope.job_id != job_id or previous_scope != scope):
        raise ValueError("Repair budget scope does not match its exact owner and baseline")
    if previous_scope is not None and scope != previous_scope:
        raise ValueError("Cannot replace unread Repair budgets without their query scope")
    if not isinstance(windows, Mapping) or not isinstance(previous, Mapping):
        raise ValueError("repair_integration_budgets must be an object")
    if previous.keys() - windows.keys() or scope is not None and FIELD not in job:
        raise ValueError("Cannot remove recorded Repair integration budgets")
    for generation in windows.keys() | previous.keys():
        if not isinstance(generation, str) or not generation.isdecimal() or int(generation) < 1 or str(int(generation)) != generation:
            raise ValueError("Repair integration budget generation must be canonical and positive")
        value, old = windows[generation], previous.get(generation)
        if generation in previous and _same(value, old):
            continue
        if not isinstance(value, Mapping):
            raise ValueError("Repair integration budget must be an object")
        relation = bounded_query(c,
            "SELECT budget_job_id FROM work_job_repair_budgets WHERE run_id=? AND job_id=? AND repair_generation=?",
            (run_id, job_id, int(generation)),
        ).fetchone()
        identity = f"{job_id}:repair-budget:{generation}"
        if relation is not None and (relation[0] != identity or scope is not None and generation not in previous):
            raise ValueError("Cannot overwrite unread Repair integration budget")
        _save_owner(c, run_id, identity, "repair_budget", generation, value,
                    old if isinstance(old, Mapping) else None, evidence_owner_id=owner_id, budget_owner_id=job_id)
        c.execute("INSERT INTO work_job_repair_budgets VALUES (?,?,?,?) "
                  "ON CONFLICT(run_id,job_id,repair_generation) DO UPDATE SET budget_job_id=excluded.budget_job_id",
                  (run_id, job_id, int(generation), identity))


def narrow_saved_scope(job: Mapping[str, Any], run_id: str, job_id: str) -> None:
    """新一代保存后，返回与下一次当前查询相同的范围。"""
    windows = job.get(FIELD)
    if not isinstance(windows, RepairBudgetProjection):
        return
    generations = current_generations(job)
    if not generations <= windows.keys():
        raise ValueError("Missing current Repair integration budget after save")
    for generation in windows.keys() - generations:
        del windows[generation]
    windows.scope = RepairBudgetScope(run_id, job_id, generations)
