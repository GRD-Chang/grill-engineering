"""明确 Resume 事件与准确 Human Response 引用，日常读取不展开旧审计。"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any

from agent_run.resume_audit_contract import RESUME_AUDIT_EVENT_KEYS, resume_event_digest, resume_identity
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.sqlite_work_evidence import get_evidence, put_human_response_member
from agent_run.state_limits import consume_query_rows, require_record_size
from agent_run.work_projection import ResumeHistory, ResumeScope

BOOLS = {"new_thread", "human_response_supplied"}
INTS = {"sequence", "generation"}
_COLUMNS = sorted(RESUME_AUDIT_EVENT_KEYS)
TABLES = {
    "run_resume_roots": "CREATE TABLE run_resume_roots (run_id TEXT PRIMARY KEY, total INTEGER NOT NULL, rolling_digest TEXT, FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "run_resume_events": "CREATE TABLE run_resume_events (run_id TEXT NOT NULL, "
    + ", ".join(key + (" INTEGER" if key in INTS | BOOLS else " TEXT") for key in _COLUMNS)
    + ", PRIMARY KEY(run_id,sequence), UNIQUE(run_id,resume_id), FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "run_human_responses": "CREATE TABLE run_human_responses (run_id TEXT NOT NULL, resume_id TEXT NOT NULL, evidence_id TEXT NOT NULL, PRIMARY KEY(run_id,resume_id), "
    "FOREIGN KEY(run_id,resume_id) REFERENCES run_resume_events(run_id,resume_id) ON DELETE CASCADE, FOREIGN KEY(run_id,evidence_id) REFERENCES work_evidence(run_id,evidence_id))",
}
INDEXES = {
    "run_resume_attempt": "CREATE INDEX run_resume_attempt ON run_resume_events(run_id,semantic_attempt_id,sequence)",
    "run_resume_subject": "CREATE INDEX run_resume_subject ON run_resume_events(run_id,work_subject,sequence)",
}


def _root(c: sqlite3.Connection, run_id: str) -> tuple[int, str | None] | None:
    require_record_size(c, "run_resume_roots", ("run_id", "total", "rolling_digest"), "run_id=?", (run_id,))
    row = c.execute("SELECT total,rolling_digest FROM run_resume_roots WHERE run_id=?", (run_id,)).fetchone()
    if row is not None and (
        type(row[0]) is not int or row[0] < 0 or row[1] is not None and not isinstance(row[1], str)
    ):
        raise ValueError("Invalid Resume audit current record")
    return (row[0], row[1]) if row is not None else None


def _decode(c: sqlite3.Connection, run_id: str, sequence: int) -> dict[str, Any]:
    require_record_size(c, "run_resume_events", _COLUMNS, "run_id=? AND sequence=?", (run_id, sequence))
    row = c.execute(
        "SELECT " + ",".join(_COLUMNS) + " FROM run_resume_events WHERE run_id=? AND sequence=?", (run_id, sequence)
    ).fetchone()
    if row is None:
        raise ValueError("Missing Resume event")
    consume_query_rows(1)
    event = dict(zip(_COLUMNS, row, strict=True))
    for key in BOOLS:
        if type(event[key]) is not int or event[key] not in (0, 1):
            raise ValueError("Invalid Resume event boolean")
        event[key] = bool(event[key])
    for key in INTS:
        if event[key] is not None and (type(event[key]) is not int or event[key] < 1):
            raise ValueError("Invalid Resume event sequence or generation")
    for key in RESUME_AUDIT_EVENT_KEYS - BOOLS - INTS:
        if event[key] is not None and not isinstance(event[key], str):
            raise ValueError("Invalid Resume event text")
    if event["resume_id"] != resume_identity(run_id, event) or event["event_digest"] != resume_event_digest(event):
        raise ValueError("Invalid Resume event identity or digest")
    return event


def load(
    c: sqlite3.Connection,
    run_id: str,
    *,
    current_only: bool = False,
    attempt_ids: set[str] | None = None,
    resume_ids: set[str] | None = None,
    include_responses: bool = True,
) -> dict[str, Any]:
    root = _root(c, run_id)
    if root is None:
        return {}
    total, digest = root
    if current_only:
        ids = set(resume_ids or ())
        ids.update(
            row[0]
            for row in c.execute(
                "SELECT DISTINCT v.resume_id FROM work_invocation_current r JOIN work_invocations v USING(run_id,work_subject,started_at) WHERE r.run_id=? AND v.resume_id IS NOT NULL",
                (run_id,),
            )
        )
        sequences = {total} if total else set()
        for identity in ids:
            row = c.execute(
                "SELECT sequence FROM run_resume_events WHERE run_id=? AND resume_id=?", (run_id, identity)
            ).fetchone()
            if row is None:
                raise ValueError("Current Invocation references missing Resume event")
            sequences.add(row[0])
    elif attempt_ids is not None or resume_ids is not None:
        sequences = set()
        for identity in attempt_ids or ():
            sequences.update(
                row[0]
                for row in c.execute(
                    "SELECT sequence FROM run_resume_events WHERE run_id=? AND semantic_attempt_id=? ORDER BY sequence",
                    (run_id, identity),
                )
            )
        for identity in resume_ids or ():
            sequences.update(
                row[0]
                for row in c.execute(
                    "SELECT sequence FROM run_resume_events WHERE run_id=? AND resume_id=?", (run_id, identity)
                )
            )
    else:
        sequences = set()
        for (position,) in c.execute(
            "SELECT sequence FROM run_resume_events WHERE run_id=? ORDER BY sequence", (run_id,)
        ):
            consume_query_rows(1)
            if position != len(sequences) + 1:
                raise ValueError("Invalid Resume event positions")
            sequences.add(position)
        if len(sequences) != total:
            raise ValueError("Invalid Resume audit total")
    events = [_decode(c, run_id, sequence) for sequence in sorted(sequences)]
    history: list[dict[str, Any]] = events
    if current_only or attempt_ids is not None or resume_ids is not None:
        history = ResumeHistory(
            events,
            scope=ResumeScope(
                run_id, total, digest, frozenset((event["sequence"], event["resume_id"]) for event in events)
            ),
        )
    responses = {}
    if include_responses:
        for event in events:
            row = c.execute(
                "SELECT evidence_id FROM run_human_responses WHERE run_id=? AND resume_id=?",
                (run_id, event["resume_id"]),
            ).fetchone()
            if event["human_response_supplied"]:
                if row is None:
                    raise ValueError("Missing Human Response audit evidence")
                responses[event["resume_id"]] = get_evidence(
                    c, run_id, row[0], owner_id=f"run:{run_id}:resume:{event['resume_id']}", kind="human_response"
                )
            elif row is not None:
                raise ValueError("Unexpected Human Response audit evidence")
    return {
        "resume_audit": {"total": total, "compacted": 0, "rolling_digest": digest, "history": history},
        "human_response_audit": responses,
    }


def _source_response(c: sqlite3.Connection, run_id: str, event: Mapping[str, Any], response: str) -> tuple[str, str]:
    subject = event.get("work_subject")
    generation = event.get("generation")
    if not isinstance(subject, str):
        raise ValueError("Human Response audit requires its exact Job")
    if subject.startswith("ticket:"):
        job_id = f"{subject}:generation:{generation}"
    elif subject.startswith("parent-only:"):
        job_id = f"parent-only:generation:{generation}"
    elif subject.startswith("run-repair:"):
        job_id = f"run-repair:generation:{generation}"
    elif subject.startswith(("run-acceptance:", "run-integration:")):
        job_id = "run-acceptance:generation:1"
    elif subject.startswith("run-publication:"):
        job_id = "run-publication:generation:1"
    else:
        raise ValueError("Unknown Human Response Job")
    row = c.execute("SELECT evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None:
        raise ValueError("Missing Human Response source Job")
    source_owner = "job:" + row[0]
    source = c.execute(
        "SELECT evidence_id FROM work_job_archives WHERE run_id=? AND job_id=? AND history_kind='human_response_history' ORDER BY position DESC LIMIT 1",
        (run_id, job_id),
    ).fetchone()
    if source is None:
        raise ValueError("Missing Human Response source material")
    original = get_evidence(c, run_id, source[0], owner_id=source_owner, kind="human_response_history")
    if (
        not isinstance(original, dict)
        or original.get("generation") != generation
        or original.get("response") != response
    ):
        raise ValueError("Human Response does not match its exact Resume/Job")
    return source[0], source_owner


def save(
    c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any], *, fields: set[str]
) -> None:
    audit = state.get("resume_audit", baseline.get("resume_audit"))
    if "resume_audit" not in fields and "human_response_audit" not in fields:
        return
    root = _root(c, run_id)
    if audit is None:
        if root is not None and root[0]:
            raise ValueError("Cannot remove retained Resume audit history")
        return
    if not isinstance(audit, Mapping) or audit.get("compacted") != 0 or not isinstance(audit.get("history"), list):
        raise ValueError("Invalid Resume audit")
    history = audit["history"]
    old = (baseline.get("resume_audit") or {}).get("history", [])
    old_by_id = {item["resume_id"]: item for item in old}
    total = root[0] if root else 0
    digest = root[1] if root else None
    changed = False
    for event in history:
        if (
            not isinstance(event, dict)
            or set(event) != RESUME_AUDIT_EVENT_KEYS
            or event["resume_id"] != resume_identity(run_id, event)
            or event["event_digest"] != resume_event_digest(event)
        ):
            raise ValueError("Invalid Resume audit event")
        sequence = event["sequence"]
        row = c.execute(
            "SELECT resume_id FROM run_resume_events WHERE run_id=? AND sequence=?", (run_id, sequence)
        ).fetchone()
        if row is not None:
            previous = _decode(c, run_id, sequence)
            if previous == event:
                continue
            if row[0] != event["resume_id"] or old_by_id.get(event["resume_id"]) != previous:
                raise ValueError("Resume audit event changed before commit")
            if previous.get("successor_invocation_started_at") is not None:
                raise ValueError("Resume already belongs to another Invocation")
            mutable = {
                "work_subject",
                "generation",
                "semantic_attempt_id",
                "successor_invocation_started_at",
                "event_digest",
            }
            if any(event[key] != previous[key] for key in RESUME_AUDIT_EVENT_KEYS - mutable):
                raise ValueError("Immutable Resume authorization was changed")
        elif type(sequence) is not int or sequence != total + 1:
            raise ValueError("Resume audit append has an invalid sequence")
        else:
            total += 1
        values = tuple(int(event[key]) if key in BOOLS else event[key] for key in _COLUMNS)
        c.execute(
            "INSERT INTO run_resume_events VALUES ("
            + ",".join("?" for _ in range(len(values) + 1))
            + ") ON CONFLICT(run_id,sequence) DO UPDATE SET "
            + ",".join(key + "=excluded." + key for key in _COLUMNS),
            (run_id, *values),
        )
        require_record_size(c, "run_resume_events", _COLUMNS, "run_id=? AND sequence=?", (run_id, sequence))
        changed = True
    if audit.get("total") != total:
        raise ValueError("Cannot hide or clear unread Resume audit history")
    if changed:
        # SHA-256 链不可逆。只有明确修改审计的边界才流式处理标量摘要；普通保存不进入这里。
        digest = None
        for position, (sequence, event_digest) in enumerate(
            c.execute(
                "SELECT sequence,event_digest FROM run_resume_events WHERE run_id=? ORDER BY sequence", (run_id,)
            ),
            1,
        ):
            consume_query_rows(1)
            if sequence != position:
                raise ValueError("Invalid Resume audit positions")
            digest = canonical_fingerprint({"previous": digest, "event_digest": event_digest})
    c.execute(
        "INSERT INTO run_resume_roots VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET total=excluded.total,rolling_digest=excluded.rolling_digest",
        (run_id, total, digest),
    )
    responses = state.get("human_response_audit", baseline.get("human_response_audit", {}))
    if not isinstance(responses, Mapping):
        raise ValueError("Human Response audit must be an object")
    for event in history:
        identity = event["resume_id"]
        if not event["human_response_supplied"]:
            continue
        response = responses.get(identity)
        prior = c.execute(
            "SELECT evidence_id FROM run_human_responses WHERE run_id=? AND resume_id=?", (run_id, identity)
        ).fetchone()
        owner = f"run:{run_id}:resume:{identity}"
        if prior is not None:
            if response is None:
                continue
            if get_evidence(c, run_id, prior[0], owner_id=owner, kind="human_response") != response:
                raise ValueError("Immutable Human Response changed")
            continue
        if not isinstance(response, str) or not response or len(response.encode()) > 8192:
            raise ValueError("Missing or oversized Human Response")
        source, source_owner = _source_response(c, run_id, event, response)
        reference = put_human_response_member(c, run_id, owner, source, source_owner_id=source_owner)
        c.execute("INSERT INTO run_human_responses VALUES (?,?,?)", (run_id, identity, reference))
    if isinstance(audit, dict):
        audit["rolling_digest"] = digest
        if isinstance(history, ResumeHistory):
            history.scope = ResumeScope(
                run_id, total, digest, frozenset((item["sequence"], item["resume_id"]) for item in history)
            )
