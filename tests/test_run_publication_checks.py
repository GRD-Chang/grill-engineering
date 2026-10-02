from __future__ import annotations

from support.run_scenarios import _accepted_run

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import GhGitHubPublisher
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine


from support.run_publication import RunPublicationAgents


class SnapshotOnlyRunPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.snapshot_calls: list[tuple[int, str]] = []

    def required_checks(self, _pr_number: int) -> str:
        raise AssertionError("Final Run must not use the aggregate check read")

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        self.snapshot_calls.append((pr_number, expected_head_sha))
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class SnapshotUnavailableAfterFirstReadPublisher(SnapshotOnlyRunPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_after_first_read = False

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        if self.fail_after_first_read:
            raise TimeoutError("final Required Checks snapshot unavailable")
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class SnapshotIdentityMismatchPublisher(SnapshotOnlyRunPublisher):
    def __init__(self, *args: Any, mismatch_kind: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mismatch_kind = mismatch_kind
        self.mismatch_enabled = False

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        snapshot = super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )
        if not self.mismatch_enabled:
            return snapshot
        if self.mismatch_kind == "pr":
            snapshot["pr_number"] = pr_number + 1
        else:
            snapshot["head_sha"] = "d" * 40
        return snapshot


class EvidenceDriftPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, drift_kind: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.drift_kind = drift_kind
        self.fail_revalidation = False

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        if self.fail_revalidation:
            raise GitHubReadError(
                "github_timeout", "Final Run PR identity has not converged"
            )
        return super().live_pull_request(pr_number)

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        assert expected_head_sha is not None
        delivery = self.data["delivery"]
        assert isinstance(delivery, dict)
        if self.drift_kind == "head":
            delivery["live_head_override"] = "e" * 40
        elif self.drift_kind == "identity":
            pulls = delivery["pull_requests"]
            assert isinstance(pulls, list)
            pull = next(
                pull
                for pull in pulls
                if isinstance(pull, dict) and pull.get("number") == pr_number
            )
            pull["head_repository"] = "attacker/project"
        else:
            self.fail_revalidation = True
        self._save()
        return {
            "pr_number": pr_number,
            "checks": [
                {
                    "name": "fixture-required-check",
                    "workflow": "fixture-ci",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "link": "https://example.invalid/checks/fixture",
                    "repairability": "code_failure",
                    "job": {
                        "id": 1,
                        "head_sha": expected_head_sha,
                        "name": "fixture-required-check",
                        "workflow_name": "fixture-ci",
                        "status": "completed",
                        "conclusion": "failure",
                        "steps": [
                            {
                                "name": "Run tests",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    },
                }
            ],
        }


class MergeBoundaryDriftPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, drift_kind: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.drift_kind = drift_kind
        self.arm_drift_after_status = False
        self.drift_enabled = False
        self.merge_calls = 0

    def record_agent_run_status(
        self, pr_number: int, status: dict[str, Any]
    ) -> None:
        super().record_agent_run_status(pr_number, status)
        if self.arm_drift_after_status:
            self.drift_enabled = True

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        if self.drift_enabled:
            if self.drift_kind == "head_repository":
                live["head_repository"] = "attacker/project"
            elif self.drift_kind == "base_branch":
                live["base_branch"] = "attacker-base"
            else:
                live["base_sha"] = "f" * 40
        return live

    def normal_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        expected_head_branch: str | None = None,
        expected_head_repository: str | None = None,
        expected_base_branch: str | None = None,
        expected_base_sha: str | None = None,
        expected_base_repository: str | None = None,
    ) -> str:
        self.merge_calls += 1
        return super().normal_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            expected_head_branch=expected_head_branch,
            expected_head_repository=expected_head_repository,
            expected_base_branch=expected_base_branch,
            expected_base_sha=expected_base_sha,
            expected_base_repository=expected_base_repository,
        )


def test_final_run_publish_and_approve_share_exact_head_observation(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = SnapshotOnlyRunPublisher(git_repo / "github.json", git)
    publisher.data["delivery"].update(
        {"required_checks": ["pass", "pending"], "check_position": 0}
    )
    publisher._save()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    published = engine.publish(str(state["run_id"]))
    run_head = git.resolve(str(state["run_branch"]))
    first_observation = published["run_publication"]["required_checks_evidence"]

    assert published["status"] == "run_approval_pending"
    assert first_observation == {
        "pr_number": published["run_publication"]["pr_number"],
        "head_sha": run_head,
        "result": "pass",
        "checks": first_observation["checks"],
    }
    assert first_observation["checks"]

    waiting = engine.approve(str(state["run_id"]))
    second_observation = waiting["run_publication"]["required_checks_evidence"]

    assert waiting["status"] == "waiting_checks"
    assert second_observation["pr_number"] == published["run_publication"]["pr_number"]
    assert second_observation["head_sha"] == run_head
    assert second_observation["result"] == "pending"
    assert publisher.snapshot_calls == [
        (published["run_publication"]["pr_number"], run_head),
        (published["run_publication"]["pr_number"], run_head),
    ]
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["run_publication"]["required_checks_evidence"] == second_observation


def test_final_run_approve_keeps_last_observation_but_cannot_merge_when_snapshot_is_unavailable(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = SnapshotUnavailableAfterFirstReadPublisher(git_repo / "github.json", git)
    publisher.data["delivery"]["required_checks"] = ["pass"]
    publisher._save()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    published = engine.publish(str(state["run_id"]))
    previous_observation = published["run_publication"]["required_checks_evidence"]
    publisher.fail_after_first_read = True

    waiting = engine.approve(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert waiting["run_publication"]["required_checks_evidence"] == previous_observation
    assert waiting["run_publication"]["required_checks_observation_status"] == "unavailable"
    assert publisher.live_pull_request(1)["state"] == "OPEN"
    assert publisher.data["delivery"]["agent_run_status"][-1]["required_checks"] == "unavailable"


@pytest.mark.parametrize("drift_kind", ["head_repository", "base_branch", "base_sha"])
def test_final_run_approve_revalidates_full_identity_before_merge(
    git_repo: Path, drift_kind: str
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = MergeBoundaryDriftPublisher(
        git_repo / "github.json", git, drift_kind=drift_kind
    )
    publisher.data["delivery"]["required_checks"] = ["pass"]
    publisher._save()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    published = engine.publish(str(state["run_id"]))
    publisher.arm_drift_after_status = True
    result = engine.approve(str(state["run_id"]))

    assert published["status"] == "run_approval_pending"
    if drift_kind == "base_sha":
        assert result["status"] == "waiting_external"
        assert result["diagnostics"][0]["code"] == "github_pr_head_observation_pending"
    else:
        assert result["status"] == "run_acceptance_pending"
        assert result["run_publication"]["phase"] == "stale"
        assert "approval_grant" not in result["run_publication"]
    assert "merge_intent" not in result["run_publication"]
    assert publisher.merge_calls == 0
    assert publisher.data["delivery"]["pull_requests"][0]["state"] == "OPEN"


@pytest.mark.parametrize("mismatch_kind", ["pr", "head"])
@pytest.mark.parametrize("operation", ["publish", "approve"])
def test_snapshot_identity_drift_invalidates_final_run_without_supervision(
    git_repo: Path, mismatch_kind: str, operation: str
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    initial_modification_attempts = state["run_acceptance"]["modification_attempts"]
    publisher = SnapshotIdentityMismatchPublisher(
        git_repo / "github.json", git, mismatch_kind=mismatch_kind
    )
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    if operation == "publish":
        publisher.mismatch_enabled = True
        result = engine.publish(str(state["run_id"]))
    else:
        published = engine.publish(str(state["run_id"]))
        assert published["status"] == "run_approval_pending"
        publisher.mismatch_enabled = True
        result = engine.approve(str(state["run_id"]))

    assert result["status"] == "run_acceptance_pending"
    assert result["run_acceptance"]["phase"] == "pending"
    assert result["run_publication"]["phase"] == "stale"
    assert "waiting_external" not in {
        result["status"], result["run_publication"]["phase"]
    }
    assert "approval_grant" not in result["run_publication"]
    assert "repair_request" not in result["run_acceptance"]
    assert "repair_job" not in result["run_acceptance"]
    assert (
        result["run_acceptance"]["modification_attempts"]
        == initial_modification_attempts
    )


@pytest.mark.parametrize("drift_kind", ["head", "identity"])
@pytest.mark.parametrize("operation", ["publish", "approve"])
def test_live_identity_drift_after_failure_evidence_cannot_start_repair(
    git_repo: Path, drift_kind: str, operation: str
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    initial_modification_attempts = state["run_acceptance"]["modification_attempts"]
    publisher = EvidenceDriftPublisher(
        git_repo / "github.json", git, drift_kind=drift_kind
    )
    if operation == "publish":
        publisher.data["delivery"]["required_checks"] = ["fail"]
    else:
        publisher.data["delivery"]["required_checks"] = ["none", "fail"]
    publisher._save()
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    if operation == "publish":
        result = engine.publish(str(state["run_id"]))
    else:
        published = engine.publish(str(state["run_id"]))
        assert published["status"] == "run_approval_pending"
        result = engine.approve(str(state["run_id"]))

    if drift_kind == "head":
        assert result["status"] == "waiting_external"
        assert result["diagnostics"][0]["code"] == "github_pr_head_observation_pending"
    else:
        assert result["status"] == "run_acceptance_pending"
        assert result["run_publication"]["phase"] == "stale"
        assert "required_checks_evidence" not in result["run_publication"]
    assert "repair_request" not in result["run_acceptance"]
    assert "repair_job" not in result["run_acceptance"]
    assert (
        result["run_acceptance"]["modification_attempts"]
        == initial_modification_attempts
    )


def test_live_identity_revalidation_read_failure_is_supervised(
    git_repo: Path,
) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    publisher = EvidenceDriftPublisher(
        git_repo / "github.json", git, drift_kind="unavailable"
    )
    publisher.data["delivery"]["required_checks"] = ["fail"]
    publisher._save()
    initial_modification_attempts = state["run_acceptance"]["modification_attempts"]

    result = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert result["status"] == "waiting_external"
    assert result["run_publication"]["phase"] == "waiting_external"
    assert result["diagnostics"][0]["waiting_for"].endswith(
        "identity before Required Check repair"
    )
    assert "repair_request" not in result["run_acceptance"]
    assert "repair_job" not in result["run_acceptance"]
    assert (
        result["run_acceptance"]["modification_attempts"]
        == initial_modification_attempts
    )


def test_final_check_failure_enters_shared_run_repair(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["fail"]
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    failed_checks = engine.publish(str(state["run_id"]))

    assert failed_checks["status"] == "run_acceptance_pending"
    assert (
        failed_checks["run_acceptance"]["repair_request"]["repair_source"]
        == "required_checks"
    )

@pytest.mark.parametrize(
    "check",
    [
        {
            "name": "cancelled",
            "workflow": "ci",
            "bucket": "cancel",
            "state": "CANCELLED",
            "description": "cancelled",
            "link": "https://example.invalid/checks/cancelled",
        },
        {
            "name": "platform",
            "workflow": "ci",
            "bucket": "fail",
            "state": "TIMED_OUT",
            "description": "runner timeout",
            "link": "https://example.invalid/checks/platform",
        },
        {
            "name": "unknown",
            "workflow": "ci",
            "bucket": "fail",
            "description": "missing conclusion",
            "link": "https://example.invalid/checks/unknown",
        },
    ],
    ids=("cancelled", "platform", "unknown"),
)
def test_final_non_repairable_check_failure_is_supervised_without_candidate(
    git_repo: Path, check: dict[str, str]
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"].update(
        {
            "required_checks": ["fail"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [check],
            },
        }
    )
    before_run = deepcopy(state["run_acceptance"])

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["supervision_window"]["kind"] == "github_convergence"
    assert waiting["run_acceptance"] == before_run
    assert "repair_request" not in waiting["run_acceptance"]

def test_final_check_evidence_for_a_new_head_cannot_repair_the_approved_head(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    publisher.data["delivery"]["required_checks"] = ["fail"]
    publisher._save()

    class HeadDriftEvidencePublisher(FixtureGitHubPublisher):
        repository = "example/project"

        def _checks(self, _pr_number: int, _fields: str) -> list[object]:
            return [
                {
                    "name": "quality",
                    "workflow": "CI",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "description": "Tests failed on a replacement head.",
                    "link": (
                        "https://github.com/example/project/actions/runs/22/job/33"
                    ),
                }
            ]

        def _json(self, *_arguments: str, **_kwargs: object) -> object:
            return {
                "id": 33,
                "head_sha": "b" * 40,
                "name": "quality",
                "workflow_name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Run tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "number": 6,
                    }
                ],
            }

        required_check_evidence = GhGitHubPublisher.required_check_evidence

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=HeadDriftEvidencePublisher(git_repo / "github.json", git),
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert "repair_request" not in waiting["run_acceptance"]
    assert "repair_job" not in waiting["run_acceptance"]
    assert waiting["run_acceptance"]["modification_attempts"] == published[
        "run_acceptance"
    ]["modification_attempts"]

@pytest.mark.parametrize("mergeable", [False, None], ids=("false", "unknown"))
def test_final_mergeability_hint_with_clean_local_merge_starts_fresh_acceptance(
    git_repo: Path, mergeable: bool | None
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["mergeable"] = False
    if mergeable is None:
        original_live_pull_request = publisher.live_pull_request

        def live_pull_request(pr_number: int) -> dict[str, Any]:
            live = original_live_pull_request(pr_number)
            live["mergeable"] = None
            return live

        publisher.live_pull_request = live_pull_request  # type: ignore[method-assign]
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert published["status"] == "run_approval_pending"

    refreshed = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).approve(str(state["run_id"]))

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["run_acceptance"]["phase"] == "pending"
    assert "repair_request" not in refreshed["run_acceptance"]
    assert "repair_job" not in refreshed["run_acceptance"]

def test_final_integration_git_failure_does_not_start_repair(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["mergeable"] = False
    engine = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    engine.publish(str(state["run_id"]))
    pending = engine.approve(str(state["run_id"]))
    assert pending["status"] == "run_acceptance_pending"

    class FailingIntegrationGit(GitRepository):
        def prepare_run_repair_checkout(self, *, branch: str, checkout: Path) -> None:
            raise GitError("worktree creation failed")

    result = RunAcceptanceEngine(
        git=FailingIntegrationGit(git_repo), states=states,
        agents=RunPublicationAgents(), github=publisher,
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    assert result["status"] == "ready_for_human"
    assert result["diagnostics"][0]["code"] == "run_integration_failed"
    assert "worktree creation failed" in result["diagnostics"][0]["message"]
    assert "repair_job" not in result["run_acceptance"]
    assert result["run_acceptance"]["review_budget"] == state["run_acceptance"]["review_budget"]

# Real conflicts now use the ordinary integration conflict path, including
# Stop/Resume and its independent budget, in test_run_approval_update.py.

def test_approve_recovers_a_merge_that_succeeded_before_state_save(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    engine.publish(str(state["run_id"]))
    publisher.data["delivery"]["crash_after_normal_merge_once"] = True

    waiting = engine.approve(str(state["run_id"]))
    assert waiting["status"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"

    recovered = engine.approve(str(state["run_id"]))
    assert recovered["status"] == "completed"
    assert recovered["run_publication"]["phase"] == "merged"
    assert recovered["run_publication"]["merge_intent"]["attempts"] == 1

def test_approve_rejects_external_merge_without_persisted_final_run_intent(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )
    published = engine.publish(str(state["run_id"]))
    pr_number = int(published["run_publication"]["pr_number"])
    publisher.normal_merge(pr_number=pr_number, expected_head_sha=git.resolve(str(state["run_branch"])))

    with pytest.raises(GitHubReadError, match="persisted merge intent"):
        engine.approve(str(state["run_id"]))

    assert publisher.data["parent"].get("state") != "CLOSED"

def test_pending_check_that_later_fails_enters_shared_run_repair(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["pending", "fail"]
    engine = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    )

    waiting = engine.publish(str(state["run_id"]))
    assert waiting["status"] == "waiting_checks"
    repaired = engine.publish(str(state["run_id"]))

    assert repaired["status"] == "run_acceptance_pending"
    assert (
        repaired["run_acceptance"]["repair_request"]["repair_source"]
        == "required_checks"
    )

def test_unknown_final_required_checks_enters_supervised_wait(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["unknown"]

    waiting = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert waiting["status"] == "waiting_external"
    assert waiting["terminal_kind"] == "waiting_external"
    assert waiting["run_publication"]["phase"] == "waiting_external"
    assert waiting["supervision_window"]["kind"] == "github_convergence"

@pytest.mark.parametrize("bucket", ["skipping", "neutral"])
def test_fixture_non_failure_required_check_buckets_are_pass(
    git_repo: Path, bucket: str
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = [bucket]

    result = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))

    assert result["status"] == "run_approval_pending"
