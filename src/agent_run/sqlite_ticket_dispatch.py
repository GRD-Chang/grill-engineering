"""Explicit read-only Ticket dispatch facts, never a work-save baseline."""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from agent_run import sqlite_run_records as roots
from agent_run.semantic_attempt import require_semantic_attempt_record
from agent_run.sqlite_change_job_codec import (
    _assign, _check_record_size, _shape, _typed, load_core, present,
)
from agent_run.sqlite_change_job_gates import load_gate
from agent_run.sqlite_change_job_schema import RECORDS, ROOT_FIELDS, column, record_table
from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_work_evidence import get_evidence
from agent_run.sqlite_work_execution_attempts import _version_row
from agent_run.sqlite_work_execution_schema import (
    ATTEMPT_EVIDENCE, ATTEMPT_FIELDS, ATTEMPT_IDENTITY, ATTEMPT_SCALARS,
    present_fields, restore_scalars,
)
from agent_run.state_limits import QueryLimitError
from agent_run.ticket_phase import TicketPhase
from agent_run.work_facts import TicketDispatchJob

_CORE_FIELDS = {"ticket_number", "ticket_branch_generation", "integration_resume_phase", "pending_review_result"}
_PHASES = frozenset(phase.value for phase in TicketPhase)


def _pending_ready(
    c: sqlite3.Connection, run_id: str, job_id: str, number: int, generation: int,
    *, included: bool,
) -> bool:
    root = bounded_query(c, "SELECT pending_present,pending_attempt_id FROM work_job_attempt_roots "
                         "WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if root is None:
        if included:
            raise ValueError("Missing dispatch Semantic Attempt root")
        return False
    present_flag, attempt_id = root
    if type(present_flag) is not int or present_flag not in (0, 1) or bool(present_flag) != included:
        raise ValueError("Invalid dispatch pending Attempt presence")
    if not included or attempt_id is None:
        if not included and attempt_id is not None:
            raise ValueError("Unexpected dispatch pending Attempt identity")
        return False
    if not isinstance(attempt_id, str):
        raise ValueError("Invalid dispatch pending Attempt identity")
    identity = bounded_query(c, "SELECT v.version_id,a.job_id," + ",".join("a." + key for key in ATTEMPT_IDENTITY)
        + " FROM work_attempt_current v LEFT JOIN work_attempts a USING(run_id,attempt_id) "
        "WHERE v.run_id=? AND v.attempt_id=?", (run_id, attempt_id)).fetchone()
    if identity is None or identity[1] != job_id:
        raise ValueError("Dispatch pending Attempt belongs to another Job")
    version = _version_row(c, run_id, attempt_id, identity[0])
    fields = present_fields(version["presence"], ATTEMPT_FIELDS)
    attempt: dict[str, Any] = {"attempt_id": attempt_id}
    restore_scalars(attempt, ATTEMPT_IDENTITY, identity[2:], fields)
    restore_scalars(attempt, ATTEMPT_SCALARS, tuple(version[key] for key in ATTEMPT_SCALARS), fields)
    for field in ATTEMPT_EVIDENCE:
        reference = version[f"{field}_evidence_id"]
        if field not in fields and reference is not None:
            raise ValueError("Unexpected dispatch Semantic Attempt evidence")
        if field == "returned_result" and field in fields:
            # Read this necessary return instead of treating a null, dangling
            # or damaged reference as readiness. Other report bodies stay out
            # of the dispatch query, including the Attempt's Review Context.
            attempt[field] = None if reference is None else get_evidence(
                c, run_id, reference, owner_id="attempt:" + attempt_id, kind=field,
            )
    require_semantic_attempt_record(attempt, expected_status="pending")
    if attempt["work_subject"] != f"ticket:{number}" or attempt["generation"] != generation:
        raise ValueError("Dispatch pending Attempt has the wrong Ticket or generation")
    return "returned_result" in attempt


def _external_wait(c: sqlite3.Connection, run_id: str, job_id: str) -> dict[str, Any] | None:
    schema = RECORDS["external_wait"]
    columns = [column(path) for path, kind in schema.items() if kind != "EVIDENCE"]
    _check_record_size(c, run_id, job_id, "external_wait", [column(path) for path in schema])
    row = bounded_query(c, "SELECT presence,nulls," + ",".join(columns) + " FROM "
        + record_table("external_wait") + " WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None:
        return None
    keys, nulls = present(row[0], _shape(schema)), present(row[1], _shape(schema))
    if not nulls <= keys:
        raise ValueError("Invalid dispatch external wait presence")
    result: dict[str, Any] = {}
    for path in sorted(keys - schema.keys(), key=lambda key: key.count(".")):
        _assign(result, path, None if path in nulls else {})
    values = iter(row[2:])
    for path, kind in schema.items():
        if kind == "EVIDENCE":
            continue
        value = next(values)
        if path in keys:
            _assign(result, path, None if path in nulls else _typed(value, kind, path))
    if result.get("status") not in {"waiting_checks", "waiting_external", "waiting_merge"}:
        raise ValueError("Invalid dispatch external wait status")
    window = result.get("supervision_window")
    if not isinstance(window, dict):
        raise ValueError("Missing dispatch external wait window")
    if (not isinstance(window.get("identity"), str) or not window["identity"]
            or window.get("kind") not in {"required_checks", "github_convergence"}):
        raise ValueError("Invalid dispatch external wait identity")
    for key in ("started_at", "deadline", "next_retry_at"):
        value = window.get(key)
        if key != "next_retry_at" or value is not None:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("Invalid dispatch external wait clock")
    return result


def _jobs(c: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    layout = bounded_query(c, "SELECT presence FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    if layout is None or "ticket_jobs" not in present(layout[0], ROOT_FIELDS):
        raise ValueError("Missing dispatch Ticket Job layout")
    result: dict[str, Any] = {}
    slots = bounded_query(c, "SELECT s.slot,s.job_id,j.owner_kind,j.owner_key FROM work_job_slots s "
        "LEFT JOIN work_jobs j USING(run_id,job_id) WHERE s.run_id=? AND s.container='ticket_jobs' "
        "ORDER BY s.position", (run_id,))
    for key, job_id, kind, owner_key in slots:
        if (not isinstance(key, str) or not key.isdecimal() or int(key) < 1 or str(int(key)) != key
                or kind != "ticket" or owner_key != key or key in result):
            raise ValueError("Invalid dispatch Ticket Job slot")
        job, metadata = load_core(c, run_id, job_id, fields=_CORE_FIELDS)
        number = int(key)
        if type(job.get("ticket_number")) is not int or job["ticket_number"] != number:
            raise ValueError("Invalid dispatch Ticket Job number")
        generation = job.get("ticket_branch_generation", 1)
        if type(generation) is not int or generation < 1 or metadata["owner_id"] != job_id:
            raise ValueError("Invalid dispatch Ticket Job generation or owner")
        selection = metadata["keys"] <= {"ticket_number", "selection_reason", "source_revision_changed"}
        expected = f"ticket:{number}:selection" if selection else f"ticket:{number}:generation:{generation}"
        if job_id != expected:
            raise ValueError("Invalid dispatch Ticket Job identity")
        load_gate(c, run_id, job_id, job, metadata["stage"])
        if "phase" in job and job["phase"] not in _PHASES:
            raise ValueError("Invalid dispatch Ticket Job phase")
        review_ready = isinstance(job.pop("pending_review_result", None), dict)
        returned = _pending_ready(c, run_id, job_id, number, generation,
                                  included="pending_semantic_attempt" in metadata["keys"])
        if "external_wait" in metadata["keys"]:
            job["external_wait"] = _external_wait(c, run_id, job_id)
        result[key] = TicketDispatchJob(job, program_ready=returned or review_ready or job.get("phase") == "committing_candidate")
    return result


def load(c: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Read one real snapshot for selecting work; no mutable baseline is made."""
    from agent_run.state_contract import (
        _require_base, _require_parent, _require_ticket_graph, raise_materials_error,
        require_currentness_boundaries, require_query_state_current,
    )
    from agent_run.ticket_work import require_queue

    state = roots.load(c, run_id)
    try:
        require_query_state_current(state)
        _require_parent(state["parent"])
        _require_base(state["base"])
        _require_ticket_graph(state["ticket_graph"])
        require_currentness_boundaries(state)
        state["ticket_jobs"] = _jobs(c, run_id)
        tickets = state["ticket_graph"]["tickets"]
        if state["ticket_jobs"].keys() - tickets.keys():
            raise ValueError("Dispatch Ticket Job is outside the fixed graph")
        if not isinstance(state.get("status"), str) or not isinstance(state.get("frontier"), list):
            raise ValueError("Missing dispatch Run status or frontier")
        if any(type(number) is not int or str(number) not in tickets for number in state["frontier"]):
            raise ValueError("Invalid dispatch frontier Ticket")
        for field in ("ticket_pause_requests", "retired_ticket_generations"):
            if not isinstance(state.get(field), dict) or any(key not in tickets for key in state[field]):
                raise ValueError("Invalid dispatch Ticket control map")
        holder = state.get("ticket_integration_holder")
        if "ticket_integration_holder" not in state or holder is not None and (
            type(holder) is not int or str(holder) not in state["ticket_jobs"]
        ):
            raise ValueError("Invalid dispatch integration holder")
        if not isinstance(state.get("ticket_integration_queue"), list):
            raise ValueError("Missing dispatch integration queue")
        if any(str(item["ticket_number"]) not in state["ticket_jobs"] for item in state["ticket_integration_queue"]):
            raise ValueError("Invalid dispatch integration Ticket")
        require_queue(state)
    except QueryLimitError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise_materials_error(state, error)
    return state
