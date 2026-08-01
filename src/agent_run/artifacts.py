from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SEMANTIC_TITLE = re.compile(
    r"^(feat|fix|improve|refactor|docs|test|chore)"
    r"(?:\([a-z0-9][a-z0-9._-]*\))?: .+"
)
_CLOSING_KEYWORD = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+\b"
)
_REQUIRED_SECTIONS = (
    "What Problem This Solves",
    "Why This Change Was Made",
    "User Impact",
    "Evidence",
)


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
        final_run: bool = False,
    ) -> PublicationArtifact:
        data = _mapping(value, "publication artifact")
        commit_message = _nonempty_string(data, "commit_message")
        pr_title = _nonempty_string(data, "pr_title")
        body = _nonempty_string(data, "pr_body_markdown")
        if not _SEMANTIC_TITLE.fullmatch(commit_message):
            raise ValueError(
                "commit_message does not match the semantic title contract"
            )
        _require_meaningful_outcome(commit_message, "commit_message")
        if not _SEMANTIC_TITLE.fullmatch(pr_title):
            raise ValueError("pr_title does not match the semantic title contract")
        _require_meaningful_outcome(pr_title, "pr_title")
        if (primary_ticket is None) == (delivery_run is None):
            raise ValueError("publication artifact requires exactly one identity")
        if primary_ticket is not None:
            identity = f"Primary Ticket: #{primary_ticket}"
            identity_lines = [
                line.strip()
                for line in body.splitlines()
                if line.strip().startswith("Primary Ticket:")
            ]
            if identity_lines != [identity]:
                raise ValueError("PR body must identify exactly one Primary Ticket")
        else:
            identity = f"Delivery Run: {delivery_run}"
            if body.splitlines()[0].strip() != identity:
                raise ValueError("Run PR body must start with Delivery Run")
        if _CLOSING_KEYWORD.search(body):
            raise ValueError("PR body must not contain automatic closing keywords")
        for section in _REQUIRED_SECTIONS:
            _require_nonempty_section(body, section)
        if final_run:
            for section in (
                "Completed Tickets",
                "Known Limitations",
                "Validation Results",
            ):
                _require_nonempty_section(body, section)
        return cls(
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body_markdown=body,
        )


@dataclass(frozen=True)
class AcceptanceArtifact:
    verdict: str
    checks: dict[str, dict[str, str]]
    findings: tuple[dict[str, Any], ...]
    human_blockers: tuple[str, ...]
    raw: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> AcceptanceArtifact:
        data = _mapping(value, "acceptance artifact")
        _exact_fields(
            data,
            {"verdict", "checks", "findings", "human_blockers"},
            "acceptance artifact",
        )
        verdict = _nonempty_string(data, "verdict")
        if verdict not in {"pass", "request_changes", "human"}:
            raise ValueError("invalid acceptance verdict")

        checks_data = _mapping(data.get("checks"), "checks")
        _exact_fields(checks_data, {"e2e", "standards", "spec"}, "checks")
        checks: dict[str, dict[str, str]] = {}
        for lane in ("e2e", "standards", "spec"):
            result = _mapping(checks_data[lane], f"{lane} check")
            _exact_fields(result, {"status", "evidence"}, f"{lane} check")
            status = _nonempty_string(result, "status")
            if status not in {"pass", "fail", "blocked"}:
                raise ValueError(f"invalid {lane} check status")
            evidence = _nonempty_string(result, "evidence")
            checks[lane] = {"status": status, "evidence": evidence}

        findings = _mapping_list(data, "findings")
        for finding in findings:
            _exact_fields(
                finding,
                {
                    "id",
                    "problem",
                    "evidence",
                    "required_outcome",
                    "verification",
                },
                "finding",
            )
            for key in (
                "id",
                "problem",
                "evidence",
                "required_outcome",
                "verification",
            ):
                _nonempty_string(finding, key)
        blockers = _string_list(data, "human_blockers")
        statuses = {result["status"] for result in checks.values()}
        if verdict == "pass" and (findings or blockers):
            raise ValueError("passing acceptance must not contain repair work")
        if verdict == "pass" and statuses != {"pass"}:
            raise ValueError("passing acceptance requires every check to pass")
        if verdict == "request_changes" and not findings:
            raise ValueError("request_changes requires findings")
        if verdict == "request_changes" and "fail" not in statuses:
            raise ValueError("request_changes requires a failed check")
        if verdict == "request_changes" and blockers:
            raise ValueError("request_changes must not contain human blockers")
        if verdict == "human" and (not blockers or "blocked" not in statuses):
            raise ValueError(
                "human verdict requires human_blockers and a blocked check"
            )
        return cls(
            verdict=verdict,
            checks=checks,
            findings=tuple(findings),
            human_blockers=tuple(blockers),
            raw=dict(data),
        )


def _require_nonempty_section(body: str, title: str) -> None:
    pattern = re.compile(
        rf"(?ms)^## {re.escape(title)}\s*\n+(.+?)(?=^## |\Z)"
    )
    match = pattern.search(body)
    if match is None or not match.group(1).strip():
        raise ValueError(f"PR body section {title!r} must be non-empty")


def _require_meaningful_outcome(value: str, field: str) -> None:
    outcome = value.split(":", 1)[1].strip()
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", outcome))
    word_count = len(re.findall(r"[A-Za-z0-9]+", outcome))
    if len(outcome) < 8 and cjk_count < 4 and word_count < 3:
        raise ValueError(f"{field} must describe a meaningful user outcome")


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


def _mapping_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return [_mapping(item, f"{key} item") for item in value]


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must contain non-empty strings")
    return [item.strip() for item in value]
