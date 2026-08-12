from __future__ import annotations

import pytest

from agent_run.agent_schemas import (
    acceptance_schema,
    human_blocker_schema,
    publication_or_human_blocker_schema,
)
from agent_run.artifacts import (
    MAX_HUMAN_BLOCKER_HISTORY,
    MAX_HUMAN_BLOCKER_LENGTH,
    MAX_HUMAN_BLOCKERS,
    AcceptanceArtifact,
    PublicationArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
    parse_human_blockers,
    parse_publication_wire_result,
)


def publication_data() -> dict[str, object]:
    return {
        "result_kind": "publication",
        "commit_message": "feat(delivery): complete one ticket autonomously",
        "pr_title": "feat(delivery): complete one ticket autonomously",
        "pr_body_markdown": """
## What Problem This Solves

The active ticket previously stopped before delivery.

## Why This Change Was Made

One bounded engine now owns the repair and publication loop.

## User Impact

Maintainers can deliver the ticket without manual GitHub mutations.

## Evidence

`pytest tests/test_delivery.py` passed.
""".strip(),
        "human_blockers": None,
    }


def test_publication_artifact_enforces_ticket_narrative_contract() -> None:
    artifact = PublicationArtifact.parse(publication_data(), primary_ticket=3)

    assert artifact.commit_message.startswith("feat(delivery):")
    assert artifact.pr_title == artifact.commit_message


def test_publication_wire_schema_is_a_flat_strict_object() -> None:
    schema = publication_or_human_blocker_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "oneOf" not in schema
    assert schema["properties"]["human_blockers"]["type"] == ["array", "null"]
    assert "anyOf" not in schema
    assert schema["required"] == [
        "result_kind",
        "commit_message",
        "pr_title",
        "pr_body_markdown",
        "human_blockers",
    ]
    assert schema["properties"]["result_kind"]["enum"] == [
        "publication",
        "human_blocker",
    ]


def test_publication_result_rejects_blockers_and_unexpected_fields() -> None:
    data = publication_data()
    data["human_blockers"] = ["Cannot publish."]
    with pytest.raises(ValueError, match="must be null"):
        PublicationArtifact.parse(data, primary_ticket=3)

    data = publication_data()
    data["extra"] = "unexpected"
    with pytest.raises(ValueError, match="unexpected fields"):
        PublicationArtifact.parse(data, primary_ticket=3)


def test_wire_parser_normalizes_publication_without_requiring_identity() -> None:
    data = publication_data()
    data["commit_message"] = "  feat(delivery): complete one ticket autonomously  "

    normalized = parse_publication_wire_result(data)

    assert normalized["result_kind"] == "publication"
    assert normalized["commit_message"] == (
        "feat(delivery): complete one ticket autonomously"
    )
    assert normalized["human_blockers"] is None


def test_wire_parser_rejects_empty_human_blocker_result() -> None:
    with pytest.raises(ValueError, match="must contain non-empty strings"):
        parse_publication_wire_result(human_blocker_data([]))


@pytest.mark.parametrize("field", ["commit_message", "pr_title", "pr_body_markdown"])
def test_human_blocker_result_requires_null_publication_fields(field: str) -> None:
    data: dict[str, object] = {
        "result_kind": "human_blocker",
        "commit_message": None,
        "pr_title": None,
        "pr_body_markdown": None,
        "human_blockers": ["A maintainer must grant access."],
    }
    data[field] = "must not leak publication content"

    with pytest.raises(ValueError, match="must be null"):
        parse_human_blockers(data)


def test_human_blocker_result_is_normalized_without_trimming() -> None:
    data = {
        "result_kind": "human_blocker",
        "commit_message": None,
        "pr_title": None,
        "pr_body_markdown": None,
        "human_blockers": ["  A maintainer must grant access.  "],
    }

    assert parse_human_blockers(data) == (
        "  A maintainer must grant access.  ",
    )


def test_legacy_human_blocker_shape_remains_supported() -> None:
    assert parse_human_blockers({"human_blockers": ["Needs approval."]}) == (
        "Needs approval.",
    )


def test_wire_result_requires_every_field() -> None:
    data = publication_data()
    del data["human_blockers"]

    with pytest.raises(ValueError, match="missing fields"):
        PublicationArtifact.parse(data, primary_ticket=3)


@pytest.mark.parametrize(
    "machine_fact",
    [
        "Parent Issue: #1",
        "Primary Ticket: #3",
        "Delivery Type: Ticket",
        "Delivery Run: run-1",
    ],
)
def test_publication_artifact_rejects_publisher_owned_facts(
    machine_fact: str,
) -> None:
    data = publication_data()
    data["pr_body_markdown"] = f"{machine_fact}\n\n{data['pr_body_markdown']}"

    with pytest.raises(ValueError):
        PublicationArtifact.parse(data, primary_ticket=3)


def test_run_publication_artifact_rejects_publisher_owned_facts() -> None:
    data = publication_data()
    data["pr_body_markdown"] = f"Delivery Run: run-1\n\n{data['pr_body_markdown']}"

    with pytest.raises(ValueError):
        PublicationArtifact.parse(data, delivery_run="run-1")


def test_ticket_publication_artifact_rejects_closing_keyword() -> None:
    data = publication_data()
    data["pr_body_markdown"] += "\n\nCloses #3"

    with pytest.raises(ValueError):
        PublicationArtifact.parse(data, primary_ticket=3)


def passing_acceptance() -> dict[str, object]:
    return {
        "verdict": "pass",
        "checks": {
            "e2e": {
                "status": "pass",
                "evidence": "The black-box scenario passed.",
            },
            "standards": {
                "status": "pass",
                "evidence": "The independent standards review passed.",
            },
            "spec": {
                "status": "pass",
                "evidence": "The independent spec review passed.",
            },
        },
        "findings": [],
        "human_blockers": [],
    }


def human_blocker_data(blockers: list[str]) -> dict[str, object]:
    return {
        "result_kind": "human_blocker",
        "commit_message": None,
        "pr_title": None,
        "pr_body_markdown": None,
        "human_blockers": blockers,
    }


def test_acceptance_artifact_has_three_evidence_backed_lanes() -> None:
    artifact = AcceptanceArtifact.parse(passing_acceptance())

    assert artifact.verdict == "pass"
    assert set(artifact.checks) == {"e2e", "standards", "spec"}
    assert artifact.checks["e2e"]["evidence"] == "The black-box scenario passed."


def test_publication_rejects_non_semantic_commit_message() -> None:
    data = publication_data()
    data["commit_message"] = "feat: x"

    with pytest.raises(ValueError, match="commit_message"):
        PublicationArtifact.parse(data, primary_ticket=3)


def test_passing_acceptance_rejects_failed_check() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The black-box scenario failed.",
    }

    with pytest.raises(ValueError, match="every check"):
        AcceptanceArtifact.parse(data)


def test_request_changes_requires_self_contained_findings() -> None:
    data = passing_acceptance()
    data["verdict"] = "request_changes"
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["spec"] = {
        "status": "fail",
        "evidence": "The requested user flow is missing.",
    }
    data["findings"] = [
        {
            "id": "F1",
            "problem": "The user flow is missing.",
            "evidence": "The E2E command cannot exercise it.",
            "required_outcome": "Expose the requested flow.",
            "verification": "Run the E2E command successfully.",
        }
    ]

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.findings[0]["id"] == "F1"


@pytest.mark.parametrize(
    "legacy_field",
    [
        "acceptance_scope",
        "reviewed_base_sha",
        "reviewed_head_sha",
        "effective_revision",
        "criteria",
        "repair_brief",
    ],
)
def test_acceptance_artifact_rejects_controller_owned_or_legacy_fields(
    legacy_field: str,
) -> None:
    data = passing_acceptance()
    data[legacy_field] = "legacy"

    with pytest.raises(ValueError, match="unexpected fields"):
        AcceptanceArtifact.parse(data)


def test_human_verdict_requires_a_blocked_lane_and_human_blocker() -> None:
    data = passing_acceptance()
    data["verdict"] = "human"
    data["human_blockers"] = ["A maintainer must grant an external permission."]

    with pytest.raises(ValueError, match="blocked check"):
        AcceptanceArtifact.parse(data)


def test_human_verdict_rejects_a_repairable_failed_lane() -> None:
    data = passing_acceptance()
    data["verdict"] = "human"
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {"status": "fail", "evidence": "The user path is broken."}
    checks["spec"] = {"status": "blocked", "evidence": "Needs maintainer permission."}
    data["human_blockers"] = ["A maintainer must grant external permission."]

    with pytest.raises(ValueError, match="no failed check"):
        AcceptanceArtifact.parse(data)


def test_human_verdict_rejects_repair_findings() -> None:
    data = passing_acceptance()
    data["verdict"] = "human"
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {"status": "blocked", "evidence": "Needs approval."}
    data["human_blockers"] = ["A maintainer must approve external access."]
    data["findings"] = [
        {
            "id": "F1",
            "problem": "A repairable problem.",
            "evidence": "Observed in the candidate.",
            "required_outcome": "Repair it.",
            "verification": "Run the flow.",
        }
    ]

    with pytest.raises(ValueError, match="human.*findings"):
        AcceptanceArtifact.parse(data)


def test_request_changes_rejects_blocked_only_checks() -> None:
    data = passing_acceptance()
    data["verdict"] = "request_changes"
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": "The local service was unavailable.",
    }
    data["findings"] = [
        {
            "id": "F1",
            "problem": "The E2E service is unavailable.",
            "evidence": "The service process is not running.",
            "required_outcome": "Make the service available.",
            "verification": "Run the E2E flow.",
        }
    ]

    with pytest.raises(ValueError, match="failed check"):
        AcceptanceArtifact.parse(data)


def test_human_blockers_have_bounded_count_length_and_history() -> None:
    blockers = ["x" * MAX_HUMAN_BLOCKER_LENGTH] * MAX_HUMAN_BLOCKERS
    assert parse_human_blockers(human_blocker_data(blockers)) == tuple(blockers)

    with pytest.raises(ValueError, match="at most"):
        parse_human_blockers(human_blocker_data(blockers + ["one too many"]))
    with pytest.raises(ValueError, match="at most"):
        parse_human_blockers(
            human_blocker_data(["x" * (MAX_HUMAN_BLOCKER_LENGTH + 1)])
        )

    subject: dict[str, object] = {
        "human_blockers": ["current blocker"],
        "human_blocker_phase": "developing",
        "prior_human_blockers": ["current blocker"],
        "blocked_reason": "agent_requires_human",
        "human_blocker_history": [
            {
                "phase": "legacy",
                "human_blockers": ["x" * (MAX_HUMAN_BLOCKER_LENGTH + 1)],
            }
        ],
    }
    for attempt in range(MAX_HUMAN_BLOCKER_HISTORY + 3):
        append_human_blocker_history(
            subject, phase=f"attempt-{attempt}", blockers=(f"blocker-{attempt}",)
        )

    history = subject["human_blocker_history"]
    assert isinstance(history, list)
    assert len(history) == MAX_HUMAN_BLOCKER_HISTORY
    assert history[0]["phase"] == "attempt-3"
    assert history[-1]["phase"] == (
        f"attempt-{MAX_HUMAN_BLOCKER_HISTORY + 2}"
    )

    clear_current_human_blocker(subject)
    assert "human_blocker_history" in subject
    for key in (
        "human_blockers",
        "human_blocker_phase",
        "prior_human_blockers",
        "blocked_reason",
    ):
        assert key not in subject


def test_human_blocker_schemas_reject_whitespace_only_strings() -> None:
    blocker_item = human_blocker_schema()["properties"]["human_blockers"]["items"]
    acceptance_item = acceptance_schema()["properties"]["human_blockers"]["items"]

    assert blocker_item["minLength"] == 1
    assert blocker_item["pattern"] == r"\S"
    assert acceptance_item["minLength"] == 1
    assert acceptance_item["pattern"] == r"\S"


def test_human_acceptance_preserves_raw_blocker_text_within_limits() -> None:
    data = passing_acceptance()
    data["verdict"] = "human"
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {"status": "blocked", "evidence": "Permission is missing."}
    data["human_blockers"] = ["  preserve surrounding spaces  "]

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.human_blockers == ("  preserve surrounding spaces  ",)
