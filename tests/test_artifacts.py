from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from agent_run import change_delivery, run_acceptance
from agent_run.agent_schemas import (
    acceptance_schema,
    development_or_human_blocker_schema,
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
    parse_development_wire_result,
    parse_publication_wire_result,
)


PASS_EVIDENCE = {
    "e2e": "操作或命令：pytest tests/test_artifacts.py；退出码：0；结果：黑盒场景通过。",
    "standards": "审查范围或基线：仓库编码规范与变更 diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：三个 Acceptance lanes；覆盖结论：全部满足。",
}


def _lane_evidence(lane: str, status: str) -> str:
    if status == "pass":
        return PASS_EVIDENCE[lane]
    if status == "blocked":
        return "发生：访问被拒绝；尝试：重新执行验收；人必须：授予访问权限。"
    return f"{lane} lane failed while evaluating the candidate."


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


def test_development_wire_schema_and_parser_are_flat_and_mutually_exclusive() -> None:
    schema = development_or_human_blocker_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["result_kind", "summary", "human_blockers"]
    assert "oneOf" not in schema
    assert parse_development_wire_result(
        {
            "result_kind": "development",
            "summary": "Implemented the requested change.",
            "human_blockers": None,
        }
    )["summary"] == "Implemented the requested change."
    with pytest.raises(ValueError, match="must be null"):
        parse_development_wire_result(
            {
                "result_kind": "development",
                "summary": "Implemented the requested change.",
                "human_blockers": ["mixed result"],
            }
        )


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


def test_human_blocker_rejects_the_removed_legacy_wire_shape() -> None:
    with pytest.raises(ValueError, match="missing result_kind"):
        parse_human_blockers({"human_blockers": ["Needs approval."]})


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
        "checks": {
            "e2e": {
                "status": "pass",
                "evidence": PASS_EVIDENCE["e2e"],
                "findings": [],
            },
            "standards": {
                "status": "pass",
                "evidence": PASS_EVIDENCE["standards"],
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": PASS_EVIDENCE["spec"],
                "findings": [],
            },
        },
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

    assert set(artifact.checks) == {"e2e", "standards", "spec"}
    assert artifact.checks["e2e"]["evidence"] == PASS_EVIDENCE["e2e"]
    assert all(check["findings"] == [] for check in artifact.checks.values())


def test_ticket_and_run_acceptance_share_the_strict_artifact_parser() -> None:
    assert change_delivery.AcceptanceArtifact is AcceptanceArtifact
    assert run_acceptance.AcceptanceArtifact is AcceptanceArtifact


def test_publication_rejects_non_semantic_commit_message() -> None:
    data = publication_data()
    data["commit_message"] = "feat: x"

    with pytest.raises(ValueError, match="commit_message"):
        PublicationArtifact.parse(data, primary_ticket=3)


def test_failed_lane_requires_self_contained_findings() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The black-box scenario failed.",
        "findings": [],
    }

    with pytest.raises(ValueError, match="requires findings"):
        AcceptanceArtifact.parse(data)


def test_failed_lane_keeps_self_contained_finding() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["spec"] = {
        "status": "fail",
        "evidence": "The requested user flow is missing.",
        "findings": [
            "问题：The user flow is missing；证据：The E2E command cannot exercise it；必须修复：Expose the requested flow；复验：Run the E2E command successfully",
        ],
    }

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.checks["spec"]["findings"] == checks["spec"]["findings"]


@pytest.mark.parametrize(
    "statuses",
    list(product(("pass", "fail", "blocked"), repeat=3)),
    ids=lambda statuses: "-".join(statuses),
)
def test_all_lane_status_combinations_enforce_the_findings_relationship(
    statuses: tuple[str, str, str],
) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    for lane, status in zip(("e2e", "standards", "spec"), statuses, strict=True):
        checks[lane] = {
            "status": status,
            "evidence": _lane_evidence(lane, status),
            "findings": (
                ["问题：候选实现不符合要求；证据：独立复验失败；必须修复：修复候选实现；复验：重新执行独立复验。"]
                if status == "fail"
                else []
            ),
        }

    artifact = AcceptanceArtifact.parse(data)

    assert tuple(artifact.checks[lane]["status"] for lane in checks) == statuses


@pytest.mark.parametrize("lane", ("e2e", "standards", "spec"))
@pytest.mark.parametrize("status", ("pass", "fail", "blocked"))
def test_lane_rejects_empty_evidence_for_every_status_and_lane(
    lane: str, status: str
) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks[lane] = {
        "status": status,
        "evidence": " ",
        "findings": (
            ["问题：x；证据：x；必须修复：x；复验：x"]
            if status == "fail"
            else []
        ),
    }

    with pytest.raises(ValueError, match="evidence must be a non-empty string"):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize("lane", ("e2e", "standards", "spec"))
def test_pass_lane_requires_lane_specific_reviewable_evidence(lane: str) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    lane_data = checks[lane]
    assert isinstance(lane_data, dict)
    lane_data["evidence"] = "The review passed."

    with pytest.raises(ValueError, match="pass evidence"):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize(
    ("lane", "evidence"),
    [
        ("e2e", "操作或命令：；退出码：0；结果：通过。"),
        ("standards", "审查范围或基线：当前 diff；结论："),
        ("spec", "已核对的验收标准：三个 lanes；覆盖结论："),
    ],
)
def test_pass_lane_rejects_empty_reviewable_evidence_values(
    lane: str, evidence: str
) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    lane_data = checks[lane]
    assert isinstance(lane_data, dict)
    lane_data["evidence"] = evidence

    with pytest.raises(ValueError, match="pass evidence"):
        AcceptanceArtifact.parse(data)


def test_failure_returns_to_development_before_blocked_lanes_route_to_human() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The public flow fails.",
        "findings": ["问题：flow fails；证据：command exits 1；必须修复：restore flow；复验：run command"],
    }
    checks["spec"] = {
        "status": "blocked",
        "evidence": "发生：spec access is missing；尝试：ran gh issue view；人必须：grant access.",
        "findings": [],
    }

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.has_failures is True
    assert artifact.requires_human is False
    assert artifact.is_accepted is False
    assert artifact.outcome == "findings"


@pytest.mark.parametrize(
    "legacy_field",
    [
        "acceptance_scope",
        "reviewed_base_sha",
        "reviewed_head_sha",
        "effective_revision",
        "criteria",
        "repair_brief",
        "verdict",
        "findings",
        "human_blockers",
    ],
)
def test_acceptance_artifact_rejects_controller_owned_or_legacy_fields(
    legacy_field: str,
) -> None:
    data = passing_acceptance()
    data[legacy_field] = "legacy"

    with pytest.raises(ValueError, match="unexpected fields"):
        AcceptanceArtifact.parse(data)


def test_blocked_lane_requires_all_human_handoff_details() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": "发生：permission is missing；尝试：re-ran the flow；人必须：grant access.",
        "findings": [],
    }

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.requires_human is True
    assert artifact.blocker_evidence == (checks["e2e"]["evidence"],)


def test_blocked_lane_rejects_incomplete_human_handoff() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": "发生：permission is missing；尝试：re-ran the flow.",
        "findings": [],
    }

    with pytest.raises(ValueError, match="blocked evidence"):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize(
    ("status", "findings", "error"),
    [
        ("pass", ["问题：x；证据：x；必须修复：x；复验：x"], "pass check"),
        ("blocked", ["问题：x；证据：x；必须修复：x；复验：x"], "blocked check"),
        ("fail", ["unstructured"], "findings must use"),
    ],
)
def test_lane_findings_follow_status_and_format_contract(
    status: str, findings: list[str], error: str
) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": status,
        "evidence": (
            "发生：approval is missing；尝试：re-ran the flow；人必须：approve access."
            if status == "blocked"
            else "The lane was evaluated."
        ),
        "findings": findings,
    }

    with pytest.raises(ValueError, match=error):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize("lane", ("e2e", "standards", "spec"))
@pytest.mark.parametrize(
    ("status", "findings", "error"),
    [
        ("pass", ["问题：x；证据：x；必须修复：x；复验：x"], "pass check"),
        ("fail", [], "requires findings"),
        ("blocked", ["问题：x；证据：x；必须修复：x；复验：x"], "blocked check"),
    ],
)
def test_each_lane_enforces_its_status_to_findings_relationship(
    lane: str, status: str, findings: list[str], error: str
) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks[lane] = {
        "status": status,
        "evidence": _lane_evidence(lane, status),
        "findings": findings,
    }

    with pytest.raises(ValueError, match=error):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize(
    "finding",
    [
        "证据：独立复验失败；必须修复：修复候选实现；复验：重新执行独立复验。",
        "问题：候选实现不符合要求；必须修复：修复候选实现；复验：重新执行独立复验。",
        "问题：候选实现不符合要求；证据：独立复验失败；复验：重新执行独立复验。",
        "问题：候选实现不符合要求；证据：独立复验失败；必须修复：修复候选实现。",
    ],
)
def test_fail_finding_requires_all_four_self_contained_sections(finding: str) -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": _lane_evidence("e2e", "fail"),
        "findings": [finding],
    }

    with pytest.raises(ValueError, match="findings must use"):
        AcceptanceArtifact.parse(data)


@pytest.mark.parametrize("scope", ("root", "checks", "lane"))
def test_acceptance_artifact_rejects_nested_extra_fields(scope: str) -> None:
    data = passing_acceptance()
    if scope == "root":
        data["extra"] = "unexpected"
    else:
        checks = data["checks"]
        assert isinstance(checks, dict)
        if scope == "checks":
            checks["extra"] = {}
        else:
            lane = checks["e2e"]
            assert isinstance(lane, dict)
            lane["extra"] = "unexpected"

    with pytest.raises(ValueError, match="unexpected fields"):
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


def test_human_blocker_and_acceptance_schemas_expose_the_new_contract() -> None:
    blocker_item = human_blocker_schema()["properties"]["human_blockers"]["items"]
    schema = acceptance_schema()
    acceptance_lane = schema["$defs"]["lane"]

    assert blocker_item["minLength"] == 1
    assert blocker_item["pattern"] == r"\S"
    assert schema["required"] == ["checks"]
    assert set(schema["properties"]["checks"]["required"]) == {
        "e2e",
        "standards",
        "spec",
    }
    assert set(schema["properties"]) == {"checks"}
    assert acceptance_lane["required"] == ["status", "evidence", "findings"]
    assert acceptance_lane["properties"]["findings"]["items"] == {"type": "string"}


def test_prompt_docs_delegate_acceptance_shape_to_the_authoritative_contract() -> None:
    root = Path(__file__).parents[1]
    prompt_docs = (root / "docs/agents/agent-prompts.md").read_text(encoding="utf-8")
    contract_docs = (root / "docs/acceptance-artifact-schema.md").read_text(
        encoding="utf-8"
    )

    assert "[Acceptance Artifact Schema](../acceptance-artifact-schema.md)" in prompt_docs
    assert '"verdict"' not in prompt_docs
    assert "request_changes" not in prompt_docs
    assert '"required": ["checks"]' in contract_docs
    assert '"required": ["status", "evidence", "findings"]' in contract_docs


def test_blocked_acceptance_normalizes_evidence() -> None:
    data = passing_acceptance()
    checks = data["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": "  发生：permission is missing；尝试：re-ran the flow；人必须：grant access.  ",
        "findings": [],
    }

    artifact = AcceptanceArtifact.parse(data)

    assert artifact.blocker_evidence == (checks["e2e"]["evidence"].strip(),)
