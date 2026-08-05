from __future__ import annotations

import pytest

from agent_run.artifacts import AcceptanceArtifact, PublicationArtifact


def publication_data() -> dict[str, str]:
    return {
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
    }


def test_publication_artifact_enforces_ticket_narrative_contract() -> None:
    artifact = PublicationArtifact.parse(publication_data(), primary_ticket=3)

    assert artifact.commit_message.startswith("feat(delivery):")
    assert artifact.pr_title == artifact.commit_message


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
