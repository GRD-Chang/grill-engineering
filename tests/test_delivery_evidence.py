"""Publication-currentness reset contracts at both consumer boundaries."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.delivery_loop import TicketDeliveryLoop
from agent_run.parent_delivery_loop import ParentDeliveryLoop


class ResetGit:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def resolve(self, branch: str) -> str:
        self.calls.append(("resolve", branch))
        return "new-base"

    def reset_checkout_to_base(self, checkout: Path, branch: str) -> None:
        assert checkout == Path("checkout")
        self.calls.append(("reset", branch))
        if self.fail:
            raise OSError("reset failed")


def stale_job() -> dict[str, Any]:
    return {
        "candidate_sha": "old-candidate",
        "publication": {"pr_title": "old"},
        "publication_sha": "old-publication",
        "publication_commit_intent": {"expected_sha": "old-publication"},
        "acceptance_artifact": {"e2e": "old"},
        "acceptance_record": {"reviewed_base_sha": "old-base"},
        "publication_attempts": 2,
        "publication_thread_id": "old-thread",
        "publication_operation_retry": {"attempt": 2},
        "last_publication_error": "old-error",
        "repair_source": "required_checks",
        "ci_evidence": {"head_sha": "old-publication"},
        "required_checks_origin": {"head_sha": "old-publication"},
        "publication_authority": "fallback",
        "fallback_publication_receipt": {"required_checks_evidence": {}},
        "deterministic_integration_record": {"head_sha": "old-publication"},
        "next_attempt_kind": "final_ci_fix",
        "last_review_candidate_sha": "old-candidate",
        "final_ci_fix_failure_head": "old-publication",
        "required_checks_evidence": {},
        "required_checks_observation_status": "observed",
        "required_checks_observed_at": "old-time",
        "required_checks": [],
        "required_checks_mode": "configured",
    }


@pytest.mark.parametrize("kind", ["ticket", "parent"])
def test_publication_reset_removes_evidence_and_retains_job_authority(kind: str) -> None:
    git = ResetGit()
    loop_type = TicketDeliveryLoop if kind == "ticket" else ParentDeliveryLoop
    loop = loop_type(git=git, states=None, github=None, agents=None)  # type: ignore[arg-type]
    state = {
        "run_branch": "run", "base": {"branch": "main", "sha": "fixed-base"},
        "diagnostics": ["old"],
    }
    preserved = {
        "pr_number": 12,
        "published_sha": "old-remote-head",
        "development_thread_id": "development-thread",
        "reviewer_thread_ids": ["reviewer-thread"],
        "modification_attempts": 4,
        "review_budget": {"development_attempts": 4, "reviewer_invocations": 3},
        "review_budget_history": [{"window": 1}],
        "effective_revision": "revision",
    }
    job = {**stale_job(), **deepcopy(preserved), "base_sha": "old-base", "validation_attempts": 3}
    if kind == "parent":
        job["approval_grant"] = {"head_sha": "old-publication"}

    loop.publisher.invalidate_stale(state, job, Path("checkout"))

    expected_base = "new-base" if kind == "ticket" else "fixed-base"
    assert job == {
        **preserved, "base_sha": expected_base, "phase": "developing",
        "validation_attempts": 0,
    }
    assert state["diagnostics"] == []
    assert state["status"] == ("active" if kind == "ticket" else "parent_delivery_pending")
    assert git.calls == (
        [("reset", "run"), ("resolve", "run")]
        if kind == "ticket" else [("reset", "fixed-base")]
    )


@pytest.mark.parametrize("kind", ["ticket", "parent"])
def test_reset_failure_does_not_discard_evidence_or_parent_approval(kind: str) -> None:
    git = ResetGit(fail=True)
    loop_type = TicketDeliveryLoop if kind == "ticket" else ParentDeliveryLoop
    loop = loop_type(git=git, states=None, github=None, agents=None)  # type: ignore[arg-type]
    state = {
        "run_branch": "run", "base": {"branch": "main", "sha": "fixed-base"},
        "status": "publishing",
    }
    job = {**stale_job(), "approval_grant": {"head_sha": "old-publication"}}
    before = deepcopy(job)
    with pytest.raises(OSError, match="reset failed"):
        loop.publisher.invalidate_stale(state, job, Path("checkout"))
    assert job == before
    assert state["status"] == "publishing"
    assert git.calls == [("reset", "run" if kind == "ticket" else "fixed-base")]
