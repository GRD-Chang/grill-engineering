"""Read-only public facts from the canonical store, never a writable Run."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal, Mapping, Sequence

from agent_run.contracts.task import TaskKey
from agent_run.storage.notification_outbox import read_notifications

from agent_run.queries.execution import activity, recorded_profile
from agent_run.queries.resume_facts import resume_facts
from agent_run.queries.model import HistoryPage, RunStatus
from agent_run.errors import KernelError
from agent_run.model import Role, artifact
from agent_run.storage.sqlite import QUERY_LIMIT, Storage, Transaction
from agent_run.storage.preflight import UnsupportedState, require_supported_state
from agent_run.storage.tasks import read_task, task_path
from agent_run.queries.read_facts import QueryError as QueryError, all_rows as _all, material as _material, timestamp as _timestamp, read_header as _header, public_phase as _public_phase, read_boundaries


@dataclass(frozen=True)
class QueryDocument:
    kind: Literal["status", "history", "runs", "settings"]
    language: str
    data: Mapping[str, Any]


def _state_error(path: Path, error: Exception) -> QueryError:
    code = error.code if isinstance(error, KernelError) and error.code in {"query_limit", "storage_limit"} else "incompatible_run_state"
    return QueryError(code, f"Cannot interpret Run state at {path}: {error}. "
                      "Only the current native format is supported. Preserve the state and use a "
                      "Runner matching the authoritative database for inspection; do not rewrite its Task pointer or fences.")


def review_findings(value: Any) -> list[str]:
    """Select original human-facing evidence without translating or deciding it."""
    if not isinstance(value, Mapping):
        return []
    blockers = value.get("human_blockers")
    result = [item for item in blockers if isinstance(item, str)] if isinstance(blockers, list) else []
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        for lane in checks.values():
            if isinstance(lane, Mapping) and lane.get("status") in {"fail", "blocked"}:
                findings = lane.get("findings")
                if isinstance(findings, list):
                    result.extend(item for item in findings if isinstance(item, str))
                if lane.get("status") == "blocked" and isinstance(lane.get("evidence"), str):
                    result.append(lane["evidence"])
    return result


def header(path: Path) -> dict[str, Any]:
    try:
        require_supported_state(path)
        with Storage(path).transaction(write=False) as tx:
            return _header(tx, path)
    except UnsupportedState:
        raise
    except KernelError as error:
        raise _state_error(path, error) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise _state_error(path, error) from error


def task_binding(task: TaskKey) -> tuple[str, Path] | None:
    """Read an exact Task publication without creating its admission lock."""
    try:
        return read_task(task_path(task))
    except (KernelError, OSError) as error:
        raise QueryError("run_locator_invalid", f"Cannot read Task publication: {error}") from error


def task_published(candidate: Mapping[str, Any]) -> bool:
    """Check the exact Task pointer; enumeration alone does not publish a Run."""
    if candidate["repository_root"] is None:
        return False
    task = TaskKey(Path(candidate["repository_root"]), candidate["repository"], candidate["parent"])
    binding = task_binding(task)
    return binding == (candidate["run_id"], Path(candidate["database"]).resolve())


def discover(root: Path, *, published: Mapping[Path, str] | None = None,
             run_id: str | None = None, repository: str | None = None,
             parent: int | None = None) -> list[dict[str, Any]]:
    """Discover canonical databases without creating a locator or repairing one."""
    if not root.exists() and published is None:
        return []
    if root.is_symlink():
        raise QueryError("run_locator_invalid", "State directory is a symbolic link")
    paths: list[Path] = [root] if root.is_file() else []
    if published is not None:
        paths.extend(path for path in published if path.is_relative_to(root.resolve()) and path not in paths)
    if root.is_dir():
        def unreadable(error: OSError) -> None:
            raise QueryError("run_locator_invalid", f"Cannot read state directory: {error}") from error

        for visited, (directory, _children, files) in enumerate(os.walk(root, followlinks=False, onerror=unreadable)):
            if visited >= 4096:
                raise QueryError("query_limit", "Discovery directory limit; provide --state-dir")
            for name in ("kernel.sqlite3", "state.sqlite3"):
                path = Path(directory) / name
                if name in files and path not in paths:
                    paths.append(path)
                if published is None and len(paths) > 256:
                    raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    candidates: list[dict[str, Any]] = []
    for path in paths:
        expected = None if published is None else published.get(path.resolve())
        if path.is_symlink():
            if published is not None and expected is None:
                continue
            raise QueryError("run_locator_conflict", f"Symbolic database path: {path}")
        try:
            candidate = header(path)
        except UnsupportedState as error:
            if published is not None and expected is None:
                continue
            if expected is None:
                raise
            # Keep the publication's known identity without reading an
            # unsupported database to infer any additional Run facts.
            raise QueryError(error.code, str(error), state_diagnostic=error.diagnostic(), candidates=[{
                "run_id": expected, "database": str(path), "state_dir": str(path.parent),
                "repository": repository, "parent": parent, "repository_root": None,
            }]) from error
        except (ValueError, sqlite3.Error, OSError, KernelError, KeyError, TypeError) as error:
            if published is not None and expected is None:
                continue
            if isinstance(error, QueryError):
                raise
            raise QueryError("incompatible_run_state", str(error)) from error
        if expected is not None and candidate["run_id"] != expected:
            raise QueryError("run_locator_conflict", f"Task publication names a different Run: {path}")
        candidates.append(candidate)
        if published is None and len(candidates) > 256:
            raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    return candidates


def _invocations(tx: Transaction, subject: str, attempt: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT i.*,a.role,t.role AS binding_role,t.profile_revision,t.backend_id,t.subject AS thread_subject,a.subject FROM invocations i
             JOIN attempts a ON a.id=i.attempt LEFT JOIN threads t ON t.id=i.thread WHERE a.subject=?"""
    values: tuple[Any, ...] = (subject,)
    if attempt is not None:
        sql += " AND a.id=?"
        values += (attempt,)
    result = _all(tx, sql + " ORDER BY i.rowid", values)
    for row in result:
        if row.pop("thread_subject") != subject:
            raise QueryError("incompatible_run_state", "Invocation has a missing or foreign Thread binding")
        profile = recorded_profile(tx, row["profile_revision"])["profiles"][row["binding_role"]]
        row.update({"model": profile["model"], "effort": profile["reasoning_effort"], "profile_reference": profile["reference"]})
        row["allocated_at"] = _timestamp(row["allocated_at"])
        outputs = _all(tx, "SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (row["id"],))
        if not outputs:
            raise QueryError("incompatible_run_state", "Invocation has no physical output allocation")
        for output in outputs:
            for field in ("allocated_at", "dispatched_at", "started_at", "returned_at", "exited_at", "recovery_ready_at"):
                output[field] = _timestamp(output[field])
            output["raw_return"] = _material(tx, output["raw_id"], subject)
            output["failure_evidence"] = _material(tx, output["start_evidence"], subject)
            measured = activity(tx, row["id"], output["ordinal"], subject)
            output["activity"] = measured.pop("events")
            output.update(measured)
            output["process_span_seconds"] = (output["exited_at"] - output["started_at"]
                if output["started_at"] is not None and output["exited_at"] is not None
                and output["exited_at"] >= output["started_at"] else None)
            output.pop("context_id")
            output.pop("process_identity")
            output.pop("start_evidence")
        row["outputs"] = outputs
        row["elapsed_seconds"] = (sum(output["elapsed_seconds"] for output in outputs)
                                  if all(output["elapsed_seconds"] is not None for output in outputs) else None)
        row["live_status"] = "unknown"
    return result


def _attempt(tx: Transaction, record: sqlite3.Row) -> dict[str, Any]:
    """One native Attempt representation for current pointers and historical closure."""
    attempt = dict(record)
    attempt["result"] = _material(tx, attempt["result_id"], attempt["subject"])
    attempt["invocations"] = _invocations(tx, attempt["subject"], attempt["id"])
    attempt.pop("context_id")
    return attempt


def _effect_failures(tx: Transaction, subject: str | None = None, *, effect: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT f.*,e.subject,e.operation,e.status FROM effect_failures f JOIN effects e ON e.id=f.effect"
    values: tuple[Any, ...] = () if subject is None else (subject,)
    clauses = ["e.subject=?"] if subject is not None else []
    if effect is not None:
        clauses.append("e.id=?")
        values += (effect,)
    rows = _all(tx, sql + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY f.observed_at,f.effect,f.dispatch_serial", values)
    for row in rows:
        row["observed_at"] = _timestamp(row["observed_at"])
        row["evidence"] = _material(tx, row["evidence"], row["subject"])
    return rows


def _resync(tx: Transaction, subject: str) -> dict[str, Any]:
    allowance = tx.one("SELECT * FROM resync_allowances WHERE subject=? AND current=1", (subject,))
    snapshots = tx.rows("SELECT * FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
    snapshot = dict(snapshots[0]) if snapshots else None
    return {"generation": allowance["generation"], "used": allowance["used"], "limit": allowance["allowance"],
            "window": snapshot["window"] if snapshot else None, "open": bool(snapshot and snapshot["open"]),
            "source_sha": snapshot["source_sha"] if snapshot else None, "target_sha": snapshot["target_sha"] if snapshot else None}


def _subject(tx: Transaction, job: dict[str, Any], boundary: Mapping[str, Any]) -> dict[str, Any]:
    subject = job["id"]
    budgets = _all(tx, "SELECT * FROM budgets WHERE subject=? ORDER BY purpose,window", (subject,))
    pending = None
    if job["current_attempt"]:
        pending = _attempt(tx, tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (job["current_attempt"], subject)))
    review = None
    if job["review_attempt"]:
        attempt = tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (job["review_attempt"], subject))
        review = {"attempt_id": attempt["id"], "outcome": attempt["outcome"],
                  "artifact": artifact(Role.REVIEW, tx.get(attempt["result_id"], subject)) if attempt["result_id"] else None}
    effects = _all(tx, "SELECT id,operation,status,attempts,before_sha,after_sha FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') ORDER BY rowid", (subject,))
    checks = None
    if job["checks_id"]:
        check = dict(tx.one("SELECT * FROM checks WHERE id=? AND subject=?", (job["checks_id"], subject)))
        check["evidence"] = _material(tx, check["material_id"], subject)
        checks = check
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?", (job["issue"], job["revision"]))
    return {"subject": subject, "kind": job["kind"], "issue": job["issue"],
            "title": title[0][0] if title else None, "phase": job["phase"], "purpose": job["purpose"],
            "generation": job["generation"], "revision": job["revision"],
            "candidate_sha": job["candidate_sha"], "publication_sha": job["publication_sha"],
            "integrated_sha": job["integrated_sha"], "pr_number": job["pr_number"],
            "ci_evidence": _material(tx, job["ci_failure"], subject),
            "gates": boundary["gates"], "budgets": budgets, "pending_attempt": pending, "review": review, "required_checks": checks,
            "integration_resync": _resync(tx, subject), "effect_failures": _effect_failures(tx, subject),
            "pending_writes": effects, "delivery_cleanup": boundary["delivery_cleanup"],
            "blocked_by": [row[0] for row in tx.all("SELECT s.issue FROM dependencies d JOIN subjects s ON s.id=d.predecessor "
                "WHERE d.subject=? AND s.phase!='complete' ORDER BY s.issue", (subject,))]}


def _next_action(run: Mapping[str, Any], actions: list[dict[str, Any]], status: str,
                 cleanup: Mapping[str, Any] | None) -> str:
    if status == "abandonment_pending":
        return f"agent-run abandon {run['parent_issue']} --repo {run['repository']}"
    if status == "abandoned":
        return "无"
    if actions:
        action = actions[0]
        return str(action["next_action"])
    if cleanup and cleanup["status"] == "cleanup_pending":
        return f"agent-run resume {run['id']}"
    if status == "ticket_completed":
        return f"agent-run run {run['parent_issue']}"
    if status in {"completed", "abandoned"}:
        return "无"  # Stable machine copy; human rendering localizes it.
    return f"agent-run status {run['parent_issue']} --repo {run['repository']}"


def _control_history(tx: Transaction, *, identities: Sequence[str] | None = None) -> list[dict[str, Any]]:
    actions = (_all(tx, "SELECT * FROM actions WHERE kind='resume' ORDER BY rowid DESC LIMIT 1")
               if identities is None else
               [dict(row) for identity in identities for row in tx.rows("SELECT * FROM actions WHERE id=?", (identity,))])
    first_owner = tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
    for action in actions:
        owner = action["subject"] or first_owner
        action["payload"] = _material(tx, action["payload_id"], owner)
        action["receipt"] = _material(tx, action["receipt_id"], owner)
    _resume_progress(tx, actions)
    return actions


def _resume_progress(tx: Transaction, control: list[dict[str, Any]]) -> None:
    """Display original acceptance and the shared exact-target outcome facts."""
    resumes: list[dict[str, Any]] = []
    applications = []
    for action in control:
        if action["kind"] != "resume":
            continue
        successors = _all(tx, "SELECT id FROM invocations WHERE action_id=? ORDER BY rowid", (action["id"],))
        started = tx.one("SELECT min(o.started_at) FROM invocation_outputs o JOIN invocations i "
                         "ON i.id=o.invocation WHERE i.action_id=?", (action["id"],))[0]
        receipt = action["receipt"]
        application = tx.rows("SELECT position,generation FROM history WHERE kind='control_applied' "
                              "AND identity=? AND material_id=? ORDER BY position LIMIT 1",
                              (action["id"], action["receipt_id"]))
        if application:
            applications.append(application[0])
        action["resume"] = {"applied": isinstance(receipt, Mapping) and receipt.get("applied") is True,
                            "successor_invocations": [row["id"] for row in successors],
                            "started_at": _timestamp(started), "progress": None}
        resumes.append(action)
    if not resumes or not applications:
        return
    by_action = {row["id"]: row["resume"] for row in resumes}
    # This is a candidate read, not a second Resume interpretation. Only the
    # shared reader decides whether a proof belongs to the original Action.
    # Archived status bodies/phase copy are never needed by that reader.
    sql = ("SELECT position,kind,identity,subject,generation,material_id FROM history WHERE position>? "
           "AND kind IN ('worker_started','effect_consumed','checks_observed','role_consumed','paused')")
    values: tuple[Any, ...] = (min(row["position"] for row in applications),)
    if len(resumes) == 1 and resumes[0]["subject"] is not None:
        sql += " AND subject=? AND generation=?"
        values += (resumes[0]["subject"], applications[0]["generation"])
    for row in tx.all(sql + " ORDER BY position", values):
        facts = resume_facts(tx, dict(row))
        if facts and facts["action_id"] in by_action:
            by_action[facts["action_id"]]["progress"] = facts


def _resume_waits(run: Mapping[str, Any], subjects: list[dict[str, Any]],
                  leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name outstanding original Workers after Stop, without accepting a Resume."""
    stopped = {job["subject"]: job for job in subjects if run["stopped"]
               or any(gate["code"] == "operator_stopped" for gate in job["gates"])}
    return [{**lease, "issue": stopped[lease["subject"]]["issue"], "reason": "worker_exit_unproven"}
            for lease in leases if lease["subject"] in stopped]



def _notification_facts(tx: Transaction, run: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Project the existing journal without claiming a configuration receipt."""
    rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
    inputs = _material(tx, rows[0]["material_id"], rows[0]["subject"]) if rows else {}
    configuration = inputs.get("notifications") if isinstance(inputs, Mapping) else None
    if not isinstance(configuration, Mapping):
        configuration = None
    journal = read_notifications(path.parent, run["id"])
    reason = journal.get("unavailable_reason")
    records = journal.get("records", [])
    readable = not reason or "pending" in journal or "records" in journal
    if any(not isinstance(item, Mapping) for item in records):
        reason = "Invalid notification delivery record"
        readable = False
    result = dict(journal)
    result.update({"configuration": configuration,
                   "configuration_source": "run_snapshot" if configuration is not None else "unavailable",
                   "journal_binding": {"status": "unknown", "reason": "not_recorded"},
                   "journal_state": "unavailable" if not readable else "available" if journal else "missing",
                   "unconfirmed_count": None})
    if reason:
        result["unavailable_reason"] = reason
    if readable and journal:
        result["unconfirmed_count"] = len(journal.get("pending", [])) + sum(
            not item.get("cancelled") and bool(item.get("exhausted") or item.get("outcome") in ("failed", "unknown"))
            for item in records)
    return result


def _frozen_settings(tx: Transaction, run: Mapping[str, Any]) -> dict[str, Any]:
    from agent_run.contracts.identity import describe_identity
    from agent_run.contracts.copy import text

    profiles = recorded_profile(tx, run["profile_revision"])
    rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
    inputs = _material(tx, rows[0]["material_id"], rows[0]["subject"]) if rows else {}
    if not isinstance(inputs, dict):
        raise QueryError("incompatible_run_state", "Invalid frozen creation inputs")
    bindings = []
    for thread in _all(tx, "SELECT * FROM threads ORDER BY rowid"):
        profile = recorded_profile(tx, thread["profile_revision"])["profiles"][thread["role"]]
        bindings.append({"thread_identity": thread["id"], "thread_id": thread["backend_id"],
                         "role": thread["role"], "profile_revision": thread["profile_revision"],
                         "model": profile["model"], "reasoning_effort": profile["reasoning_effort"],
                         "reference": profile["reference"]})
    return {"result": "settings", "scope": "run", "run_id": run["id"], "language": run["language"],
            "repository_root": run["repository_root"], "policy": json.loads(run["policy"]),
            "github": describe_identity(inputs["worker_identity"], run["language"], inputs.get("runner_identity"))
                      if inputs.get("worker_identity") is not None else None,
            "notifications": inputs.get("notifications"), "prompt_resources": inputs.get("prompt_resources"),
            "provenance": inputs.get("provenance"),
            "profile": {"preset": profiles.get("preset"), "profiles": profiles["profiles"], "profile_revision": run["profile_revision"]},
            "source": "run_snapshot", "bindings": bindings, "message": text("settings.run_notice", language=run["language"]),
            "unavailable_facts": [key for key in ("worker_identity", "runner_identity", "notifications", "prompt_resources", "provenance")
                                  if inputs.get(key) is None]}

def _native_history(tx: Transaction, subjects: list[dict[str, Any]], *,
                    after: int, limit: int | None) -> HistoryPage:
    events = (_all(tx, "SELECT * FROM history WHERE position>? ORDER BY position", (after,)) if limit is None else
              [dict(row) for row in tx.rows("SELECT * FROM history WHERE position>? ORDER BY position LIMIT ?", (after, limit))])
    identities = list(dict.fromkeys(event["identity"] for event in events))
    for event in events:
        if (type(event["position"]) is not int or type(event["generation"]) is not int or event["generation"] < 1
                or not isinstance(event["kind"], str) or not isinstance(event["identity"], str)):
            raise QueryError("incompatible_run_state", "Invalid history event identity")
        event["created_at"] = _timestamp(event.get("created_at"))
        event["status"] = json.loads(event["status"]) if event["status"] is not None else None
        event["evidence"] = _material(tx, event["material_id"], event["subject"])
    next_after = events[-1]["position"] if events else after
    has_more = bool(tx.rows("SELECT position FROM history WHERE position>? LIMIT 1", (next_after,)))
    attempt_ids = {row[0] for identity in identities for row in tx.rows(
        "SELECT id FROM attempts WHERE id=? UNION SELECT attempt FROM invocations WHERE id=?",
        (identity, identity)) if row[0] is not None}
    attempts = [_attempt(tx, tx.one("SELECT * FROM attempts WHERE id=?", (identity,))) for identity in sorted(attempt_ids)]
    owners = {job["subject"]: job for job in subjects}
    rounds: dict[str, int] = {}
    for attempt in attempts:
        prefix = {"ticket": "ticket", "parent_only": "parent", "final_run": "run"}[owners[attempt["subject"]]["kind"]]
        key = prefix + "_" + attempt["role"]
        rounds[key] = rounds.get(key, 0) + 1
    return {"events": events, "attempts": attempts, "time_zone": "UTC",
            "effect_failures": [failure for identity in identities for failure in _effect_failures(tx, effect=identity)],
            "effects": [dict(row) for identity in sorted(identities) for row in tx.rows(
                "SELECT id,subject,operation,attempts,status FROM effects WHERE id=?", (identity,))],
            "actions": _control_history(tx, identities=identities), "rounds": rounds,
            "after": after, "next_after": next_after, "has_more": has_more}



def read(path: Path, kind: Literal["status", "history", "settings"], *, after: int = 0, limit: int | None = None,
         expected_run: str | None = None, expected_repository: str | None = None) -> QueryDocument:
    """Read native facts without granting control or interpreting other formats."""
    if after < 0 or (limit is not None and not 1 <= limit <= 128):
        raise QueryError("query_limit", "History requires after >= 0 and an optional limit between 1 and 128")
    try:
        require_supported_state(path)
        with Storage(path).transaction(write=False) as tx:
            location = _header(tx, path)
            run = dict(tx.one("SELECT * FROM runs"))
            if ((expected_run is not None and run["id"] != expected_run)
                    or (expected_repository is not None and run["repository"].lower() != expected_repository)):
                raise QueryError("run_locator_conflict", "Selected database no longer matches its Run identity")
            language = run.get("language")
            if language not in {"zh", "en"}:
                raise QueryError("recovery_materials_missing", "Run language is missing or invalid; personal defaults cannot replace it")
            if kind == "settings":
                settings = _frozen_settings(tx, run)
                if len(json.dumps(settings, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
                    raise QueryError("query_limit", "Public response exceeds its bounded limit")
                return QueryDocument("settings", language, settings)
            now = time.time()
            boundaries = read_boundaries(tx, now)
            by_subject = {job["subject"]: job for job in boundaries["subjects"]}
            subjects = [_subject(tx, job, by_subject[job["id"]]) for job in _all(tx, "SELECT * FROM subjects ORDER BY id")]
            windows = boundaries["supervision_windows"]
            for subject in subjects:
                subject_windows = [window for window in windows if window["subject"] == subject["subject"]]
                subject["supervision_windows"] = subject_windows
            abandonment = boundaries["abandonment"]
            actions = boundaries["actions"]
            title = tx.rows("SELECT title,body FROM requirement_versions WHERE issue=? AND revision=?", (run["parent_issue"], run["parent_revision"]))
            active = [job for job in subjects if job["phase"] not in {"complete", "abandoned"}]
            complete = boundaries["is_terminal"]
            status = boundaries["status"]
            created = _timestamp(run.get("created_at"))
            closed = tx.rows("SELECT max(h.created_at) FROM history h JOIN effects e ON e.id=h.identity WHERE h.kind='effect_consumed' AND e.operation='close'")
            end = _timestamp(closed[0][0]) if complete and closed else None
            elapsed_end = end if complete else now
            elapsed = elapsed_end - created if created is not None and elapsed_end is not None and elapsed_end >= created else None
            focused: dict[str, Any] | None = next((job for job in subjects if job["subject"] == run["integration_holder"]), None)
            if focused is None:
                if len(active) == 1:
                    focused = active[0]
                else:
                    focused = next((job for job in subjects if job["kind"] == "final_run" and job["phase"] == "complete"), None)
            data: RunStatus = {"run_id": run["id"], "repository": run["repository"],
                "parent": {"number": run["parent_issue"], "title": title[0][0] if title else None,
                           "body": title[0][1] if title else None, "revision": run["parent_revision"]},
                "status": status, "phase": status if abandonment is not None else _public_phase(active[0]["phase"]) if len(active) == 1 else "parallel" if active else
                    "merged" if any(job["kind"] == "final_run" and job["phase"] == "complete" for job in subjects) else status,
                "created_at": created, "elapsed_seconds": elapsed, "repository_root": location["repository_root"],
                "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "policy_snapshot": json.loads(run["policy"]),
                "subjects": subjects, "operator_actions": actions,
                "focus_subject": focused["subject"] if focused else None,
                "next_action": _next_action(run, actions, status, boundaries["delivery_cleanup"]),
                "delivery_cleanup": boundaries["delivery_cleanup"], "supervision_windows": windows,
                "latest_resume": None, "resume_waits": [], "abandonment": abandonment, "unavailable_facts": [],
                "diagnostics": [], "notifications": _notification_facts(tx, run, path),
                "executor_control": {"owner": run["executor"], "epoch": run["epoch"], "stopped": bool(run["stopped"]), "blocked": run["blocked"], "live_status": "unknown"},
                "scheduling": {"concurrency": run["concurrency"], "integration_holder": run["integration_holder"],
                               "active": _all(tx, "SELECT id,subject,invocation FROM leases ORDER BY subject"),
                               "work_fifo": [row["subject"] for row in _all(tx, "SELECT subject FROM work_queue ORDER BY ready")],
                               "integration_fifo": [row["subject"] for row in _all(tx, "SELECT subject FROM integration_queue ORDER BY ready")]}}
            control = _control_history(tx)
            data["latest_resume"] = control[0] if control else None
            data["resume_waits"] = _resume_waits(run, subjects, data["scheduling"]["active"])
            data["unavailable_facts"] = ["host_liveness", "host_activity_segments", "notifications.journal_binding"]
            if data["notifications"]["journal_state"] != "available":
                data["unavailable_facts"].append("notifications")
            if data["notifications"]["configuration"] is None:
                data["unavailable_facts"].append("notifications.configuration")
            if kind == "history":
                data["history"] = _native_history(tx, subjects, after=after, limit=limit)
            if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > QUERY_LIMIT:
                raise QueryError("query_limit", "Public response exceeds its bounded limit")
            return QueryDocument(kind, language, data)
    except QueryError:
        raise
    except UnsupportedState:
        raise
    except KernelError as error:
        raise _state_error(path, error) from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise _state_error(path, error) from error
