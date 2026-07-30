from __future__ import annotations

from typing import Any


def publication_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["commit_message", "pr_title", "pr_body_markdown"],
        "properties": {
            key: {"type": "string"}
            for key in ("commit_message", "pr_title", "pr_body_markdown")
        },
    }


def acceptance_schema() -> dict[str, Any]:
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "problem",
            "evidence",
            "required_outcome",
            "verification",
        ],
        "properties": {
            key: {"type": "string"}
            for key in (
                "id",
                "problem",
                "evidence",
                "required_outcome",
                "verification",
            )
        },
    }
    check = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "evidence"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pass", "fail", "blocked"],
            },
            "evidence": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "checks",
            "findings",
            "human_blockers",
        ],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "request_changes", "human"],
            },
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": ["e2e", "standards", "spec"],
                "properties": {
                    lane: check for lane in ("e2e", "standards", "spec")
                },
            },
            "findings": {"type": "array", "items": finding},
            "human_blockers": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }
