from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_run.github import GitHubReadError
from agent_run.models import Blocker, DeliveryGraph, Issue, ParentIssue, Repository


class FixtureGitHubReader:
    """供黑盒测试使用的确定性 GitHub 只读适配器。"""

    def __init__(self, path: Path) -> None:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise GitHubReadError("invalid_fixture", "fixture root must be an object")
        self.data = loaded

    def repository(self) -> Repository:
        default_head = self.data.get("default_head_sha")
        return Repository(
            name_with_owner=_string(self.data, "repository"),
            default_branch=_string(self.data, "default_branch"),
            default_head_sha=default_head if isinstance(default_head, str) else None,
        )

    def delivery_graph(self, parent_number: int) -> DeliveryGraph:
        configured_error = self.data.get("error")
        if isinstance(configured_error, dict):
            raise GitHubReadError(
                str(configured_error.get("code", "github_read_failed")),
                str(configured_error.get("message", "GitHub read failed")),
            )
        parent_data = _mapping(self.data, "parent")
        actual_parent = _integer(parent_data, "number")
        if actual_parent != parent_number:
            raise GitHubReadError(
                "missing_parent",
                f"fixture contains parent #{actual_parent}, not #{parent_number}",
            )
        sub_issues = parent_data.get("sub_issues")
        if not isinstance(sub_issues, list) or not all(
            isinstance(number, int) for number in sub_issues
        ):
            raise GitHubReadError(
                "invalid_parent", "parent.sub_issues must contain issue numbers"
            )
        parent = ParentIssue(
            number=actual_parent,
            title=_string(parent_data, "title"),
            body=_string(parent_data, "body"),
            sub_issue_numbers=tuple(sub_issues),
            sub_issue_order_reliable=bool(
                parent_data.get("sub_issue_order_reliable", True)
            ),
        )
        raw_issues = _mapping(self.data, "issues")
        issues: dict[int, Issue] = {}
        for number in sub_issues:
            raw_issue = raw_issues.get(str(number))
            if raw_issue is None:
                continue
            if not isinstance(raw_issue, dict):
                raise GitHubReadError(
                    "invalid_fixture", f"issues.{number} must be an object"
                )
            issues[number] = _parse_issue(raw_issue)
        return DeliveryGraph(parent=parent, issues=issues)


def _parse_issue(data: dict[str, Any]) -> Issue:
    raw_labels = data.get("labels")
    raw_blockers = data.get("blocked_by")
    if not isinstance(raw_labels, list) or not all(
        isinstance(label, str) for label in raw_labels
    ):
        raise GitHubReadError("invalid_fixture", "issue.labels must contain strings")
    if not isinstance(raw_blockers, list):
        raise GitHubReadError("invalid_fixture", "issue.blocked_by must be a list")
    return Issue(
        number=_integer(data, "number"),
        title=_string(data, "title"),
        body=_string(data, "body"),
        state=_string(data, "state"),
        labels=frozenset(raw_labels),
        blocked_by=tuple(
            Blocker(
                number=_integer(_as_mapping(blocker), "number"),
                state=_string(_as_mapping(blocker), "state"),
            )
            for blocker in raw_blockers
        ),
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise GitHubReadError("invalid_fixture", f"{key} must be an object")
    return value


def _as_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("invalid_fixture", "blocker must be an object")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise GitHubReadError("invalid_fixture", f"{key} must be a string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubReadError("invalid_fixture", f"{key} must be an integer")
    return value
