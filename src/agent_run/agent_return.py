"""Small durable role results, independent of stage transitions."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, TypeAlias

from agent_run.agents import (
    DevelopmentResult, HumanBlockerResult, PublicationResult, ReviewResult,
)
from agent_run.artifacts import (
    AcceptanceArtifact, PublicationArtifact, parse_publication_wire_result,
    MAX_HUMAN_BLOCKERS, MAX_HUMAN_BLOCKER_LENGTH,
)

AgentReturn: TypeAlias = DevelopmentResult | HumanBlockerResult | ReviewResult | PublicationResult | dict[str, Any]


def has_returned_agent_result(subject: dict[str, Any], *, role: str | None = None) -> bool:
    attempt = subject.get("pending_semantic_attempt")
    return (
        isinstance(attempt, dict) and attempt.get("status") == "pending"
        and (role is None or attempt.get("role") == role)
        and "returned_result" in attempt
    )


def store_agent_return(result: AgentReturn, attempt: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any]
    if isinstance(result, HumanBlockerResult):
        record = {"kind": "human_blocker", "thread_id": result.thread_id,
                  "human_blockers": list(result.human_blockers), "replaced_thread_id": result.replaced_thread_id}
    elif isinstance(result, DevelopmentResult):
        record = {"kind": "development", "thread_id": result.thread_id,
                  "summary": result.summary, "replaced_thread_id": result.replaced_thread_id}
    elif isinstance(result, ReviewResult):
        record = {"kind": "review", "thread_id": result.thread_id, "artifact": deepcopy(result.artifact)}
    elif isinstance(result, PublicationResult):
        record = {"kind": "publication", "thread_id": result.thread_id,
                  "artifact": deepcopy(result.artifact), "replaced_thread_id": result.replaced_thread_id}
    else:
        record = {"kind": "publication_wire", "artifact": deepcopy(result)}
    restore_agent_return(record, attempt)
    return record


def restore_agent_return(value: object, attempt: dict[str, Any]) -> AgentReturn:
    if not isinstance(value, dict):
        raise ValueError("Agent returned result must be an object")
    kind = value.get("kind")
    role = attempt.get("role")
    fields = {
        "human_blocker": {"kind", "thread_id", "human_blockers", "replaced_thread_id"},
        "development": {"kind", "thread_id", "summary", "replaced_thread_id"},
        "review": {"kind", "thread_id", "artifact"},
        "publication": {"kind", "thread_id", "artifact", "replaced_thread_id"},
        "publication_wire": {"kind", "artifact"},
    }
    if not isinstance(kind, str) or kind not in fields or set(value) != fields[kind]:
        raise ValueError("Agent returned result fields are invalid")
    if kind == "human_blocker":
        if role not in {"development", "publication"}:
            raise ValueError("Human Blocker result has the wrong role")
        blockers = value.get("human_blockers")
        if not isinstance(blockers, list) or not blockers or len(blockers) > MAX_HUMAN_BLOCKERS or any(
            not isinstance(x, str) or not x.strip() or len(x) > MAX_HUMAN_BLOCKER_LENGTH for x in blockers
        ):
            raise ValueError("Agent returned Human Blocker is incomplete")
        return HumanBlockerResult(_thread(value), tuple(blockers), _replacement(value))
    if kind == "development" and role == "development":
        summary = value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Development result is incomplete")
        return DevelopmentResult(_thread(value), summary, _replacement(value))
    if kind == "review" and role == "reviewer":
        artifact = AcceptanceArtifact.parse(value.get("artifact"))
        return ReviewResult(_thread(value), deepcopy(artifact.raw))
    if kind in {"publication", "publication_wire"} and role == "publication":
        raw = deepcopy(value.get("artifact"))
        if not isinstance(raw, dict):
            raise ValueError("Publication returned result is incomplete")
        thread = raw.pop("_thread_id", None)
        # Older test backends return the Controller's normalized narrative.
        wire = raw if "result_kind" in raw else {"result_kind": "publication", **raw, "human_blockers": None}
        normalized = parse_publication_wire_result(wire)
        if normalized["result_kind"] == "publication":
            subject = str(attempt["work_subject"])
            identity: dict[str, Any] = ({"primary_ticket": int(subject.split(":", 1)[1])}
                if subject.startswith("ticket:") else {"delivery_run": subject.split(":", 1)[1]})
            PublicationArtifact.parse(normalized, **identity)
        if kind == "publication":
            return PublicationResult(_thread(value), deepcopy(value["artifact"]), _replacement(value))
        if thread is not None and (not isinstance(thread, str) or not thread.strip()):
            raise ValueError("Publication returned Thread is invalid")
        result: dict[str, Any] = deepcopy(value["artifact"])
        return result
    raise ValueError("Agent returned result does not match its Semantic Attempt role")


def agent_return_requires_human(value: dict[str, Any]) -> bool:
    if value["kind"] == "human_blocker":
        return True
    if value["kind"] == "review":
        return AcceptanceArtifact.parse(value["artifact"]).requires_human
    if value["kind"] in {"publication", "publication_wire"}:
        return bool(value["artifact"].get("result_kind") == "human_blocker")
    return False


def clear_blocked_agent_return(subject: dict[str, Any]) -> None:
    """Only explicit Human Blocker continuation may request another response."""
    attempt = subject.get("pending_semantic_attempt")
    result = attempt.get("returned_result") if isinstance(attempt, dict) else None
    if isinstance(attempt, dict) and isinstance(result, dict) and agent_return_requires_human(result):
        attempt.pop("returned_result", None)


def _thread(value: dict[str, Any]) -> str:
    thread = value.get("thread_id")
    if not isinstance(thread, str) or not thread.strip():
        raise ValueError("Agent returned Thread is empty")
    return thread


def _replacement(value: dict[str, Any]) -> str | None:
    replacement = value.get("replaced_thread_id")
    if replacement is not None and (not isinstance(replacement, str) or not replacement.strip()):
        raise ValueError("Agent returned replacement Thread is invalid")
    return replacement
