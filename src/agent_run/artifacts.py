from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_CLOSING_KEYWORD = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+\b"
)
_PUBLISHER_OWNED_CONTEXT = re.compile(
    r"(?im)^\s*(?:Parent Issue|Primary Ticket|Delivery Type|Delivery Run):"
)
_PUBLISHER_OWNED_SECTION = re.compile(r"(?im)^## Completed Tickets\s*$")

MAX_HUMAN_BLOCKERS = 8
MAX_HUMAN_BLOCKER_LENGTH = 2_000
MAX_HUMAN_BLOCKER_HISTORY = 16
_PUBLICATION_RESULT_FIELDS = {
    "result_kind",
    "commit_message",
    "pr_title",
    "pr_body_markdown",
    "human_blockers",
}
_DEVELOPMENT_RESULT_FIELDS = {"result_kind", "summary", "human_blockers"}


@dataclass(frozen=True)
class PublicationArtifact:
    commit_message: str
    pr_title: str
    pr_body_markdown: str

    @classmethod
    def parse(
        cls,
        value: object,
        *,
        primary_ticket: int | None = None,
        delivery_run: str | None = None,
    ) -> PublicationArtifact:
        data = parse_publication_wire_result(value)
        if data.get("result_kind", "publication") != "publication":
            raise ValueError("publication artifact result_kind must be publication")
        commit_message = _nonempty_string(data, "commit_message")
        pr_title = _nonempty_string(data, "pr_title")
        body = _nonempty_string(data, "pr_body_markdown")
        if (primary_ticket is None) == (delivery_run is None):
            raise ValueError("publication artifact requires exactly one identity")
        if _PUBLISHER_OWNED_CONTEXT.search(body):
            raise ValueError("PR narrative must not contain Publisher-owned facts")
        if _PUBLISHER_OWNED_SECTION.search(body):
            raise ValueError("PR narrative must not contain Publisher-owned sections")
        if _CLOSING_KEYWORD.search(body):
            raise ValueError("PR body must not contain automatic closing keywords")
        return cls(
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body_markdown=body,
        )

    @classmethod
    def from_stored(
        cls,
        value: object,
        *,
        primary_ticket: int | None = None,
        delivery_run: str | None = None,
    ) -> PublicationArtifact:
        """Parse Controller-owned normalized storage, never untrusted wire output."""
        data = _mapping(value, "stored publication artifact")
        _exact_fields(
            data,
            {"commit_message", "pr_title", "pr_body_markdown"},
            "stored publication artifact",
        )
        return cls.parse(
            {
                "result_kind": "publication",
                **data,
                "human_blockers": None,
            },
            primary_ticket=primary_ticket,
            delivery_run=delivery_run,
        )


@dataclass(frozen=True)
class AcceptanceArtifact:
    checks: dict[str, dict[str, Any]]
    raw: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> AcceptanceArtifact:
        data = _mapping(value, "acceptance artifact")
        _exact_fields(
            data,
            {"checks"},
            "acceptance artifact",
        )
        checks_data = _mapping(data.get("checks"), "checks")
        _exact_fields(checks_data, {"e2e", "standards", "spec"}, "checks")
        checks: dict[str, dict[str, Any]] = {}
        for lane in ("e2e", "standards", "spec"):
            result = _mapping(checks_data[lane], f"{lane} check")
            _exact_fields(
                result, {"status", "evidence", "findings"}, f"{lane} check"
            )
            status = _nonempty_string(result, "status")
            if status not in {"pass", "fail", "blocked"}:
                raise ValueError(f"invalid {lane} check status")
            evidence = _nonempty_string(result, "evidence")
            findings = _string_list(result, "findings")
            if status == "pass" and findings:
                raise ValueError(f"{lane} pass check must not contain findings")
            if status == "fail" and not findings:
                raise ValueError(f"{lane} fail check requires findings")
            if status == "blocked" and findings:
                raise ValueError(f"{lane} blocked check must not contain findings")
            checks[lane] = {
                "status": status,
                "evidence": evidence,
                "findings": findings,
            }
        return cls(
            checks=checks,
            raw=dict(data),
        )

    @property
    def has_failures(self) -> bool:
        return any(check["status"] == "fail" for check in self.checks.values())

    @property
    def requires_human(self) -> bool:
        return not self.has_failures and any(
            check["status"] == "blocked" for check in self.checks.values()
        )

    @property
    def is_accepted(self) -> bool:
        return not self.has_failures and not self.requires_human

    @property
    def outcome(self) -> str:
        if self.has_failures:
            return "findings"
        if self.requires_human:
            return "blocked"
        return "pass"

    @property
    def blocker_evidence(self) -> tuple[str, ...]:
        return tuple(
            str(check["evidence"])
            for check in self.checks.values()
            if check["status"] == "blocked"
        )


def parse_publication_wire_result(value: object) -> dict[str, Any]:
    """Validate and normalize the flat Publication Structured Output contract."""
    data = _mapping(value, "publication result")
    _exact_fields(data, _PUBLICATION_RESULT_FIELDS, "publication result")
    result_kind = data.get("result_kind")
    if result_kind == "publication":
        if data.get("human_blockers") is not None:
            raise ValueError("publication result human_blockers must be null")
        return {
            "result_kind": "publication",
            "commit_message": _nonempty_string(data, "commit_message"),
            "pr_title": _nonempty_string(data, "pr_title"),
            "pr_body_markdown": _nonempty_string(data, "pr_body_markdown"),
            "human_blockers": None,
        }
    if result_kind == "human_blocker":
        for field in ("commit_message", "pr_title", "pr_body_markdown"):
            if data.get(field) is not None:
                raise ValueError(f"human blocker {field} must be null")
        blockers = _bounded_blocker_list(data.get("human_blockers"))
        if not blockers:
            raise ValueError("human_blockers must contain non-empty strings")
        return {
            "result_kind": "human_blocker",
            "commit_message": None,
            "pr_title": None,
            "pr_body_markdown": None,
            "human_blockers": blockers,
        }
    raise ValueError("invalid publication result_kind")


def parse_development_wire_result(value: object) -> dict[str, Any]:
    """Validate the flat Development/Human Blocker Structured Output contract."""

    data = _mapping(value, "development result")
    _exact_fields(data, _DEVELOPMENT_RESULT_FIELDS, "development result")
    result_kind = data.get("result_kind")
    if result_kind == "development":
        if data.get("human_blockers") is not None:
            raise ValueError("development result human_blockers must be null")
        return {
            "result_kind": "development",
            "summary": _nonempty_string(data, "summary"),
            "human_blockers": None,
        }
    if result_kind == "human_blocker":
        if data.get("summary") is not None:
            raise ValueError("human blocker summary must be null")
        blockers = _bounded_blocker_list(data.get("human_blockers"))
        if not blockers:
            raise ValueError("human_blockers must contain non-empty strings")
        return {
            "result_kind": "human_blocker",
            "summary": None,
            "human_blockers": blockers,
        }
    raise ValueError("invalid development result_kind")


def parse_human_blockers(value: object) -> tuple[str, ...] | None:
    """Return the unified Publication Human Blocker alternative."""
    if not isinstance(value, dict):
        return None
    if "result_kind" not in value:
        if "human_blockers" in value:
            raise ValueError("human blocker result is missing result_kind")
        return None
    normalized = parse_publication_wire_result(value)
    if normalized["result_kind"] != "human_blocker":
        return None
    blockers = _bounded_blocker_list(normalized["human_blockers"])
    return tuple(blockers)


def append_human_blocker_history(
    subject: dict[str, Any], *, phase: str, blockers: tuple[str, ...]
) -> None:
    """Keep only the recent raw blocker attempts needed for operator history."""
    history = subject.setdefault("human_blocker_history", [])
    if not isinstance(history, list):
        raise ValueError("human_blocker_history must be a list")
    history[:] = _valid_human_blocker_history(history)
    current = _bounded_blocker_list(list(blockers))
    if not current:
        raise ValueError("human_blockers must contain non-empty strings")
    history.append({"phase": phase, "human_blockers": current})
    subject.pop("current_human_response", None)
    if len(history) > MAX_HUMAN_BLOCKER_HISTORY:
        del history[:-MAX_HUMAN_BLOCKER_HISTORY]


def clear_current_human_blocker(subject: dict[str, Any]) -> None:
    """Clear the resolved alert while retaining bounded historical attempts."""
    for key in (
        "human_blockers",
        "human_blocker_phase",
        "prior_human_blockers",
        "current_human_response",
    ):
        subject.pop(key, None)
    if subject.get("blocked_reason") in {
        "agent_requires_human",
        "reviewer_requires_human",
    }:
        subject.pop("blocked_reason", None)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be an object")
    return value


def _exact_fields(
    data: dict[str, Any], expected: set[str], name: str
) -> None:
    actual = set(data)
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        details = []
        if unexpected:
            details.append(f"unexpected fields: {', '.join(unexpected)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise ValueError(f"{name} has {'; '.join(details)}")


def _nonempty_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if key == "human_blockers":
        return _bounded_blocker_list(value)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must contain non-empty strings")
    return [item.strip() for item in value]


def _bounded_blocker_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("human_blockers must contain non-empty strings")
    if len(value) > MAX_HUMAN_BLOCKERS:
        raise ValueError(f"human_blockers must contain at most {MAX_HUMAN_BLOCKERS} items")
    if any(len(item) > MAX_HUMAN_BLOCKER_LENGTH for item in value):
        raise ValueError(
            "each human blocker must contain at most "
            f"{MAX_HUMAN_BLOCKER_LENGTH} characters"
        )
    return list(value)


def _valid_human_blocker_history(value: list[object]) -> list[dict[str, object]]:
    valid: list[dict[str, object]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        phase = entry.get("phase")
        if not isinstance(phase, str) or not phase:
            continue
        try:
            blockers = _bounded_blocker_list(entry.get("human_blockers"))
        except ValueError:
            continue
        if blockers:
            valid.append({"phase": phase, "human_blockers": blockers})
    return valid[-(MAX_HUMAN_BLOCKER_HISTORY - 1) :]
