from __future__ import annotations

from typing import Any

from agent_run.artifacts import MAX_HUMAN_BLOCKER_LENGTH, MAX_HUMAN_BLOCKERS


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


def human_blocker_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["human_blockers"],
        "properties": {
            "human_blockers": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_HUMAN_BLOCKERS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_HUMAN_BLOCKER_LENGTH,
                    "pattern": r"\S",
                },
            }
        },
    }


def publication_or_human_blocker_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "result_kind",
            "commit_message",
            "pr_title",
            "pr_body_markdown",
            "human_blockers",
        ],
        "properties": {
            "result_kind": {
                "type": "string",
                "enum": ["publication", "human_blocker"],
            },
            **{
                key: {"type": ["string", "null"]}
                for key in ("commit_message", "pr_title", "pr_body_markdown")
            },
            "human_blockers": {
                "type": ["array", "null"],
                "maxItems": MAX_HUMAN_BLOCKERS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_HUMAN_BLOCKER_LENGTH,
                    "pattern": r"\S",
                },
            },
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
                "maxItems": MAX_HUMAN_BLOCKERS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_HUMAN_BLOCKER_LENGTH,
                    "pattern": r"\S",
                },
            },
        },
    }
