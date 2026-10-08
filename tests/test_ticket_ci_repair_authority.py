from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.change_delivery_fallback import (
    prepare_ticket_ci_publication_authorization,
    previous_publication_authorization,
)
from agent_run.ci_recovery_budget import (
    mark_ci_repair,
    mark_ci_review_invocation,
    observe_repairable_ci_failure,
    reset_exhausted_ci_budget,
)
from agent_run.delivery_policy import resolve_delivery_policy
from agent_run.integration_record_contract import (
    require_completed_ticket_integration_records,
)
from agent_run.review_budget import new_budget
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.ticket_publication_contract import (
    require_active_ticket_publication_authorization,
)
from support.cli_payloads import passing_acceptance, repair_acceptance


def _reviewed_failed_job() -> dict[str, Any]:
    artifact = passing_acceptance("reviewer-1", "Original candidate passed")
    artifact.pop("thread_id")
    budget = new_budget(
        development_attempts=1,
        reviewer_invocations=1,
        review_artifacts=[{
            "reviewer_thread_id": "reviewer-1",
            "candidate_sha": "reviewed-candidate",
            "reviewed_base_sha": "base",
            "review_identity": {
                "reviewed_base_sha": "base",
                "reviewed_candidate_sha": "reviewed-candidate",
                "reviewed_candidate_tree": "reviewed-tree",
            },
            "artifact": artifact,
        }],
    )
    failed_check = {
        "workflow": "quality", "name": "tests", "link": "https://example.test/check/1",
        "bucket": "fail", "state": "FAILURE", "repairability": "code_failure",
    }
    failed = {
        "pr_number": 7, "head_sha": "failed-head", "result": "fail",
        "checks": [failed_check],
    }
    job: dict[str, Any] = {
        "phase": "waiting_checks",
        "base_sha": "base",
        "candidate_sha": "reviewed-candidate",
        "effective_revision": "revision",
        "policy_snapshot": None,
        "review_budget": budget,
        "review_budget_history": [],
        "publication_authority": "acceptance",
        "acceptance_record": {
            "acceptance_scope": "change_job",
            "reviewed_base_sha": "base",
            "reviewed_candidate_sha": "reviewed-candidate",
            "reviewed_candidate_tree": "reviewed-tree",
            "effective_revision": "revision",
            "reviewer_thread_id": "reviewer-1",
            "artifact": artifact,
        },
        "acceptance_artifact": artifact,
        "publication_sha": "failed-head",
        "pr_number": 7,
        "required_checks_evidence": failed,
    }
    previous = previous_publication_authorization(job)
    assert previous is not None
    job.update({
        "phase": "candidate",
        "candidate_sha": "repaired-candidate",
        "repair_source": "required_checks",
        "attempt_kind": "ci_repair",
        "development_summary": "Fixed the failing test path.",
        "ci_evidence": deepcopy(failed),
        "required_checks_origin": deepcopy(failed),
        "ci_recovery": {
            "window": 1, "developments_used": 1, "semantic_work_ids": [],
            "failure_head": "failed-head", "repair_pending": True,
            "review_required": False, "checkpoint_reason": None,
            "history": [],
            "previous_publication_authorization": previous,
            "reviewer_invocations": 0,
            "review_artifacts": [],
        },
    })
    job.pop("required_checks_evidence")
    return job


def _stage() -> SimpleNamespace:
    class Git:
        def verify_candidate_integrity(self, base: str, candidate: str) -> dict[str, str]:
            return {
                "status": "pass", "base_sha": base,
                "candidate_sha": candidate, "candidate_tree": "repaired-tree",
                "base_is_ancestor": "true",
            }

        def resolve(self, _revision: str) -> str:
            return "repaired-tree"

        def is_ancestor(self, ancestor: str, descendant: str) -> bool:
            return ancestor == "failed-head" and descendant == "repaired-candidate"

        def diff_name_status(self, _base: str, _candidate: str) -> list[dict[str, str]]:
            return [{"status": "M", "path": "tests/test_feature.py"}]

    return SimpleNamespace(git=Git(), save=lambda state: state)


def test_first_ticket_ci_repair_has_narrow_authority_without_review() -> None:
    job = _reviewed_failed_job()
    assert prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    assert job["phase"] == "accepted"
    assert job["publication_authority"] == "ci_repair"
    assert "acceptance_record" not in job
    assert "fallback_publication_receipt" not in job
    assert job["review_budget"]["reviewer_invocations"] == 1
    require_active_ticket_publication_authorization(
        job, candidate_tree="repaired-tree"
    )

    job["phase"] = "waiting_checks"
    job["publication_sha"] = "repaired-head"
    job["required_checks_evidence"] = {
        "pr_number": 7, "head_sha": "repaired-head", "result": "pass",
        "checks": [{"bucket": "pass"}],
    }
    receipt = job["ci_repair_publication_receipt"]
    receipt.update({
        "pr_number": 7,
        "publication_sha": "repaired-head",
        "required_checks_evidence": deepcopy(job["required_checks_evidence"]),
    })
    require_active_ticket_publication_authorization(job, candidate_tree="repaired-tree")

    for mutation in (
        lambda copy: copy["ci_repair_publication_receipt"].update(candidate_tree="other-tree"),
        lambda copy: copy["ci_repair_publication_receipt"].update(failure_head="other-head"),
        lambda copy: copy["ci_repair_publication_receipt"]["failure_evidence"]["checks"][0].update(name="wrong-check"),
        lambda copy: copy["ci_recovery"].update(review_required=True),
        lambda copy: copy["required_checks_evidence"].update(head_sha="old-head"),
    ):
        changed = deepcopy(job)
        mutation(changed)
        with pytest.raises(IncompatibleRunStateError):
            require_active_ticket_publication_authorization(
                changed, candidate_tree="repaired-tree"
            )


def test_first_ticket_ci_repair_rejects_later_round_and_missing_prior_head() -> None:
    job = _reviewed_failed_job()
    job["ci_recovery"]["developments_used"] = 2
    job["ci_recovery"]["review_required"] = True
    assert not prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    assert job["phase"] == "candidate"
    assert "ci_repair_publication_receipt" not in job

    job = _reviewed_failed_job()
    job["ci_recovery"]["previous_publication_authorization"]["publication_sha"] = "other-head"
    assert not prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    assert "ci_repair_publication_receipt" not in job


def test_completed_ticket_ci_repair_record_keeps_its_own_source() -> None:
    job = _reviewed_failed_job()
    assert prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    job["phase"] = "completed"
    job["publication_sha"] = "repaired-head"
    job["integrated_sha"] = "merge-head"
    job["required_checks_evidence"] = {
        "pr_number": 7, "head_sha": "repaired-head", "result": "pass",
        "checks": [{"bucket": "pass"}],
    }
    receipt = job["ci_repair_publication_receipt"]
    receipt.update({
        "publication_sha": "repaired-head", "pr_number": 7,
        "required_checks_evidence": deepcopy(job["required_checks_evidence"]),
    })
    ticket_integration = {
        "base_sha": "base", "candidate_sha": "repaired-candidate",
        "candidate_tree": "repaired-tree", "source_sha": "failed-head",
        "target_sha": "run-head", "head_sha": "integration-head",
        "head_tree": "integration-tree", "conflict_paths": [],
    }
    job["ticket_integration"] = ticket_integration
    record = {
        "source": "ci_repair", "policy_snapshot": None,
        "base_sha": "run-head", "candidate_sha": "repaired-candidate",
        "candidate_tree": "repaired-tree", "publication_sha": "repaired-head",
        "integrated_sha": "merge-head", "integrated_tree": "integration-tree",
        "integrated_message": "Merged Ticket", "integrated_parents": ["run-head"],
        "integrated_publication_sha": "repaired-head",
        "effective_revision": "revision", "pr_number": 7,
        "window": 1, "final_ci_fix_used": False,
        "required_checks_evidence": deepcopy(job["required_checks_evidence"]),
        "required_checks_origin": deepcopy(job["required_checks_origin"]),
        "pr": {
            "number": 7, "head_sha": "repaired-head", "base_sha": "run-head",
            "state": "MERGED", "merge_commit_sha": "merge-head",
        },
        "ticket_integration": deepcopy(ticket_integration),
        "ci_repair_publication_receipt": deepcopy(receipt),
    }
    job["deterministic_integration_record"] = record
    state = {"ticket_jobs": {"3": job}}
    require_completed_ticket_integration_records(state)

    record["ci_repair_publication_receipt"]["candidate_tree"] = "wrong-tree"
    with pytest.raises(IncompatibleRunStateError, match="ci_repair_publication_receipt"):
        require_completed_ticket_integration_records(state)


def test_later_ticket_ci_round_needs_its_real_reviewer_artifact() -> None:
    job = _reviewed_failed_job()
    artifact = passing_acceptance("ci-reviewer", "Repaired candidate passed")
    artifact.pop("thread_id")
    job["phase"] = "accepted"
    job["publication_authority"] = "accepted"
    job["candidate_sha"] = "reviewed-ci-candidate"
    job["acceptance_artifact"] = artifact
    job["acceptance_record"] = {
        "acceptance_scope": "change_job",
        "reviewed_base_sha": "base",
        "reviewed_candidate_sha": "reviewed-ci-candidate",
        "reviewed_candidate_tree": "reviewed-ci-tree",
        "effective_revision": "revision",
        "reviewer_thread_id": "ci-reviewer",
        "artifact": artifact,
    }
    recovery = job["ci_recovery"]
    recovery["developments_used"] = 2
    recovery["review_required"] = True
    recovery["reviewer_invocations"] = 1
    recovery["review_artifacts"] = [{
        "reviewer_thread_id": "ci-reviewer",
        "candidate_sha": "reviewed-ci-candidate",
        "reviewed_base_sha": "base",
        "review_identity": {
            "reviewed_base_sha": "base",
            "reviewed_candidate_sha": "reviewed-ci-candidate",
            "reviewed_candidate_tree": "reviewed-ci-tree",
        },
        "artifact": artifact,
    }]
    require_active_ticket_publication_authorization(
        job, candidate_tree="reviewed-ci-tree"
    )
    # Older full artifacts may be pruned after explicit CI budget renewals;
    # the latest reviewed candidate must still authorize this publication.
    recovery["reviewer_invocations"] = 5
    require_active_ticket_publication_authorization(
        job, candidate_tree="reviewed-ci-tree"
    )
    for mutation in (
        lambda copy: copy["ci_recovery"].update(review_required=False),
        lambda copy: copy["ci_recovery"]["review_artifacts"].clear(),
        lambda copy: copy["ci_recovery"]["review_artifacts"][-1].update(candidate_sha="old-ci-candidate"),
    ):
        forged = deepcopy(job)
        mutation(forged)
        with pytest.raises(IncompatibleRunStateError):
            require_active_ticket_publication_authorization(forged, candidate_tree="reviewed-ci-tree")


@pytest.mark.parametrize("used", [1, 2, 3])
def test_unreviewed_ticket_ci_policy_applies_to_every_development(used: int) -> None:
    job = _reviewed_failed_job()
    job["ci_recovery"]["developments_used"] = used
    job["ci_evidence"]["checks"][0]["repairability"] = "unknown"
    assert prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    require_active_ticket_publication_authorization(job, candidate_tree="repaired-tree")
    assert "acceptance_record" not in job


def test_next_failure_preserves_compact_prior_unreviewed_authority() -> None:
    job = _reviewed_failed_job()
    assert prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    failed = {
        "pr_number": 7, "head_sha": "failed-head", "result": "fail",
        "checks": deepcopy(job["ci_evidence"]["checks"]),
    }
    receipt = job["ci_repair_publication_receipt"]
    receipt.update(publication_sha="failed-head", pr_number=7, required_checks_evidence=failed)
    prior = previous_publication_authorization(job)
    assert prior is not None and prior["authority"] == "ci_repair"
    assert "previous_publication_authorization" not in prior["fallback_receipt"]
    job["ci_recovery"].update(developments_used=2, previous_publication_authorization=prior)
    prior["candidate_sha"] = "previous-candidate"
    prior["candidate_tree"] = "previous-tree"
    prior["fallback_receipt"].update(candidate_sha="previous-candidate", candidate_tree="previous-tree")
    prior["fallback_receipt"]["git_integrity"].update(candidate_sha="previous-candidate", candidate_tree="previous-tree")
    assert prepare_ticket_ci_publication_authorization(_stage(), {}, job)
    require_active_ticket_publication_authorization(job, candidate_tree="repaired-tree")
    assert "acceptance_record" not in job
