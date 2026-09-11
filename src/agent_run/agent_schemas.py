from __future__ import annotations

from typing import Any

from agent_run.artifacts import MAX_HUMAN_BLOCKER_LENGTH, MAX_HUMAN_BLOCKERS


def publication_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["commit_message", "pr_title", "pr_body_markdown"],
        "properties": {
            key: {"type": "string", "minLength": 1, "pattern": r"\S"}
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


def development_or_human_blocker_schema() -> dict[str, Any]:
    """Wire contract for a Development result and its one semantic escape hatch."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["result_kind", "summary", "human_blockers"],
        "properties": {
            "result_kind": {
                "type": "string",
                "enum": ["development", "human_blocker"],
            },
            "summary": {"type": ["string", "null"], "minLength": 1, "pattern": r"\S"},
            "human_blockers": {
                "type": ["array", "null"],
                "minItems": 1,
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
                key: {"type": ["string", "null"], "minLength": 1, "pattern": r"\S"}
                for key in ("commit_message", "pr_title", "pr_body_markdown")
            },
            "human_blockers": {
                "type": ["array", "null"],
                "minItems": 1,
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
    lane = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "evidence", "findings"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pass", "fail", "blocked"],
            },
            "evidence": {"type": "string", "minLength": 1, "pattern": r"\S"},
            "findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "pattern": r"\S"},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks"],
        "properties": {
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": ["e2e", "standards", "spec"],
                "properties": {
                    name: {"$ref": "#/$defs/lane"}
                    for name in ("e2e", "standards", "spec")
                },
            },
        },
        "$defs": {"lane": lane},
    }
