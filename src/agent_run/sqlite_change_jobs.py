"""Read and commit bounded Change Jobs and identity-only selections."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_change_job_archives import read_metadata, require_metadata, save_metadata
from agent_run.sqlite_change_job_codec import (
    history_rows, load_core, load_record, material, presence, present, put_material, save_core, save_record,
)
from agent_run.sqlite_change_job_schema import (
    ATTEMPT_FIELDS, CYCLE_DERIVED, GATE_FIELDS, HISTORY_FIELDS, JOB_FIELDS, RECORDS,
    ROOT_FIELDS, STRING_FIELDS, THREAD_FIELDS,
)
from agent_run.sqlite_change_job_schema import INDEXES as INDEXES, TABLES as TABLES
from agent_run.sqlite_change_job_gates import gate_projection, load_gate as _load_gate, save_gate as _save_gate
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_mutations import _same

FIELDS = frozenset(ROOT_FIELDS)
_ARCHIVES = {
    "retired_job_generations": "retired_job_generation", "retired_semantic_attempt_owners": "retired_attempt_owner",
    "retired_parent_job": "retired_parent", "retired_run_acceptance": "retired_acceptance", "retired_run_publication": "retired_publication",
}
_OWNERS = {"parent_job": "parent", "run_acceptance": "run_acceptance", "run_publication": "run_publication"}
_MISSING = object()
_HISTORIES = HISTORY_FIELDS | THREAD_FIELDS | {"review_budget_history", "semantic_attempt_history"}


def job_identity(owner_kind: str, subject: Mapping[str, Any], *, ticket_number: int | None = None) -> str:
    """Use replacement generations for change work and fixed Run containers."""
    if owner_kind == "ticket":
        number = ticket_number if ticket_number is not None else subject.get("ticket_number")
        if type(number) is not int:
            raise ValueError("Ticket Job identity requires its Ticket number")
        if "selection_reason" in subject and not subject.keys() - {"ticket_number", "selection_reason", "source_revision_changed"}:
            return f"ticket:{number}:selection"
        return f"ticket:{number}:generation:{subject.get('ticket_branch_generation', 1)}"
    prefix, field = {"parent": ("parent-only", "parent_generation"), "run_repair": ("run-repair", "repair_generation"),
                     "selected_ticket": ("active-ticket", "ticket_branch_generation")}.get(owner_kind, (owner_kind.replace("_", "-"), ""))
    generation = subject.get(field, 1) if field else 1
    if type(generation) is not int or generation < 1:
        raise ValueError("Change Job generation must be positive")
    return f"{prefix}:generation:{generation}"


def _save_history(c: sqlite3.Connection, run_id: str, job_id: str, field: str, value: Any, *, owner_id: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"Change Job {field} must be an array")
    c.execute("DELETE FROM work_job_archives WHERE run_id=? AND job_id=? AND history_kind=?", (run_id, job_id, field))
    total = 0
    for position, item in enumerate(value):
        if field in THREAD_FIELDS:
            if not isinstance(item, str):
                raise ValueError(f"Change Job {field} must contain Thread identities")
            evidence_id, thread_id = None, item
        else:
            evidence_id, thread_id = put_material(c, run_id, owner_id, field, item), None
        size = len(encode_json(item).encode())
        total += size
        c.execute("INSERT INTO work_job_archives VALUES (?,?,?,?,?,?,?)", (run_id, job_id, field, position, evidence_id, thread_id, size))
    save_metadata(c, run_id, job_id, field, total + 2 + max(len(value) - 1, 0), len(value))


def _load_history(c: sqlite3.Connection, run_id: str, job_id: str, field: str, *, owner_id: str) -> list[Any]:
    result, total = [], 0
    for evidence_id, thread, size in history_rows(c, run_id, job_id, field, owner_id=owner_id):
        result.append(thread if evidence_id is None else material(c, run_id, owner_id, field, evidence_id))
        total += size
    require_metadata(c, run_id, job_id, field, total, len(result))
    return result


def _save_completed_repairs(c: sqlite3.Connection, run_id: str, job_id: str, records: Any) -> None:
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise ValueError("Completed Repairs must contain completion records")
    container = "completed_repair_jobs:" + job_id
    c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, container))
    total = 0
    for position, item in enumerate(records):
        encoded = encode_json(item).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        total += len(encoded)
        identity = job_id + ":completed-repair:" + digest
        origin = c.execute("SELECT job_id FROM work_jobs WHERE run_id=? AND owner_kind='run_repair' AND value_repair_branch=? AND value_candidate_sha=?",
                           (run_id, item.get("repair_branch"), item.get("candidate_sha"))).fetchone()
        if not c.execute("SELECT 1 FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity)).fetchone():
            _save_owner(c, run_id, identity, "completed_repair", origin[0] if origin else job_id, item,
                        evidence_owner_id=origin[0] if origin else identity, budget_owner_id=job_id)
        _slot(c, run_id, container, str(position), position, identity)
    save_metadata(c, run_id, job_id, "completed_repair_jobs", total + 2 + max(len(records) - 1, 0), len(records))


def _load_owner(c: sqlite3.Connection, run_id: str, job_id: str, *, include_history: bool = True,
                omitted_history: set[str] | None = None) -> dict[str, Any]:
    from agent_run.sqlite_work_execution import load_attempts
    job, meta = load_core(c, run_id, job_id)
    keys, owner = meta["keys"], meta["owner_id"]
    for field in RECORDS.keys() & keys:
        job[field] = load_record(c, run_id, job_id, field, owner_id=owner)
    omitted = omitted_history or set()
    for field in ((HISTORY_FIELDS | THREAD_FIELDS) & keys) - omitted:
        if include_history:
            if field == "completed_repair_jobs":
                job[field] = [_load_owner(c, run_id, row[0]) for row in c.execute("SELECT job_id FROM work_job_slots WHERE run_id=? AND container=? ORDER BY position", (run_id, "completed_repair_jobs:" + job_id))]
            else:
                job[field] = _load_history(c, run_id, job_id, field, owner_id=owner)
    for field in STRING_FIELDS & keys:
        job[field] = [row[0] for row in c.execute("SELECT text_value FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind='job' AND list_name=? ORDER BY position", (run_id, job_id, field))]
    job.update((field, value) for field, value in load_attempts(
        c, run_id, job_id, include_history=include_history and "semantic_attempt_history" not in omitted).items() if field in keys)
    if include_history and "review_budget_history" in keys and "review_budget_history" not in omitted:
        snapshots, total = [], 0
        for position, (window, identity, size) in enumerate(c.execute(
                "SELECT window,snapshot_job_id,logical_bytes FROM work_job_windows WHERE run_id=? AND job_id=? ORDER BY window", (run_id, job_id)), 1):
            if window != position or type(size) is not int or size < 0:
                raise ValueError("Invalid Change Job budget history size or window")
            snapshots.append(_load_owner(c, run_id, identity))
            total += size
        require_metadata(c, run_id, job_id, "review_budget_history", total, len(snapshots))
        job["review_budget_history"] = snapshots
    if "repair_integration_budgets" in keys:
        job["repair_integration_budgets"] = {str(generation): _load_owner(c, run_id, identity)
            for generation, identity in c.execute("SELECT repair_generation,budget_job_id FROM work_job_repair_budgets WHERE run_id=? AND job_id=? ORDER BY repair_generation", (run_id, job_id))}
    if "repair_job" in keys:
        repair_id = meta["repair_job_id"]
        job["repair_job"] = _load_owner(c, run_id, repair_id, include_history=include_history) if repair_id is not None else None
    cycle = job.get("repair_cycle")
    if isinstance(cycle, dict):
        repair = job.get("repair_job")
        if isinstance(repair, dict) and cycle.get("generation") == repair.get("repair_generation"):
            cycle.update(code_modification_attempts=int(repair.get("modification_attempts", 0)), validation_attempts=int(repair.get("validation_attempts", 0)))
            for source, target in (("development_thread_id", "development_thread_id"), ("repair_checkout", "worktree")):
                if isinstance(repair.get(source), str):
                    cycle[target] = repair[source]
            job["code_modification_attempts"] = job["modification_attempts"] = int(repair.get("modification_attempts", 0))
        elif meta["cycle_evidence_id"] is not None:
            cycle.update(material(c, run_id, owner, "repair_cycle.completed", meta["cycle_evidence_id"]))
    _load_gate(c, run_id, job_id, job, meta["stage"])
    return job


def _save_owner(c: sqlite3.Connection, run_id: str, job_id: str, kind: str, owner_key: str, job: Mapping[str, Any],
                baseline: Mapping[str, Any] | None = None, *, evidence_owner_id: str | None = None, budget_owner_id: str | None = None) -> None:
    from agent_run.sqlite_work_execution import save_attempts
    if set(job) - JOB_FIELDS:
        raise ValueError(f"Unsupported {kind} Job fields: {sorted(set(job) - JOB_FIELDS)}")
    existing = c.execute("SELECT presence,stage,evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    # A replacement generation has its own row. Values equal to its former
    # generation still need columns, child records and archive summaries here.
    old = (baseline or {}) if existing is not None else {}
    owner_id = evidence_owner_id or (existing[2] if existing else job_id)
    if existing is None:
        c.execute("INSERT INTO work_jobs (run_id,job_id,owner_kind,owner_key,revision,presence,evidence_owner_id,budget_owner_id) VALUES (?,?,?,?,0,?,?,?)",
                  (run_id, job_id, kind, owner_key, presence((), JOB_FIELDS), owner_id, budget_owner_id or job_id))
    stored_keys = present(existing[0], JOB_FIELDS) if existing else set()
    for field in (job.keys() & _HISTORIES) - old.keys():
        if field in stored_keys:
            raise ValueError(f"cannot replace unloaded Job history: {field}")
    save_attempts(c, run_id, job_id, dict(job), dict(old))
    changed = {key for key in job.keys() | old.keys() if not _same(job.get(key, _MISSING), old.get(key, _MISSING))}
    current, meta = load_core(c, run_id, job_id, fields=changed)
    _load_gate(c, run_id, job_id, current, meta["stage"])
    if existing is not None and baseline is not None:
        if ("phase" in changed and "phase" in old and not _same(current.get("phase"), old["phase"])
                and not _same(current.get("phase"), job.get("phase"))):
            raise ValueError(f"Change Job {job_id} changed concurrently: phase")
        for key in changed - GATE_FIELDS - ATTEMPT_FIELDS:
            if key not in old or key not in current:
                continue
            if not _same(current[key], old[key]) and not _same(current[key], job.get(key, _MISSING)):
                raise ValueError(f"Change Job {job_id} changed concurrently: {key}")
    # Historical fields absent from both sparse views stay untouched.
    removed = old.keys() - job.keys()
    keys = (meta["keys"] - removed) | job.keys()
    for key in changed:
        if key in job:
            current[key] = job[key]
        else:
            current.pop(key, None)
    # Presence includes child records without embedding their mutable values.
    for key in keys - current.keys():
        current[key] = job.get(key)
    repair_id = meta["repair_job_id"]
    if "repair_job" in changed:
        repair = job.get("repair_job")
        repair_id = None
        if isinstance(repair, Mapping):
            repair_id = job_identity("run_repair", repair) if kind == "run_acceptance" else job_id + ":repair"
            old_repair = old.get("repair_job")
            _save_owner(c, run_id, repair_id, "run_repair" if kind == "run_acceptance" else "budget_window", owner_key,
                        repair, old_repair if isinstance(old_repair, Mapping) else None, evidence_owner_id=owner_id if kind != "run_acceptance" else None,
                        budget_owner_id=job_id)
    for field in RECORDS.keys() & changed:
        value = job.get(field)
        previous_value = old.get(field)
        if field == "repair_cycle" and isinstance(value, Mapping):
            value = {key: item for key, item in value.items() if key not in CYCLE_DERIVED}
            if isinstance(previous_value, Mapping):
                previous_value = {key: item for key, item in previous_value.items() if key not in CYCLE_DERIVED}
        if existing is not None and field in old:
            stored = load_record(c, run_id, job_id, field, owner_id=owner_id)
            if not _same(stored, previous_value) and not _same(stored, value):
                raise ValueError(f"Change Job {job_id} changed concurrently: {field}")
        save_record(c, run_id, job_id, field, value, owner_id=owner_id)
    for field in (HISTORY_FIELDS | THREAD_FIELDS) & changed:
        if field in job:
            if field == "completed_repair_jobs":
                _save_completed_repairs(c, run_id, job_id, job[field])
            else:
                _save_history(c, run_id, job_id, field, job[field], owner_id=owner_id)
    for field in STRING_FIELDS & changed:
        c.execute("DELETE FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind='job' AND list_name=?", (run_id, job_id, field))
        value = job.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"Change Job {field} must contain strings")
        c.executemany("INSERT INTO work_job_record_strings VALUES (?,?,?,?,?,?)", ((run_id, job_id, "job", field, position, item) for position, item in enumerate(value)))
    if "review_budget_history" in changed and "review_budget_history" in job:
        snapshots = job["review_budget_history"]
        if not isinstance(snapshots, list):
            raise ValueError("review_budget_history must be an array")
        prior_size, prior_count = read_metadata(c, run_id, job_id, "review_budget_history", required="review_budget_history" in stored_keys)
        total = prior_size - 2 - max(prior_count - 1, 0)
        if len(snapshots) < prior_count:
            raise ValueError("Change Job budget history cannot be removed")
        for position, snapshot in enumerate(snapshots, 1):
            identity = f"{job_id}:budget-window:{position}"
            if position <= prior_count:
                previous = old.get("review_budget_history")
                if isinstance(previous, list) and position <= len(previous) and _same(snapshot, previous[position - 1]):
                    continue
                if not _same(snapshot, _load_owner(c, run_id, identity)):
                    raise ValueError("Historical budget evidence cannot be changed")
                continue
            _save_owner(c, run_id, identity, "budget_window", owner_key, snapshot, evidence_owner_id=owner_id)
            size = len(encode_json(snapshot).encode())
            total += size
            c.execute("INSERT INTO work_job_windows VALUES (?,?,?,?,?)", (run_id, job_id, position, identity, size))
        save_metadata(c, run_id, job_id, "review_budget_history", total + 2 + max(len(snapshots) - 1, 0), len(snapshots))
    if "repair_integration_budgets" in changed:
        windows = job.get("repair_integration_budgets", {})
        if not isinstance(windows, Mapping):
            raise ValueError("repair_integration_budgets must be an object")
        c.execute("DELETE FROM work_job_repair_budgets WHERE run_id=? AND job_id=?", (run_id, job_id))
        for generation, value in windows.items():
            identity = f"{job_id}:repair-budget:{generation}"
            _save_owner(c, run_id, identity, "repair_budget", str(generation), value, evidence_owner_id=owner_id, budget_owner_id=job_id)
            c.execute("INSERT INTO work_job_repair_budgets VALUES (?,?,?,?)", (run_id, job_id, int(generation), identity))
    cycle = job.get("repair_cycle")
    cycle_evidence = meta["cycle_evidence_id"]
    if isinstance(cycle, Mapping) and repair_id is None:
        cycle_evidence = put_material(c, run_id, owner_id, "repair_cycle.completed", {key: value for key, value in cycle.items() if key in CYCLE_DERIVED})
    repair = job.get("repair_job")
    if (repair_id is not None and kind == "run_acceptance" and isinstance(cycle, Mapping)
            and isinstance(repair, Mapping) and cycle.get("generation") == repair.get("repair_generation")):
        current.pop("code_modification_attempts", None)
        current.pop("modification_attempts", None)
    # While a Repair is current its counters are derived. Once it retires,
    # those same values become the completed cycle's fixed facts; their
    # absence from the SQL core still requires writing them even when the
    # caller's derived baseline had the same visible values.
    changed.update(current.keys() - meta["keys"])
    stage = _save_gate(c, run_id, job_id, job, old, meta["stage"])
    save_core(c, run_id, job_id, current, owner_id=owner_id, stage=stage, budget_owner_id=budget_owner_id or meta["budget_owner_id"] or job_id,
              repair_job_id=repair_id, cycle_evidence_id=cycle_evidence, changed=changed)


def _slot(c: sqlite3.Connection, run_id: str, field: str, key: str, position: int, identity: str) -> None:
    c.execute("INSERT INTO work_job_slots VALUES (?,?,?,?,?) ON CONFLICT(run_id,container,slot) DO UPDATE SET position=excluded.position,job_id=excluded.job_id",
              (run_id, field, key, position, identity))


def append_archive(c: sqlite3.Connection, run_id: str, field: str, items: list[Any]) -> None:
    if field not in _ARCHIVES:
        raise ValueError("Unsupported Change Job archive")
    position = c.execute("SELECT COALESCE(MAX(position)+1,0) FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field)).fetchone()[0]
    for offset, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError("Archived Job must be an object")
        slot = position + offset
        subject = str(item.get("work_subject", field))
        generation = item.get("generation") or item.get("ticket_branch_generation") or item.get("parent_generation") or item.get("repair_generation") or 1
        identity = f"archive:{field}:{slot}:{subject}:generation:{generation}"
        origin_kind = str(item.get("owner_kind") or ("ticket" if subject.startswith("ticket:") else "parent" if subject.startswith("parent-only:") else "run_repair" if subject.startswith("run-repair:") else ""))
        origin: str | None = None
        if origin_kind in {"ticket", "parent", "run_repair", "run_acceptance", "run_publication"}:
            origin_data = dict(item)
            field_name = {"ticket": "ticket_branch_generation", "parent": "parent_generation", "run_repair": "repair_generation"}.get(origin_kind)
            if field_name is not None:
                origin_data[field_name] = generation
            number = int(subject.split(":", 1)[1]) if origin_kind == "ticket" else None
            candidate = job_identity(origin_kind, origin_data, ticket_number=number)
            if c.execute("SELECT 1 FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, candidate)).fetchone():
                origin = candidate
        _save_owner(c, run_id, identity, _ARCHIVES[field], str(slot), item, evidence_owner_id=origin)
        _slot(c, run_id, field, str(slot), slot, identity)
    row = c.execute("SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    keys = present(row[0], ROOT_FIELDS) if row else set()
    c.execute("INSERT INTO work_job_layout VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET presence=excluded.presence",
              (run_id, presence(keys | {field}, ROOT_FIELDS), row[1] if row else None))


def save_jobs(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any], *,
              fields: set[str] | None = None, keyed_changes: Mapping[str, Any] | None = None) -> None:
    selected = FIELDS if fields is None else FIELDS & fields
    row = c.execute("SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    keys, active_id = (present(row[0], ROOT_FIELDS), row[1]) if row else (set(), None)
    for field in ("ticket_jobs", "parent_job", "run_acceptance", "run_publication", *_ARCHIVES):
        if field not in selected:
            continue
        if field not in state and field not in baseline:
            continue
        if field in state:
            keys.add(field)
        else:
            keys.discard(field)
        if field == "active_ticket_job":
            continue
        incoming, previous = state.get(field), baseline.get(field)
        if field in _ARCHIVES:
            if _same(incoming, previous):
                continue
            values = incoming if isinstance(incoming, list) else [incoming] if isinstance(incoming, Mapping) else []
            if isinstance(previous, list) and _same(values[:len(previous)], previous):
                append_archive(c, run_id, field, values[len(previous):])
            else:
                c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field))
                append_archive(c, run_id, field, values)
            continue
        if field == "ticket_jobs":
            jobs, old_jobs = incoming or {}, previous or {}
            changed = set(keyed_changes[field]) if keyed_changes is not None and field in keyed_changes else set(jobs) | set(old_jobs)
            for key in changed:
                job, old = jobs.get(key), old_jobs.get(key)
                if _same(job, old):
                    continue
                if isinstance(job, Mapping):
                    identity = job_identity("ticket", job, ticket_number=int(key))
                    _save_owner(c, run_id, identity, "ticket", str(key), job, old if isinstance(old, Mapping) else None)
                    _slot(c, run_id, field, str(key), int(key), identity)
                else:
                    c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=? AND slot=?", (run_id, field, str(key)))
            continue
        if isinstance(incoming, Mapping):
            kind = _OWNERS[field]
            identity = job_identity(kind, incoming)
            _save_owner(c, run_id, identity, kind, field, incoming, previous if isinstance(previous, Mapping) else None)
            _slot(c, run_id, field, "current", 0, identity)
        else:
            c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field))
    if "active_ticket_job" in selected and ("active_ticket_job" in state or "active_ticket_job" in baseline):
        if "active_ticket_job" in state:
            keys.add("active_ticket_job")
        else:
            keys.discard("active_ticket_job")
        active = state.get("active_ticket_job")
        active_id = None
        if isinstance(active, Mapping):
            number = active.get("ticket_number")
            kind = "ticket" if type(number) is int else "selected_ticket"
            active_id = job_identity(kind, active)
            jobs = state.get("ticket_jobs")
            stored = jobs.get(str(number)) if isinstance(jobs, Mapping) else None
            old_active = baseline.get("active_ticket_job")
            if stored is None or not _same(active, old_active) and not _same(active, stored):
                _save_owner(c, run_id, active_id, kind, str(number) if number is not None else "active_ticket_job", active,
                            old_active if isinstance(old_active, Mapping) else None)
    c.execute("INSERT INTO work_job_layout VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET presence=excluded.presence,active_job_id=excluded.active_job_id",
              (run_id, presence(keys, ROOT_FIELDS), active_id))


def selected_ticket_number(c: sqlite3.Connection, run_id: str) -> int | None:
    """Read selection identity without materializing the selected Job."""
    row = c.execute("SELECT l.active_job_id,j.owner_kind,j.owner_key,j.value_ticket_number FROM work_job_layout l "
                    "LEFT JOIN work_jobs j ON j.run_id=l.run_id AND j.job_id=l.active_job_id WHERE l.run_id=?", (run_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    if row[1] == "selected_ticket":
        return None
    if row[1] != "ticket":
        raise ValueError("Invalid selected Change Job identity")
    if type(row[3]) is int:
        return row[3]
    try:
        number = int(row[2])
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid selected Ticket owner") from error
    if str(number) != row[2]:
        raise ValueError("Invalid selected Ticket owner")
    return number


def project_current_gates(c: sqlite3.Connection, run_id: str, state: dict[str, Any], *, fields: set[str],
                          keyed_changes: Mapping[str, set[str]] | None = None) -> tuple[set[int], set[int]]:
    """Publish only committed gates for the owners touched by this save."""
    owners: list[tuple[str, dict[str, Any]]] = []
    if "ticket_jobs" in fields:
        tickets = state.get("ticket_jobs", {})
        for key in (keyed_changes or {}).get("ticket_jobs", tickets.keys()):
            job = tickets.get(key)
            if isinstance(job, dict):
                owners.append((job_identity("ticket", job, ticket_number=int(key)), job))
    for field, kind in _OWNERS.items():
        job = state.get(field)
        if field not in fields or not isinstance(job, dict):
            continue
        identity = job_identity(kind, job)
        owners.append((identity, job))
        repair = job.get("repair_job") if field == "run_acceptance" else None
        if isinstance(repair, dict):
            linked = c.execute("SELECT repair_job_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity)).fetchone()
            if linked is not None and linked[0] is not None:
                owners.append((linked[0], repair))
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        selected = c.execute("SELECT active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
        if selected is not None and selected[0] is not None and (
            "active_ticket_job" in fields or any(identity == selected[0] for identity, _ in owners)
        ):
            owners.append((selected[0], active))
    changed: set[int] = set()
    revealed: set[int] = set()
    projections: dict[str, dict[str, Any]] = {}
    for identity, job in owners:
        if identity not in projections:
            projections[identity] = gate_projection(c, run_id, identity)
        current = projections[identity]
        if _same({key: job[key] for key in GATE_FIELDS if key in job}, current):
            continue
        if any(not _same(job.get(key), current.get(key)) for key in ("phase", "blocked_reason")):
            revealed.add(id(job))
        for key in GATE_FIELDS:
            job.pop(key, None)
        job.update(current)
        changed.add(id(job))
    return changed, revealed


def load_jobs(c: sqlite3.Connection, run_id: str, *, ticket_numbers: set[int] | None = None,
              include_history: bool = True, fields: set[str] | None = None,
              archive_scope: Mapping[str, tuple[int, int]] | None = None) -> dict[str, Any]:
    if fields is not None and not fields & FIELDS:
        return {}
    row = c.execute("SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    selected = present(row[0], ROOT_FIELDS) & (FIELDS if fields is None else fields)
    result: dict[str, Any] = {}
    loaded: dict[str, dict[str, Any]] = {}
    for field in selected - {"active_ticket_job"}:
        if field in _ARCHIVES and not include_history:
            continue
        slots = c.execute("SELECT slot,job_id FROM work_job_slots WHERE run_id=? AND container=? ORDER BY position", (run_id, field))
        values: Any = {} if field == "ticket_jobs" else [] if field in _ARCHIVES and field != "retired_parent_job" else None
        for key, identity in slots:
            if field == "ticket_jobs" and ticket_numbers is not None and include_history and int(key) not in ticket_numbers:
                continue
            history = include_history or field != "ticket_jobs" or ticket_numbers is None or int(key) in ticket_numbers
            path = (field, key) if field == "ticket_jobs" else (field,)
            omitted = {name for name in _HISTORIES if encode_json((*path, name)) in (archive_scope or {})}
            value = _load_owner(c, run_id, identity, include_history=history, omitted_history=omitted)
            loaded[identity] = value
            if isinstance(values, dict):
                values[key] = value
            elif isinstance(values, list):
                values.append(value)
            else:
                values = value
        result[field] = values
    if "active_ticket_job" in selected:
        identity = row[1]
        omitted = {name for name in _HISTORIES if encode_json(("active_ticket_job", name)) in (archive_scope or {})}
        result["active_ticket_job"] = (loaded.get(identity) or _load_owner(
            c, run_id, identity, include_history=include_history, omitted_history=omitted)) if identity is not None else None
    return result


def read_job_archives(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    from agent_run.sqlite_work_execution import attempt_history_metadata
    from agent_run.sqlite_job_history import FIELDS as ARCHIVE_FIELDS
    result = {}
    owners = [(('active_ticket_job',), state.get("active_ticket_job"))] + [
        (("ticket_jobs", key), value) for key, value in state.get("ticket_jobs", {}).items()]
    for path, job in owners:
        if not isinstance(job, Mapping):
            continue
        number = job.get("ticket_number", int(path[1]) if len(path) == 2 else None)
        identity = job_identity("ticket" if type(number) is int else "selected_ticket", job, ticket_number=number)
        row = c.execute("SELECT presence,evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity)).fetchone()
        if row is None:
            continue
        for field in (present(row[0], JOB_FIELDS) & ARCHIVE_FIELDS) - job.keys():
            if field == "semantic_attempt_history":
                size, count = attempt_history_metadata(c, run_id, identity)
            else:
                size, count = read_metadata(c, run_id, identity, field)
            result[encode_json((*path, field))] = size, count
    return result


def read_related_attempts(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], archives: Mapping[str, tuple[int, int]]) -> dict[str, Any]:
    from agent_run.sqlite_work_execution import load_completed_publication_attempt
    result = {}
    owners = [(('active_ticket_job',), state.get("active_ticket_job"))] + [
        (("ticket_jobs", key), value) for key, value in state.get("ticket_jobs", {}).items()]
    for path, job in owners:
        if not isinstance(job, Mapping) or job.get("publication_operation_retry") is None or encode_json((*path, "semantic_attempt_history")) not in archives:
            continue
        pending = job.get("pending_semantic_attempt")
        if isinstance(pending, Mapping) and pending.get("role") == "publication":
            continue
        number = job.get("ticket_number", int(path[1]) if len(path) == 2 else None)
        identity = job_identity("ticket" if type(number) is int else "selected_ticket", job, ticket_number=number)
        ordinal = job.get("publication_attempts")
        if type(ordinal) is not int:
            continue
        attempt = load_completed_publication_attempt(c, run_id, identity, ordinal)
        if attempt is not None:
            result[encode_json(path)] = attempt
    return result
