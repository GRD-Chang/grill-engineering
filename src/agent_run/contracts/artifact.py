"""The role output wire contract: schemas and semantic validation, without runtime state."""
from __future__ import annotations

from agent_run.errors import ArtifactContractError


import json
import re
from typing import Any


MAX_HUMAN_BLOCKERS = 8
MAX_HUMAN_BLOCKER_LENGTH = 2_000
_CONTENT = {
    "development": ("summary",),
    "publication": ("commit_message", "pr_title", "pr_body_markdown"),
}
_LANES = ("e2e", "standards", "spec")
_STATUSES = ("pass", "fail", "blocked")
_CLOSING_KEYWORD = re.compile(r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+\b")
_PUBLISHER_FACT = re.compile(r"(?im)^\s*(?:Parent Issue|Primary Ticket|Delivery Type|Delivery Run):")
_PUBLISHER_SECTION = re.compile(r"(?im)^## Completed Tickets\s*$")




def _role(role: str) -> None:
    if role not in (*_CONTENT, "review"):
        raise ArtifactContractError("unknown artifact role")


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


def _text_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string",
            "minLength": 1, "pattern": r"\S"}


def role_schema(role: str) -> dict[str, Any]:
    """Return a fresh strict Structured Output schema; Python enforces field relationships."""
    _role(role)
    if role == "review":
        lane = _object_schema({
            "status": {"type": "string", "enum": list(_STATUSES)},
            "evidence": _text_schema(),
            "findings": {"type": "array", "items": _text_schema()},
        })
        checks = _object_schema({name: {"$ref": "#/$defs/lane"} for name in _LANES})
        return {**_object_schema({"checks": checks}), "$defs": {"lane": lane}}
    return _object_schema({
        "result_kind": {"type": "string", "enum": [role, "human_blocker"]},
        **{name: _text_schema(nullable=True) for name in _CONTENT[role]},
        "human_blockers": {"type": ["array", "null"], "minItems": 1,
                           "maxItems": MAX_HUMAN_BLOCKERS,
                           "items": {**_text_schema(), "maxLength": MAX_HUMAN_BLOCKER_LENGTH}},
    })


def _object(value: object, fields: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactContractError(f"{label} must be an object")
    extra, missing = set(value) - set(fields), set(fields) - set(value)
    if extra:
        raise ArtifactContractError(f"{label} has unexpected fields: {', '.join(sorted(extra))}")
    if missing:
        raise ArtifactContractError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactContractError(f"{label} must be a non-empty string")
    return value.strip()


def parse_artifact(role: str, raw: bytes) -> dict[str, Any]:
    """Validate one role's exact wire shape and normalize prose; callers retain raw bytes."""
    _role(role)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ArtifactContractError("artifact must be valid JSON") from error
    if role == "review":
        data = _object(value, ("checks",), "review artifact")
        checks = _object(data["checks"], _LANES, "checks")
        normalized = {}
        for name in _LANES:
            lane = _object(checks[name], ("status", "evidence", "findings"), f"{name} check")
            status = _text(lane["status"], "status")
            if status not in _STATUSES:
                raise ArtifactContractError(f"invalid {name} check status")
            evidence = _text(lane["evidence"], "evidence")
            findings = lane["findings"]
            if (not isinstance(findings, list)
                    or any(not isinstance(item, str) or not item.strip() for item in findings)):
                raise ArtifactContractError("findings must contain non-empty strings")
            if status == "fail" and not findings:
                raise ArtifactContractError(f"{name} fail check requires findings")
            if status != "fail" and findings:
                raise ArtifactContractError(f"{name} {status} check must not contain findings")
            normalized[name] = {"status": status, "evidence": evidence,
                                "findings": [item.strip() for item in findings]}
        return {"checks": normalized}
    content = _CONTENT[role]
    data = _object(value, ("result_kind", *content, "human_blockers"), f"{role} result")
    kind = data["result_kind"]
    if kind == "human_blocker":
        for name in content:
            if data[name] is not None:
                raise ArtifactContractError(f"human blocker {name} must be null")
        blockers = data["human_blockers"]
        if (not isinstance(blockers, list) or not blockers
                or any(not isinstance(item, str) or not item.strip() for item in blockers)):
            raise ArtifactContractError("human_blockers must contain non-empty strings")
        if len(blockers) > MAX_HUMAN_BLOCKERS:
            raise ArtifactContractError(f"human_blockers must contain at most {MAX_HUMAN_BLOCKERS} items")
        if any(len(item) > MAX_HUMAN_BLOCKER_LENGTH for item in blockers):
            raise ArtifactContractError(f"each human blocker must contain at most {MAX_HUMAN_BLOCKER_LENGTH} characters")
        return data
    if kind != role:
        raise ArtifactContractError(f"invalid {role} result_kind")
    if data["human_blockers"] is not None:
        raise ArtifactContractError(f"{role} result human_blockers must be null")
    result: dict[str, Any] = {
        "result_kind": role,
        **{name: _text(data[name], name) for name in content},
        "human_blockers": None,
    }
    if role == "publication":
        body = result["pr_body_markdown"]
        if _PUBLISHER_FACT.search(body):
            raise ArtifactContractError("PR narrative must not contain Publisher-owned facts")
        if _PUBLISHER_SECTION.search(body):
            raise ArtifactContractError("PR narrative must not contain Publisher-owned sections")
        if _CLOSING_KEYWORD.search(body):
            raise ArtifactContractError("PR body must not contain automatic closing keywords")
    return result
