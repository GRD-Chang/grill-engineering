from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.controller import Controller, _resume_review_budget_window
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.git import MergeConflictError
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import run_currentness_boundary
from agent_run.run_currentness import (
    ticket_completion_records,
    ticket_integration_records,
)
from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_active_ticket_publication_authorization,
    require_completed_ticket_integration_records,
)

from conftest import seed_idle_control
from support.inprocess_cli import invoke_cli_inprocess
from support.workspace import prepare_workspace
from test_cli import run_internal_stage, run_cli, stdout_json
from test_cli_delivery import final_run_publication

from run_acceptance_test_support import (
    FreshCycleRunAgents,
    ScriptedRunAgents,
    _canonical_run_budget,
    _BLOCKED_EVIDENCE,
    _candidate_finding_artifact,
    _completed_run,
    _failed_invocation,
    _human_artifact,
    _passing_artifact,
    _repair_artifact,
)


def _integration_record(
    *,
    source: str = "accepted",
    pr_number: int = 21,
    base_sha: str = "base-sha",
    candidate_sha: str = "candidate-sha",
    candidate_tree: str = "candidate-tree",
    publication_sha: str = "published-sha",
    integrated_sha: str | None = None,
    acceptance_record: dict[str, Any] | None = None,
    fallback_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    integrated = integrated_sha or candidate_sha
    record: dict[str, Any] = {
        "source": source,
        "pr_number": pr_number,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "publication_sha": publication_sha,
        "integrated_sha": integrated,
        "integrated_publication_sha": publication_sha,
        "integrated_tree": candidate_tree,
        "integrated_message": "fix: integrated ticket",
        "integrated_parents": [base_sha],
        "effective_revision": "effective-revision",
        "window": 1,
        "final_ci_fix_used": False,
        "required_checks_mode": (
            "configured" if source == "accepted" else "not_configured"
        ),
        "required_checks": "pass" if source == "accepted" else "none",
        "required_checks_evidence": {
            "pr_number": pr_number,
            "head_sha": publication_sha,
            "result": "pass" if source == "accepted" else "none",
            "checks": (
                [{"name": "test", "bucket": "pass"}]
                if source == "accepted"
                else []
            ),
        },
        "pr": {
            "number": pr_number,
            "state": "MERGED",
            "head_sha": publication_sha,
            "base_sha": base_sha,
            "merge_commit_sha": integrated,
        },
    }
    if source == "accepted":
        authorization = dict(acceptance_record or {})
        authorization.setdefault("acceptance_scope", "change_job")
        authorization.setdefault("reviewed_base_sha", base_sha)
        authorization.setdefault("reviewed_candidate_sha", candidate_sha)
        authorization.setdefault("reviewed_candidate_tree", candidate_tree)
        authorization.setdefault("effective_revision", record["effective_revision"])
        authorization.setdefault("reviewer_thread_id", "ticket-reviewer")
        authorization.setdefault("artifact", _passing_artifact())
        record["acceptance_record"] = authorization
        review_artifact = {
            "reviewer_thread_id": authorization["reviewer_thread_id"],
            "candidate_sha": authorization["reviewed_candidate_sha"],
            "reviewed_base_sha": authorization["reviewed_base_sha"],
            "review_identity": {
                "reviewed_base_sha": authorization["reviewed_base_sha"],
                "reviewed_candidate_sha": authorization["reviewed_candidate_sha"],
                "reviewed_candidate_tree": authorization["reviewed_candidate_tree"],
            },
            "artifact": authorization["artifact"],
        }
        record["review_budget"] = {
            "window": 1,
            "development_attempts": 1,
            "reviewer_invocations": 1,
            "final_ci_fix_used": False,
            "review_artifacts": [review_artifact],
            "checkpoint_reason": None,
        }
    else:
        authorization = dict(fallback_receipt or {})
        authorization.setdefault("kind", "ticket_fallback_publication_receipt")
        authorization.setdefault("base_sha", base_sha)
        authorization.setdefault("candidate_sha", candidate_sha)
        authorization.setdefault("candidate_tree", candidate_tree)
        authorization.setdefault("publication_sha", publication_sha)
        authorization.setdefault("effective_revision", record["effective_revision"])
        authorization.setdefault("pr_number", pr_number)
        authorization.setdefault(
            "required_checks_evidence", record["required_checks_evidence"]
        )
        review_artifacts = [
            {
                "reviewer_thread_id": f"ticket-reviewer-{index}",
                "candidate_sha": f"reviewed-candidate-{index}",
                "reviewed_base_sha": base_sha,
                "review_identity": {
                    "reviewed_base_sha": base_sha,
                    "reviewed_candidate_sha": f"reviewed-candidate-{index}",
                    "reviewed_candidate_tree": f"reviewed-tree-{index}",
                },
                "artifact": _candidate_finding_artifact(),
            }
            for index in range(1, 4)
        ]
        last_artifact = review_artifacts[-1]["artifact"]
        authorization.setdefault("window", 1)
        authorization.setdefault("reviewer_invocations", 3)
        authorization.setdefault("review_artifacts", review_artifacts)
        authorization.setdefault(
            "last_review_candidate_sha", "reviewed-candidate-3"
        )
        authorization.setdefault("validation_attempts", 3)
        authorization.setdefault("development_attempts", 4)
        authorization.setdefault(
            "review_budget",
            {
                "window": 1,
                "development_attempts": 4,
                "reviewer_invocations": 3,
                "final_ci_fix_used": False,
                "review_artifacts": review_artifacts,
                "checkpoint_reason": None,
            },
        )
        authorization.setdefault("final_ci_fix_used", False)
        authorization.setdefault("final_ci_fix_failure_head", None)
        authorization.setdefault("last_attempt_kind", "ordinary")
        authorization.setdefault("repair_source", "acceptance")
        authorization.setdefault("failure_evidence_source", "acceptance")
        authorization.setdefault("failure_evidence", last_artifact)
        authorization.setdefault("git_integrity_evidence", None)
        authorization.setdefault(
            "development_summary", "Repaired the ticket candidate."
        )
        authorization.setdefault("code_delta_base_sha", base_sha)
        authorization.setdefault(
            "code_delta", [{"status": "M", "path": "ticket.py"}]
        )
        authorization.setdefault("repair_delta_base_sha", "reviewed-candidate-3")
        authorization.setdefault(
            "repair_delta", [{"status": "M", "path": "ticket.py"}]
        )
        authorization.setdefault("required_check_failure_head", None)
        authorization.setdefault("previous_publication_authority", None)
        authorization.setdefault(
            "git_integrity",
            {
                "status": "pass",
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
                "candidate_tree": candidate_tree,
                "base_is_ancestor": "true",
            },
        )
        authorization.setdefault("last_acceptance_artifact", last_artifact)
        record["fallback_receipt"] = authorization
    return record


def test_run_acceptance_invocation_binds_the_reviewed_run_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    integration_record = _integration_record(
        publication_sha="published-2",
        integrated_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
    )
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = (
        integration_record
    )
    state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
        integration_record["review_budget"]
    )
    states.save_run(str(state["run_id"]), state)

    class InvocationReviewer:
        def __init__(self) -> None:
            self.request: dict[str, Any] | None = None

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.request = request
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="run-reviewer", attempt_count=1)
            return ReviewResult("run-reviewer", _passing_artifact())

    agents = InvocationReviewer()
    completed = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    assert agents.request is not None
    invocation = completed["active_agent_invocation"]
    acceptance = completed["run_acceptance"]["acceptance_record"]
    assert invocation["status"] == "completed"
    assert invocation["role"] == "reviewer"
    assert invocation["phase"] == "run_acceptance"
    assert invocation["work_subject"] == f"run-acceptance:{state['run_id']}"
    assert invocation["generation"] == 1
    assert invocation["input_fingerprint"] == canonical_fingerprint(agents.request)
    assert agents.request["ticket_integration_records"] == [
        {
            "ticket_number": 2,
            "integrated_sha": state["ticket_jobs"]["2"]["integrated_sha"],
            "effective_revision": state["ticket_jobs"]["2"]["effective_revision"],
            "integration_record": integration_record,
        }
    ]
    assert invocation["currentness_boundary"] == {
        "reviewed_head_sha": acceptance["reviewed_head_sha"],
        "reviewed_default_base_sha": acceptance["reviewed_default_base_sha"],
        "expected_merge_tree": acceptance["expected_merge_tree"],
        "parent_revision": acceptance["parent_revision"],
        "ticket_graph_revision": acceptance["ticket_graph_revision"],
        "ticket_completion_records_fingerprint": canonical_fingerprint(
            acceptance["ticket_completion_records"]
        ),
    }


def test_run_repair_review_budget_stops_at_r_n_plus_one_without_development_n_plus_one(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    policy = DeliveryPolicy(run_repair_rounds=1)
    state["policy_snapshot"] = policy.snapshot()
    states.save_run(str(state["run_id"]), state)

    agents = ScriptedRunAgents()
    agents._reviews = [_repair_artifact(), _candidate_finding_artifact()]
    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    run = result["run_acceptance"]
    job = run["repair_job"]
    assert result["status"] == "ready_for_human"
    assert run["blocked_reason"] == "review_budget_exhausted"
    assert len(agents.development_requests) == 1
    assert len(agents.review_requests) == 2
    assert run["review_budget"]["development_attempts"] == 1
    assert run["review_budget"]["reviewer_invocations"] == 2
    assert job["review_budget"] == run["review_budget"]
    assert job["phase"] == "blocked"


def test_merge_preflight_conflict_does_not_consume_a_reviewer_ordinal(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, states, git = _completed_run(git_repo)
    run = {
        "phase": "pending",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 2,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["run_acceptance"] = run
    states.save_run(str(state["run_id"]), state)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    prepare_expected_merge = git.prepare_expected_merge_checkout

    def conflict(**_kwargs: object) -> None:
        raise MergeConflictError("merge preview conflicts")

    monkeypatch.setattr(git, "prepare_expected_merge_checkout", conflict)
    assert engine._review(state, run)

    assert run["phase"] == "repairing"
    assert run["validation_attempts"] == 2
    assert run["review_budget"]["reviewer_invocations"] == 0
    assert "pending_semantic_attempt" not in run
    assert "semantic_attempt_history" not in run
    assert state.get("active_agent_invocation") is None

    monkeypatch.setattr(
        git, "prepare_expected_merge_checkout", prepare_expected_merge
    )
    run["phase"] = "pending"
    run.pop("repair_request")
    state.update(
        {
            "status": "run_acceptance_pending",
            "terminal_kind": "all_tickets_completed",
            "diagnostics": [],
        }
    )
    states.save_run(str(state["run_id"]), state)

    assert engine._review(state, run)
    assert run["validation_attempts"] == 3
    assert run["review_budget"]["reviewer_invocations"] == 1
    assert [attempt["ordinal"] for attempt in run["semantic_attempt_history"]] == [3]


def test_run_acceptance_rejects_completed_ticket_without_integration_record(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    state["ticket_jobs"]["2"].pop("deterministic_integration_record")
    states.save_run(str(state["run_id"]), state)
    state_before = deepcopy(states.load_run(str(state["run_id"])))

    with pytest.raises(
        IncompatibleRunStateError,
        match=r"completed Ticket 2 is missing .*deterministic_integration_record",
    ):
        RunAcceptanceEngine(
            git=_git,
            states=states,
            agents=object(),
        ).accept(str(state["run_id"]))

    assert states.load_run(str(state["run_id"])) == state_before


def test_run_acceptance_cli_reports_incompatible_missing_integration_record(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, _git = _completed_run(repo, state_root=workspace.state_root)
    state["ticket_jobs"]["2"].pop("deterministic_integration_record")
    states.save_run(str(state["run_id"]), state)
    state_before = deepcopy(states.load_run(str(state["run_id"])))

    result = run_cli(
        repo,
        repo / "github.json",
        "resume",
        str(state["run_id"]),
    )

    output = stdout_json(result)
    assert result.returncode == 2
    assert output["status"] == "incompatible_run_state"
    assert output["diagnostics"][0]["code"] == "incompatible_run_state"
    assert "不会迁移" in output["diagnostics"][0]["message"]
    assert states.load_run(str(state["run_id"])) == state_before


@pytest.mark.parametrize(
    ("result", "checks"),
    [
        ("pass", []),
        ("pass", [{"name": "quality", "bucket": "fail"}]),
        ("none", [{"name": "quality", "bucket": "pass"}]),
        ("pending", [{"name": "quality", "bucket": "pass"}]),
        ("fail", [{"name": "quality", "bucket": "pending"}]),
        ("unknown", []),
    ],
)
def test_integration_record_rejects_required_checks_result_bucket_contradictions(
    git_repo: Path, result: str, checks: list[dict[str, str]]
) -> None:
    state, _states, _git = _completed_run(git_repo)
    record = state["ticket_jobs"]["2"]["deterministic_integration_record"]
    assert isinstance(record, dict)
    evidence = record["required_checks_evidence"]
    assert isinstance(evidence, dict)
    evidence.update({"result": result, "checks": checks})

    with pytest.raises(
        IncompatibleRunStateError, match="required_checks_evidence"
    ):
        require_completed_ticket_integration_records(state)


@pytest.mark.parametrize("source", ["accepted", "fallback"])
def test_completed_ticket_integration_record_rejects_source_evidence_drift(
    git_repo: Path, source: str
) -> None:
    state, states, git = _completed_run(git_repo)
    record = _integration_record(
        source=source,
        candidate_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
        candidate_tree=git.resolve(
            f'{state["ticket_jobs"]["2"]["integrated_sha"]}^{{tree}}'
        ),
        publication_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
        integrated_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
        acceptance_record={"artifact": _passing_artifact()},
        fallback_receipt={"candidate_sha": "fallback-candidate"},
    )
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    if source == "accepted":
        state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
            record["review_budget"]
        )
    if source == "accepted":
        record["pr"]["head_sha"] = "different-head"
    else:
        record["required_checks_evidence"]["result"] = "pass"
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(
        IncompatibleRunStateError, match="deterministic_integration_record"
    ):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=object(),
        ).accept(str(state["run_id"]))


@pytest.mark.parametrize("source", ["accepted", "fallback"])
def test_completed_ticket_integration_record_rejects_empty_nested_authorization(
    git_repo: Path, source: str
) -> None:
    state, states, git = _completed_run(git_repo)
    record = _integration_record(
        source=source,
        candidate_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
        candidate_tree=git.resolve(
            f'{state["ticket_jobs"]["2"]["integrated_sha"]}^{{tree}}'
        ),
        publication_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
        integrated_sha=str(state["ticket_jobs"]["2"]["integrated_sha"]),
    )
    record["acceptance_record" if source == "accepted" else "fallback_receipt"] = {}
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    if source == "accepted":
        state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
            record["review_budget"]
        )
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(
        IncompatibleRunStateError,
        match=r"acceptance_record|fallback_receipt",
    ):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


def test_completed_ticket_integration_record_rejects_nested_boundary_drift(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    integrated = str(state["ticket_jobs"]["2"]["integrated_sha"])
    record = _integration_record(
        candidate_sha=integrated,
        candidate_tree=git.resolve(f"{integrated}^{{tree}}"),
        publication_sha=integrated,
        integrated_sha=integrated,
    )
    record["acceptance_record"]["reviewed_candidate_sha"] = "foreign-candidate"
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    state["ticket_jobs"]["2"]["review_budget"] = deepcopy(record["review_budget"])
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(
        IncompatibleRunStateError, match="acceptance_record boundary"
    ):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


def test_completed_ticket_acceptance_authorization_requires_a_passing_artifact(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    integrated = str(state["ticket_jobs"]["2"]["integrated_sha"])
    record = _integration_record(
        candidate_sha=integrated,
        candidate_tree=git.resolve(f"{integrated}^{{tree}}"),
        publication_sha=integrated,
        integrated_sha=integrated,
        acceptance_record={"artifact": _candidate_finding_artifact()},
    )
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    state["ticket_jobs"]["2"]["review_budget"] = deepcopy(record["review_budget"])
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="artifact must be pass"):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


@pytest.mark.parametrize(
    "mutation", ["zero_invocations", "count_mismatch", "record_budget"]
)
def test_completed_accepted_ticket_binds_review_budget_authorization(
    git_repo: Path, mutation: str
) -> None:
    state, states, git = _completed_run(git_repo)
    job = state["ticket_jobs"]["2"]
    record = job["deterministic_integration_record"]
    if mutation == "zero_invocations":
        job["review_budget"]["reviewer_invocations"] = 0
        job["review_budget"]["review_artifacts"] = []
        record["review_budget"] = deepcopy(job["review_budget"])
        expected = "no Reviewer invocation"
    elif mutation == "count_mismatch":
        job["review_budget"]["reviewer_invocations"] = 2
        record["review_budget"] = deepcopy(job["review_budget"])
        expected = "invocation and Artifact counts differ"
    else:
        record["review_budget"] = {}
        expected = "review budget is not bound"
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match=expected):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


def test_active_ticket_publication_rejects_a_structurally_valid_fail_artifact() -> None:
    record = _integration_record()
    acceptance = record["acceptance_record"]
    job: dict[str, Any] = {
        "phase": "accepted",
        "base_sha": record["base_sha"],
        "candidate_sha": record["candidate_sha"],
        "effective_revision": record["effective_revision"],
        "acceptance_record": acceptance,
        "acceptance_artifact": acceptance["artifact"],
    }
    acceptance["artifact"] = _candidate_finding_artifact()
    job["acceptance_artifact"] = acceptance["artifact"]

    with pytest.raises(IncompatibleRunStateError, match="artifact must be pass"):
        require_active_ticket_publication_authorization(
            job, candidate_tree=record["candidate_tree"]
        )


def test_active_ticket_accepted_publication_binds_budget_and_review_artifact() -> None:
    record = _integration_record()
    acceptance = record["acceptance_record"]
    review_artifact = {
        "reviewer_thread_id": acceptance["reviewer_thread_id"],
        "candidate_sha": acceptance["reviewed_candidate_sha"],
        "reviewed_base_sha": acceptance["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": acceptance["reviewed_base_sha"],
            "reviewed_candidate_sha": acceptance["reviewed_candidate_sha"],
            "reviewed_candidate_tree": acceptance["reviewed_candidate_tree"],
        },
        "artifact": acceptance["artifact"],
    }
    job: dict[str, Any] = {
        "phase": "accepted",
        "base_sha": record["base_sha"],
        "candidate_sha": record["candidate_sha"],
        "effective_revision": record["effective_revision"],
        "acceptance_record": acceptance,
        "acceptance_artifact": acceptance["artifact"],
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 1,
            "final_ci_fix_used": False,
            "review_artifacts": [review_artifact],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }

    require_active_ticket_publication_authorization(
        job, candidate_tree=record["candidate_tree"]
    )

    job["review_budget"]["reviewer_invocations"] = 0
    job["review_budget"]["review_artifacts"] = []
    with pytest.raises(IncompatibleRunStateError, match="no Reviewer invocation"):
        require_active_ticket_publication_authorization(
            job, candidate_tree=record["candidate_tree"]
        )

    job["review_budget"]["reviewer_invocations"] = 1
    job["review_budget"]["review_artifacts"] = [review_artifact]
    job["review_budget"]["review_artifacts"][0]["candidate_sha"] = "foreign"
    with pytest.raises(IncompatibleRunStateError, match="matching Review Artifact"):
        require_active_ticket_publication_authorization(
            job, candidate_tree=record["candidate_tree"]
        )


def test_active_ticket_fallback_rejects_missing_audit_before_publication() -> None:
    record = _integration_record(source="fallback")
    receipt = record["fallback_receipt"]
    job: dict[str, Any] = {
        "phase": "publication_pending",
        "base_sha": record["base_sha"],
        "candidate_sha": record["candidate_sha"],
        "effective_revision": record["effective_revision"],
        "review_budget": receipt["review_budget"],
        "publication_authority": "fallback",
        "fallback_publication_receipt": receipt,
    }
    for key in ("publication_sha", "pr_number", "required_checks_evidence"):
        receipt.pop(key)
    receipt.pop("development_summary")

    with pytest.raises(IncompatibleRunStateError, match="fallback_receipt"):
        require_active_ticket_publication_authorization(
            job, candidate_tree=record["candidate_tree"]
        )


def test_active_ticket_fallback_requires_complete_receipt_before_merge() -> None:
    record = _integration_record(source="fallback")
    receipt = record["fallback_receipt"]
    job: dict[str, Any] = {
        "phase": "merging",
        "base_sha": record["base_sha"],
        "candidate_sha": record["candidate_sha"],
        "effective_revision": record["effective_revision"],
        "publication_sha": record["publication_sha"],
        "pr_number": record["pr_number"],
        "required_checks": "none",
        "required_checks_evidence": record["required_checks_evidence"],
        "review_budget": receipt["review_budget"],
        "publication_authority": "fallback",
        "fallback_publication_receipt": receipt,
    }
    receipt.pop("git_integrity")

    with pytest.raises(IncompatibleRunStateError, match="fallback_receipt"):
        require_active_ticket_publication_authorization(
            job, candidate_tree=record["candidate_tree"]
        )


def test_required_checks_fallback_must_bind_previous_publication_authority() -> None:
    record = _integration_record(source="fallback")
    receipt = record["fallback_receipt"]
    failure = {
        "pr_number": record["pr_number"],
        "head_sha": record["publication_sha"],
        "checks": [
            {
                "name": "tests",
                "workflow": "tests",
                "link": "https://github.com/example/project/actions/runs/1",
                "bucket": "fail",
                "state": "FAILURE",
                "repairability": "code_failure",
            }
        ],
        "result": "fail",
    }
    receipt.update(
        {
            "repair_source": "required_checks",
            "failure_evidence_source": "required_checks",
            "failure_evidence": failure,
            "required_check_failure_evidence": failure,
            "required_check_failure_head": record["publication_sha"],
            "repair_delta_base_sha": record["publication_sha"],
            "previous_publication_authority": "acceptance",
            "previous_publication_authorization": {
                "authority": "acceptance",
                "pr_number": record["pr_number"],
                "publication_sha": record["publication_sha"],
                "base_sha": record["base_sha"],
                "effective_revision": record["effective_revision"],
                "candidate_sha": "previous-candidate",
                "candidate_tree": "previous-tree",
                "acceptance_record": {
                    "acceptance_scope": "change_job",
                    "reviewed_base_sha": record["base_sha"],
                    "reviewed_candidate_sha": "previous-candidate",
                    "reviewed_candidate_tree": "previous-tree",
                    "effective_revision": record["effective_revision"],
                    "reviewer_thread_id": "previous-reviewer",
                    "artifact": _passing_artifact(),
                },
                "fallback_receipt": None,
            },
        }
    )
    state = {
        "ticket_jobs": {
            "2": {
                "phase": "completed",
                "integrated_sha": record["integrated_sha"],
                "deterministic_integration_record": record,
            }
        }
    }

    require_completed_ticket_integration_records(state)
    with pytest.raises(IncompatibleRunStateError, match="previous publication"):
        receipt["previous_publication_authority"] = None
        require_completed_ticket_integration_records(state)

    receipt["previous_publication_authority"] = "fallback"
    with pytest.raises(IncompatibleRunStateError, match="previous_publication_authorization"):
        require_completed_ticket_integration_records(state)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer_invocations", 2),
        ("review_artifacts", []),
        ("repair_source", "required_checks"),
        ("failure_evidence", {}),
        ("development_attempts", 3),
        ("git_integrity", {"status": "fail"}),
    ],
)
def test_completed_ticket_fallback_receipt_requires_complete_audit_facts(
    git_repo: Path, field: str, value: object
) -> None:
    state, states, git = _completed_run(git_repo)
    integrated = str(state["ticket_jobs"]["2"]["integrated_sha"])
    record = _integration_record(
        source="fallback",
        candidate_sha=integrated,
        candidate_tree=git.resolve(f"{integrated}^{{tree}}"),
        publication_sha=integrated,
        integrated_sha=integrated,
    )
    record["fallback_receipt"][field] = value
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="fallback_receipt"):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


@pytest.mark.parametrize("mutated", ["record", "job"])
def test_completed_ticket_integration_record_binds_the_real_integrated_sha(
    git_repo: Path, mutated: str
) -> None:
    state, states, git = _completed_run(git_repo)
    if mutated == "record":
        state["ticket_jobs"]["2"]["deterministic_integration_record"][
            "integrated_sha"
        ] = "foreign-integrated-sha"
    else:
        state["ticket_jobs"]["2"]["integrated_sha"] = "foreign-integrated-sha"
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="integrated_sha"):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ("accepted", "acceptance_scope"),
        ("fallback", "kind"),
    ],
)
def test_completed_ticket_integration_record_rejects_nested_role_drift(
    git_repo: Path, source: str, field: str
) -> None:
    state, states, git = _completed_run(git_repo)
    integrated = str(state["ticket_jobs"]["2"]["integrated_sha"])
    record = _integration_record(
        source=source,
        candidate_sha=integrated,
        candidate_tree=git.resolve(f"{integrated}^{{tree}}"),
        publication_sha=integrated,
        integrated_sha=integrated,
    )
    record["acceptance_record" if source == "accepted" else "fallback_receipt"][
        field
    ] = "foreign-role"
    state["ticket_jobs"]["2"]["deterministic_integration_record"] = record
    if source == "accepted":
        state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
            record["review_budget"]
        )
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="scope|kind"):
        RunAcceptanceEngine(git=git, states=states, agents=object()).accept(
            str(state["run_id"])
        )


def test_run_repair_resume_reuses_the_repair_development_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    run_head = git.resolve(str(state["run_branch"]))
    candidate = subprocess.run(
        [
            "git",
            "commit-tree",
            git.resolve(f"{run_head}^{{tree}}"),
            "-p",
            run_head,
            "-m",
            "fix(run): preserve the reviewed repair candidate",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    thread_id = "run-repair-development-thread"
    thread_history = ["run-repair-development-thread-old-turn"]
    exhausted_budget = _canonical_run_budget()
    exhausted_budget["reviewer_invocations"] = 11
    state["run_acceptance"] = {
        "phase": "ready_for_human",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_generation": 1,
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 0,
        "reviewer_thread_ids": [],
        "development_thread_id": None,
        "development_thread_history": [],
        "review_budget": deepcopy(exhausted_budget),
        "review_budget_history": [],
        "repair_job": {
            "phase": "blocked",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "blocked_reason": "review_budget_exhausted",
            "repair_mode": "squash",
            "repair_source": "acceptance",
            "acceptance_artifact": _repair_artifact(),
            "candidate_sha": candidate,
            "development_thread_id": thread_id,
            "development_thread_history": thread_history,
            "review_budget": exhausted_budget,
            "review_budget_history": [],
        },
    }
    state["status"] = "ready_for_human"
    states.save_run(str(state["run_id"]), state)

    assert _resume_review_budget_window(state)
    run = state["run_acceptance"]
    assert run["phase"] == "repairing"
    assert "repair_job" not in run

    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=object(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    job = engine._repair_lifecycle._repair_job(state, run)
    assert job["phase"] == "developing"
    assert job["development_thread_id"] == thread_id
    assert job["development_thread_history"] == thread_history
    assert job["repair_seed_candidate_sha"] == candidate
    assert job["repair_mode"] == "squash"

    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "run-repair-seed"
    git.prepare_ticket_checkout(
        branch=str(job["repair_branch"]),
        base_sha=str(job["base_sha"]),
        checkout=checkout,
    )
    git.seed_managed_checkout(checkout, expected_head=candidate)
    assert git.checkout_head(checkout) == candidate
    git.remove_worktree(checkout)

    assert engine.repair_requests is not None
    request = engine.repair_requests.development(state, job, git_repo)
    assert request["thread_id"] == thread_id


@pytest.mark.parametrize("new_thread", [False, True])
def test_run_acceptance_execution_failure_resumes_selected_thread(
    git_repo: Path, new_thread: bool
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
    )
    state["run_acceptance"]["pending_semantic_attempt"] = state[
        "active_agent_invocation"
    ]["semantic_attempt"]
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=new_thread)

    run = resumed["run_acceptance"]
    assert resumed["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"
    if new_thread:
        assert run["reviewer_new_thread"] is True
        assert "reviewer_resume_thread_id" not in run
    else:
        assert run["reviewer_resume_thread_id"] == "failed-run-reviewer"

def test_second_reviewer_attempt_keeps_the_run_acceptance_generation(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 2,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="second-run-reviewer",
        ordinal=2,
    )
    state["run_acceptance"]["pending_semantic_attempt"] = state[
        "active_agent_invocation"
    ]["semantic_attempt"]
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["run_acceptance"]["validation_attempts"] == 2
    assert resumed["active_agent_invocation"]["generation"] == 1
    assert resumed["active_agent_invocation"]["status"] == "resuming"

@pytest.mark.parametrize(
    ("role", "work_subject"),
    [
        ("reviewer", "run-acceptance:{run_id}"),
        ("final_publication", "run-publication:{run_id}"),
    ],
)
def test_resume_rejects_stale_run_invocation_before_agent_start(
    git_repo: Path, role: str, work_subject: str
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    if role == "final_publication":
        state["run_acceptance"]["phase"] = "accepted"
        state["run_publication"] = {"phase": "publishing"}
    state["active_agent_invocation"] = _failed_invocation(
        role=role,
        phase="run_acceptance" if role == "reviewer" else "run_publication",
        work_subject=work_subject.format(run_id=state["run_id"]),
        generation=1,
        requested_thread_id="failed-thread",
        reported_thread_id="failed-thread",
        currentness_boundary={"reviewed_head_sha": "stale-head"},
    )
    attempt_owner = (
        state["run_publication"]
        if role == "final_publication"
        else state["run_acceptance"]
    )
    attempt_owner["pending_semantic_attempt"] = state["active_agent_invocation"][
        "semantic_attempt"
    ]
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["terminal_kind"] == "run_acceptance_stale"
    assert resumed["run_acceptance"]["phase"] == "pending"
    assert "reviewer_resume_thread_id" not in resumed["run_acceptance"]

def test_ticket_completion_records_are_minimal_and_sorted_numerically(
    git_repo: Path,
) -> None:
    state, _states, git = _completed_run(git_repo)
    state["ticket_graph"]["tickets"]["10"] = {
        "number": 10,
        "title": "Ticket 10",
        "body": "Deliver ticket 10.",
        "content_revision": "ticket-10-revision",
    }
    state["ticket_jobs"]["10"] = {
        "ticket_number": 10,
        "phase": "completed",
        "integrated_sha": "integrated-10",
        "effective_revision": "effective-10",
        "acceptance_record": {
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
            "artifact": _passing_artifact(),
        },
        "deterministic_integration_record": _integration_record(
            pr_number=31,
            candidate_sha="candidate-10",
            candidate_tree="tree-10",
            publication_sha="published-10",
            integrated_sha="integrated-10",
            acceptance_record={"artifact": _passing_artifact()},
        ),
    }
    state["ticket_jobs"]["10"]["review_budget"] = deepcopy(
        state["ticket_jobs"]["10"]["deterministic_integration_record"][
            "review_budget"
        ]
    )
    state["ticket_jobs"]["10"]["review_budget_history"] = []
    state["ticket_jobs"]["2"].update(
        {
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "acceptance_record": {
                "reviewed_base_sha": git.resolve(str(state["run_branch"])),
                "reviewed_candidate_tree": "tree-2",
                "artifact": _passing_artifact(),
            },
        }
    )
    integration = state["ticket_jobs"]["2"]["deterministic_integration_record"]
    state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
        integration["review_budget"]
    )
    integration["integrated_sha"] = "integrated-2"
    integration["pr"]["merge_commit_sha"] = "integrated-2"
    records = ticket_completion_records(state)

    assert records == [
        {
            "ticket_number": 2,
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-2",
        },
        {
            "ticket_number": 10,
            "integrated_sha": "integrated-10",
            "effective_revision": "effective-10",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
        },
    ]


def test_ticket_integration_records_preserve_exact_pr_and_checks_evidence(
    git_repo: Path,
) -> None:
    state, _states, _git = _completed_run(git_repo)
    state["ticket_jobs"]["2"].update(
        {
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "deterministic_integration_record": _integration_record(
                pr_number=21,
                publication_sha="published-2",
                integrated_sha="integrated-2",
                acceptance_record={"artifact": _passing_artifact()},
            ),
        }
    )
    state["ticket_jobs"]["2"]["review_budget"] = deepcopy(
        state["ticket_jobs"]["2"]["deterministic_integration_record"][
            "review_budget"
        ]
    )
    state["ticket_jobs"]["2"]["deterministic_integration_record"][
        "required_checks_evidence"
    ]["checks"] = [{"name": "test", "bucket": "pass", "state": "SUCCESS"}]
    state["ticket_jobs"]["10"] = {
        "ticket_number": 10,
        "phase": "completed",
        "integrated_sha": "integrated-10",
        "effective_revision": "effective-10",
        "deterministic_integration_record": _integration_record(
            source="fallback",
            pr_number=31,
            candidate_sha="candidate-10",
            candidate_tree="tree-10",
            publication_sha="published-10",
            integrated_sha="integrated-10",
            fallback_receipt={"candidate_sha": "candidate-10"},
        ),
    }

    records = ticket_integration_records(state)

    assert [record["ticket_number"] for record in records] == [2, 10]
    assert records[0]["integration_record"]["pr_number"] == 21
    assert records[0]["integration_record"]["pr"]["head_sha"] == "published-2"
    assert records[0]["integration_record"]["required_checks_evidence"] == {
        "pr_number": 21,
        "head_sha": "published-2",
        "result": "pass",
        "checks": [{"name": "test", "bucket": "pass", "state": "SUCCESS"}],
    }
    assert records[1]["integration_record"]["source"] == "fallback"

def test_closed_ticket_edits_do_not_change_its_completion_revision(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    original_ticket = dict(state["ticket_graph"]["tickets"]["2"])
    original_completion = ticket_completion_records(state)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["title"] = "Edited after completion"
    data["issues"]["2"]["body"] = "This edit is outside the completed Ticket."
    data["parent"]["body"] = "The live Parent changed after Ticket completion."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["ticket_graph"]["tickets"]["2"] == original_ticket
    assert ticket_completion_records(refreshed) == original_completion

def test_parent_drift_immediately_stales_completed_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))
    assert accepted["run_acceptance"]["phase"] == "accepted"
    accepted["run_acceptance"].update(
        {
            "prior_human_blockers": ["An old blocker must not cross generations."],
            "human_response_history": [{"generation": 1, "response": "old"}],
            "reviewer_resume_thread_id": "old-run-reviewer",
        }
    )
    accepted["run_publication"] = {"phase": "pending"}
    states.save_run(str(state["run_id"]), accepted)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Updated after all Tickets completed."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["terminal_kind"] == "run_acceptance_stale"
    assert refreshed["run_acceptance"]["phase"] == "pending"
    assert "prior_human_blockers" not in refreshed["run_acceptance"]
    assert "human_response_history" not in refreshed["run_acceptance"]
    assert "reviewer_resume_thread_id" not in refreshed["run_acceptance"]
    assert refreshed["run_publication"]["phase"] == "stale"

def test_reopened_completed_ticket_fails_closed(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["state"] = "OPEN"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "unsupported_scope_change"
    assert refreshed["terminal_kind"] == "unsupported_scope_change"
    assert refreshed["diagnostics"] == [
        {
            "code": "completed_ticket_reopened",
            "message": "Completed Ticket #2 was reopened outside Run Abandonment Recovery",
            "ticket_numbers": [2],
        }
    ]

def test_run_acceptance_new_thread_resume_omits_failed_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    default_head = git.resolve("main")
    run_head = git.resolve(str(state["run_branch"]))
    expected_merge_tree = git.expected_merge_tree(
        default_head_sha=default_head,
        run_head_sha=run_head,
    )
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
        currentness_boundary=run_currentness_boundary(
            state,
            reviewed_head_sha=run_head,
            reviewed_default_base_sha=default_head,
            expected_merge_tree=expected_merge_tree,
        ),
    )
    state["run_acceptance"]["pending_semantic_attempt"] = state[
        "active_agent_invocation"
    ]["semantic_attempt"]
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=True)

    class NewThreadReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            assert request["thread_id"] is None
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="new-run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="new-run-reviewer", attempt_count=1)
            return ReviewResult("new-run-reviewer", _passing_artifact())

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=NewThreadReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(resumed["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["reviewer_thread_ids"] == [
        "new-run-reviewer"
    ]

def test_run_repair_development_human_blocker_stops_before_candidate_or_pr(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, git = _completed_run(repo, state_root=workspace.state_root)
    fixture = repo / "github.json"

    class BlockedRunRepairDevelopment(ScriptedRunAgents):
        def develop(
            self, request: dict[str, Any]
        ) -> DevelopmentResult | HumanBlockerResult:
            self.development_requests.append(request)
            return HumanBlockerResult(
                thread_id="run-repair-development-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairDevelopment(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "developing"
    assert repair["development_thread_id"] == "run-repair-development-blocked"
    assert "candidate_sha" not in repair
    assert blocked["run_acceptance"]["repair_cycle"]["status"] == "human_blocked"
    old_generation = repair["repair_generation"]
    old_attempt = deepcopy(repair["pending_semantic_attempt"])
    old_budget = deepcopy(repair["review_budget"])
    old_checkout = Path(str(repair["repair_checkout"]))
    assert old_checkout.exists()
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["pull_requests"] == []
    status = stdout_json(
        invoke_cli_inprocess(repo, fixture, "status", str(state["run_id"]), "--json")
    )
    assert status["status"] == "ready_for_human"
    assert status["diagnostics"][0]["message"].startswith("GitHub denied access")
    history = stdout_json(
        invoke_cli_inprocess(repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("thread_id") == "run-repair-development-blocked"
        and event.get("worker") == "开发工作代理"
        and event.get("human_blockers")
        for event in history["timeline"]
    )

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        human_response="Issue read access has been granted.",
    )

    resumed_run = resumed["run_acceptance"]
    resumed_job = resumed_run["repair_job"]
    assert resumed_job["repair_generation"] == old_generation
    assert resumed_job["phase"] == "developing"
    assert resumed_job["pending_semantic_attempt"] == old_attempt
    assert resumed_job["review_budget"] == old_budget
    assert resumed_job["development_thread_id"] == "run-repair-development-blocked"
    assert resumed_job["human_response_history"] == [
        {
            "generation": old_generation,
            "human_blockers": repair["human_blockers"],
            "response": "Issue read access has been granted.",
        }
    ]
    assert resumed_run["repair_cycle"]["status"] == "active"
    assert resumed_run.get("repair_cycle_history", []) == []

def test_run_repair_reviewer_human_blocker_history_uses_reviewer_thread(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, git = _completed_run(repo, state_root=workspace.state_root)
    fixture = repo / "github.json"

    class BlockedRunRepairReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_repair_artifact(), _human_artifact()]

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["blocked_reason"] == "reviewer_requires_human"
    assert repair["reviewer_thread_ids"][-1] == "run-reviewer-2"
    old_generation = repair["repair_generation"]
    old_attempt = deepcopy(repair["pending_semantic_attempt"])
    old_budget = deepcopy(repair["review_budget"])
    old_candidate = repair["candidate_sha"]
    old_checkout = Path(str(repair["repair_checkout"]))
    assert blocked["run_acceptance"]["repair_cycle"]["status"] == "human_blocked"
    assert not old_checkout.exists()
    history = stdout_json(
        invoke_cli_inprocess(repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "独立验收工作代理"
        and event.get("thread_id") == "run-reviewer-2"
        and event.get("human_blockers")
        for event in history["timeline"]
    )

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        human_response="The review dependency is now available.",
    )
    resumed_run = resumed["run_acceptance"]
    resumed_job = resumed_run["repair_job"]
    assert resumed_job["repair_generation"] == old_generation
    assert resumed_job["phase"] == "candidate"
    assert resumed_job["candidate_sha"] == old_candidate
    assert resumed_job["pending_semantic_attempt"] == old_attempt
    assert resumed_job["review_budget"] == old_budget
    assert resumed_job["reviewer_thread_ids"][-1] == "run-reviewer-2"
    assert resumed_job["review_human_blocker_resume"] is True
    assert resumed_run["repair_cycle"]["status"] == "active"
    assert resumed_run.get("repair_cycle_history", []) == []

def test_run_repair_publication_human_blocker_stops_before_pr_mutation(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, git = _completed_run(repo, state_root=workspace.state_root)
    fixture = repo / "github.json"

    class BlockedRunRepairPublication(ScriptedRunAgents):
        def publication(
            self, request: dict[str, Any]
        ) -> HumanBlockerResult:
            del request
            return HumanBlockerResult(
                thread_id="run-repair-publication-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairPublication(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "accepted"
    assert repair["publication_thread_id"] == "run-repair-publication-blocked"
    assert repair["publication_attempts"] == 1
    assert "publication_sha" not in repair
    old_generation = repair["repair_generation"]
    old_attempt = deepcopy(repair["pending_semantic_attempt"])
    old_budget = deepcopy(repair["review_budget"])
    old_candidate = repair["candidate_sha"]
    old_checkout = Path(str(repair["repair_checkout"]))
    assert blocked["run_acceptance"]["repair_cycle"]["status"] == "human_blocked"
    assert not old_checkout.exists()
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    history = stdout_json(
        invoke_cli_inprocess(repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "发布工作代理"
        and event.get("attempt") == 1
        and event.get("thread_id") == "run-repair-publication-blocked"
        and event.get("human_blockers")
        for event in history["timeline"]
    )

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        human_response="Publication access has been restored.",
    )
    resumed_run = resumed["run_acceptance"]
    resumed_job = resumed_run["repair_job"]
    assert resumed_job["repair_generation"] == old_generation
    assert resumed_job["phase"] == "accepted"
    assert resumed_job["candidate_sha"] == old_candidate
    assert resumed_job["pending_semantic_attempt"] == old_attempt
    assert resumed_job["review_budget"] == old_budget
    assert resumed_job["publication_attempts"] == 1
    assert resumed_job["publication_thread_id"] == "run-repair-publication-blocked"
    assert resumed_run["repair_cycle"]["status"] == "active"
    assert resumed_run.get("repair_cycle_history", []) == []

def test_run_acceptance_rejects_ticket_or_previous_reviewer_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("ticket-reviewer", _passing_artifact())

    with pytest.raises(ValueError, match="new Reviewer Thread"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedReviewer(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))

@pytest.mark.parametrize("new_thread", [False, True])
def test_run_acceptance_human_resume_uses_selected_thread_and_clears_current_blocker(
    git_repo: Path,
    new_thread: bool,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenPassingReviewer:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []
            self.checkouts: list[Path] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.requests.append(request)
            self.checkouts.append(Path(str(request["checkout"])))
            if len(self.requests) == 1:
                return ReviewResult("blocked-run-reviewer", _human_artifact())
            assert request["thread_id"] == (
                None if new_thread else "blocked-run-reviewer"
            )
            if new_thread:
                assert request["_invocation_mode"] == "new-thread"
            else:
                assert "_invocation_mode" not in request
            assert request["prior_human_blockers"] == [_BLOCKED_EVIDENCE]
            assert request["human_response_history"] == [
                {
                    "generation": 1,
                    "human_blockers": [
                        _BLOCKED_EVIDENCE
                    ],
                    "response": "Issue read access has been granted.",
                }
            ]
            return ReviewResult(
                "new-run-reviewer" if new_thread else "blocked-run-reviewer",
                _passing_artifact(),
            )

    agents = HumanThenPassingReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )

    blocked = engine.accept(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    assert all(not checkout.exists() for checkout in agents.checkouts)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        new_thread=new_thread,
        human_response="Issue read access has been granted.",
    )
    assert resumed["run_acceptance"]["prior_human_blockers"] == [
        _BLOCKED_EVIDENCE
    ]
    assert resumed["run_acceptance"]["human_response_history"] == [
        {
            "generation": 1,
            "human_blockers": [
                _BLOCKED_EVIDENCE
            ],
            "response": "Issue read access has been granted.",
        }
    ]

    accepted = engine.accept(str(state["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    assert "previous_acceptance_artifact" not in agents.requests[1]
    assert "previous_review_identity" not in agents.requests[1]
    assert agents.requests[1]["review_budget_context"] == {
        "current_review_attempt": 1,
        "remaining_review_attempts": 10,
    }
    run = accepted["run_acceptance"]
    assert run["reviewer_thread_ids"] == (
        ["blocked-run-reviewer", "new-run-reviewer"]
        if new_thread
        else ["blocked-run-reviewer"]
    )
    assert len(set(agents.checkouts)) == 1
    assert all(not checkout.exists() for checkout in agents.checkouts)
    assert run["human_blocker_history"] == [
        {
            "phase": "pending",
            "human_blockers": [
                _BLOCKED_EVIDENCE
            ],
        }
    ]
    for key in ("human_blockers", "human_blocker_phase", "prior_human_blockers"):
        assert key not in run

def test_run_acceptance_human_resume_rejects_an_older_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenHistoricalReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            self.calls += 1
            if self.calls == 1:
                return ReviewResult("blocked-latest-reviewer", _human_artifact())
            return ReviewResult("older-reviewer", _passing_artifact())

    agents = HumanThenHistoricalReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    blocked = engine.accept(str(state["run_id"]))
    run = blocked["run_acceptance"]
    run["reviewer_thread_ids"].insert(0, "older-reviewer")
    states.save_run(str(state["run_id"]), blocked)
    Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), resume_human_blocker=True)

    with pytest.raises(
        ValueError, match="Human Blocker resume requires the latest Reviewer Thread"
    ):
        engine.accept(str(state["run_id"]))

def test_stale_run_repair_publication_returns_to_fresh_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    run = state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "modification_attempts": 0,
        "validation_attempts": 0,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "acceptance_record": {"reviewed_head_sha": git.resolve(state["run_branch"])},
    }
    job: dict[str, Any] = {
        "phase": "publication_pending",
        "repair_source": "acceptance",
        "repair_mode": "squash",
        "acceptance_artifact": _repair_artifact(),
    }
    run["repair_job"] = job

    engine._invalidate_stale_repair(
        state, job, git_repo / "unused-checkout"
    )

    assert job["phase"] == "stale"
    assert "repair_job" not in run
    assert state["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"

def test_run_acceptance_rejects_run_repair_developer_as_a_reviewer(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedRunDeveloper(ScriptedRunAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            artifact = self._reviews.pop(0)
            thread_id = (
                "run-reviewer-1"
                if len(self.review_requests) == 1
                else "run-repair-developer"
            )
            return ReviewResult(thread_id, artifact)

    with pytest.raises(
        ValueError, match="Fresh Acceptance cannot reuse the Development Thread"
    ):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedRunDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))

def test_run_repair_rejects_ticket_development_thread_reuse(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedTicketDeveloper(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            return DevelopmentResult("ticket-developer", result.summary)

    with pytest.raises(ValueError, match="not independent"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedTicketDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))

def test_run_repair_rejects_current_reviewer_thread_as_development(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedCandidateReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_repair_artifact(), _candidate_finding_artifact()]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if not self.development_requests:
                return super().develop(request)
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            (checkout / "follow-up-repair.txt").write_text(
                "repaired reviewer finding\n", encoding="utf-8"
            )
            return DevelopmentResult(
                "run-reviewer-2", "Illegally reused the Candidate Reviewer."
            )

    with pytest.raises(ValueError, match="not independent"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedCandidateReviewer(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))

def test_run_acceptance_discards_a_review_when_its_parent_snapshot_drifts(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    fixture = git_repo / "github.json"

    class DriftingReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["parent"]["body"] = "Changed while the Reviewer was running."
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return ReviewResult("run-reviewer", _passing_artifact())

    pending = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=DriftingReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert pending["run_acceptance"]["phase"] == "pending"
    assert "acceptance_artifact" not in pending["run_acceptance"]
    assert "acceptance_record" not in pending["run_acceptance"]

def test_run_acceptance_refreshes_currentness_before_reusing_an_accepted_run(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    initial = ScriptedRunAgents()
    initial._reviews = [_passing_artifact()]
    RunAcceptanceEngine(
        git=git,
        states=states,
        agents=initial,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed after acceptance."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    class FreshReviewer:
        def __init__(self) -> None:
            self.review_requests: list[dict[str, Any]] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            return ReviewResult("fresh-run-reviewer", _passing_artifact())

    refreshed = FreshReviewer()

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=refreshed,
        github=FixtureGitHubPublisher(fixture, git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2
    assert len(refreshed.review_requests) == 1

def test_run_acceptance_fixture_repairs_malformed_output_in_same_thread(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, _git = _completed_run(repo, state_root=workspace.state_root)
    agents = repo / "repair-run-review.json"
    agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "run-reviewer-thread",
                        "thread_id": "run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        repo,
        repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["reported_thread_id"] == "run-reviewer-thread"
    assert invocation["attempt_count"] == 2
    assert persisted["run_acceptance"]["review_budget"][
        "reviewer_invocations"
    ] == 1

def test_run_acceptance_fixture_missing_thread_marks_invocation_failed(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, _git = _completed_run(repo, state_root=workspace.state_root)
    agents = repo / "missing-run-review-thread.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"no_thread": True, "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    failed = run_internal_stage(
        repo,
        repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["active_agent_invocation"]["status"] == "failed"

def test_run_acceptance_fixture_records_a_fresh_thread_without_expectation(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, _git = _completed_run(repo, state_root=workspace.state_root)
    agents = repo / "fresh-run-review.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"thread_id": "fresh-run-reviewer", "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        repo,
        repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["requested_thread_id"] is None
    assert invocation["reported_thread_id"] == "fresh-run-reviewer"

def test_run_acceptance_fixture_resume_gets_a_fresh_repair_budget(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, _git = _completed_run(repo, state_root=workspace.state_root)
    seed_idle_control(
        TaskControlStore(states.root),
        TaskKey(repo, str(state["repository"]), 1),
        str(state["run_id"]),
        state_dir=states.root,
    )
    failed_agents = repo / "failed-run-review.json"
    failed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = run_internal_stage(
        repo,
        repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2

    resumed_agents = repo / "resumed-run-review.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    }
                ],
                "run_publications": [final_run_publication()],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        repo,
        repo / "github.json",
        "resume",
        str(state["run_id"]),
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    review_history = [
        invocation
        for invocation in persisted["agent_invocation_history"]
        if invocation["role"] == "reviewer"
    ]
    assert review_history[-2]["status"] == "failed"
    assert review_history[-2]["attempt_count"] == 3
    assert review_history[-1]["status"] == "completed"
    assert review_history[-1]["attempt_count"] == 1
