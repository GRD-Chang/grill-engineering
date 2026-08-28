from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.github import GitHubReadError
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.required_checks_observation import (
    bind_new_publication_head,
    clear_required_checks_observation,
    read_required_checks_observation,
    require_required_checks_observation,
    validate_legacy_required_checks_projection,
)


@pytest.mark.parametrize("result", ["none", "pass", "pending", "unknown", "fail"])
def test_reads_one_complete_exact_head_observation(result: str) -> None:
    expected = {
        "pr_number": 17,
        "head_sha": "candidate-head",
        "result": result,
        "checks": [] if result == "none" else [{"name": "quality", "bucket": result}],
    }
    calls: list[tuple[int, str]] = []

    def snapshot(pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
        calls.append((pr_number, expected_head_sha))
        return expected

    github = SimpleNamespace(
        required_checks=lambda _pr_number: pytest.fail("aggregate read is forbidden"),
        required_checks_snapshot=snapshot,
    )

    observation = read_required_checks_observation(
        github, 17, expected_head_sha="candidate-head"
    )

    assert observation == expected
    assert observation is not expected
    assert observation["checks"] is not expected["checks"]
    assert calls == [(17, "candidate-head")]


@pytest.mark.parametrize(
    ("snapshot", "error"),
    [
        (None, "must be an object"),
        (
            {
                "pr_number": 17,
                "head_sha": "candidate-head",
                "result": "pass",
                "checks": ["not-an-object"],
            },
            "checks must contain objects",
        ),
        (
            {
                "pr_number": 17,
                "head_sha": "candidate-head",
                "result": "observed",
                "checks": [],
            },
            "result is invalid",
        ),
    ],
)
def test_rejects_incomplete_snapshot_shapes(snapshot: object, error: str) -> None:
    github = SimpleNamespace(required_checks_snapshot=lambda *_args, **_kwargs: snapshot)

    with pytest.raises(ValueError, match=error):
        read_required_checks_observation(
            github, 17, expected_head_sha="candidate-head"
        )


@pytest.mark.parametrize(
    ("pr_number", "head_sha", "code"),
    [
        (18, "candidate-head", "change_pr_identity_mismatch"),
        (17, "other-head", "change_pr_head_drift"),
    ],
)
def test_rejects_snapshot_identity_mismatch(
    pr_number: int, head_sha: str, code: str
) -> None:
    snapshot = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "result": "pass",
        "checks": [],
    }
    github = SimpleNamespace(required_checks_snapshot=lambda *_args, **_kwargs: snapshot)

    with pytest.raises(GitHubReadError) as raised:
        read_required_checks_observation(
            github, 17, expected_head_sha="candidate-head"
        )

    assert raised.value.code == code


def test_new_publication_head_clears_observation_but_keeps_failure_provenance() -> None:
    observation = {
        "pr_number": 17,
        "head_sha": "failed-head",
        "result": "fail",
        "checks": [{"name": "quality", "bucket": "fail"}],
    }
    failure_evidence = {**observation, "repairability": "code_failure"}
    job = {
        "pr_number": 17,
        "required_checks": "fail",
        "required_checks_mode": "configured",
        "required_checks_evidence": observation,
        "ci_evidence": failure_evidence,
        "fallback_publication_receipt": {
            "publication_sha": "failed-head",
            "required_checks_evidence": dict(observation),
        },
    }

    bind_new_publication_head(job, "successor-head")

    assert "required_checks" not in job
    assert "required_checks_mode" not in job
    assert "required_checks_evidence" not in job
    assert job["ci_evidence"] is failure_evidence
    receipt = job["fallback_publication_receipt"]
    assert receipt["publication_sha"] == "successor-head"
    assert receipt["pr_number"] == 17
    assert "required_checks_evidence" not in receipt


def test_clear_observation_clears_fallback_receipt_but_keeps_failure_provenance() -> None:
    observation = {
        "pr_number": 17,
        "head_sha": "failed-head",
        "result": "fail",
        "checks": [{"name": "quality", "bucket": "fail"}],
    }
    failure_evidence = {**observation, "repairability": "code_failure"}
    receipt = {
        "publication_sha": "failed-head",
        "required_checks_evidence": dict(observation),
        "required_check_failure_evidence": failure_evidence,
        "final_ci_fix_failure_head": "failed-head",
    }
    job = {
        "required_checks_evidence": observation,
        "ci_evidence": failure_evidence,
        "final_ci_fix_failure_head": "failed-head",
        "fallback_publication_receipt": receipt,
    }

    clear_required_checks_observation(job)

    assert "required_checks_evidence" not in job
    assert "required_checks_evidence" not in receipt
    assert job["ci_evidence"] is failure_evidence
    assert job["final_ci_fix_failure_head"] == "failed-head"
    assert receipt["required_check_failure_evidence"] is failure_evidence
    assert receipt["final_ci_fix_failure_head"] == "failed-head"


def test_matching_legacy_projection_is_accepted_but_not_required() -> None:
    observation = {
        "pr_number": 17,
        "head_sha": "candidate-head",
        "result": "pass",
        "checks": [{"name": "quality", "bucket": "pass"}],
    }
    owner = {
        "required_checks": "pass",
        "required_checks_mode": "configured",
        "required_checks_evidence": observation,
    }

    require_required_checks_observation(
        observation,
        location="job.required_checks_evidence",
        expected_pr_number=17,
        expected_head_sha="candidate-head",
    )
    validate_legacy_required_checks_projection(
        owner, location="job", observation=observation
    )


@pytest.mark.parametrize(
    "owner",
    [
        {
            "required_checks": "fail",
            "required_checks_mode": "configured",
            "required_checks_evidence": {
                "pr_number": 17,
                "head_sha": "candidate-head",
                "result": "pass",
                "checks": [],
            },
        },
        {
            "required_checks": "pass",
            "required_checks_mode": "configured",
        },
    ],
)
def test_legacy_projection_requires_a_matching_canonical_observation(
    owner: dict[str, object],
) -> None:
    observation = owner.get("required_checks_evidence")
    if not isinstance(observation, dict):
        observation = None

    with pytest.raises(IncompatibleRunStateError, match="incompatible_run_state"):
        validate_legacy_required_checks_projection(
            owner, location="job", observation=observation
        )
