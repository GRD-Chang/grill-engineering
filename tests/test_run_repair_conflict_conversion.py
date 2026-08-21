from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from run_acceptance_test_support import (
    _completed_run,
    _passing_artifact,
    _repair_artifact,
)


@pytest.mark.parametrize(
    ("drift_boundary", "conversion_crash", "post_crash_drift"),
    [
        pytest.param("development", False, False, id="development"),
        pytest.param("candidate_validation", False, False, id="candidate-validation"),
        pytest.param("publication", False, False, id="publication"),
        pytest.param(
            "candidate_validation", True, False, id="conversion-crash-resume"
        ),
        pytest.param(
            "candidate_validation",
            True,
            True,
            id="conversion-crash-then-default-drift",
        ),
        pytest.param(
            "development_double", False, False, id="reprepared-development-drift"
        ),
    ],
)
def test_squash_repair_default_drift_conflict_converts_to_merge_resolution(
    git_repo: Path,
    drift_boundary: str,
    conversion_crash: bool,
    post_crash_drift: bool,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    run_branch = str(state["run_branch"])
    run_head = git.resolve(run_branch)
    initial_default = git.resolve("main")
    class CrashAfterConversionGit(GitRepository):
        crashed = False

        def convert_squash_candidate_to_integration_repair(
            self,
            checkout: Path,
            *,
            run_head_sha: str,
            default_head_sha: str,
            candidate_sha: str,
        ) -> str:
            evidence = super().convert_squash_candidate_to_integration_repair(
                checkout,
                run_head_sha=run_head_sha,
                default_head_sha=default_head_sha,
                candidate_sha=candidate_sha,
            )
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("crash after preparing squash conflict conversion")
            return evidence

    execution_git: GitRepository = (
        CrashAfterConversionGit(git_repo) if conversion_crash else git
    )
    publisher = FixtureGitHubPublisher(fixture, execution_git)
    repair_checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    advanced_default: list[str] = []

    def advance_default(*, again: bool = False) -> None:
        if advanced_default and not again:
            return
        (git_repo / "shared-drift.txt").write_text(
            f"latest default {len(advanced_default) + 1}\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "shared-drift.txt"], cwd=git_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"advance default during {drift_boundary}"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        advanced_default.append(git.resolve("main"))
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data["default_head_sha"] = advanced_default[-1]
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        publisher.data["default_head_sha"] = advanced_default[-1]

    class ConflictConvertingAgents:
        def __init__(self) -> None:
            self.development_requests: list[dict[str, Any]] = []
            self.review_requests: list[dict[str, Any]] = []
            self.publication_requests: list[dict[str, Any]] = []

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            if len(self.development_requests) == 1:
                assert request["repair_source"] == "acceptance"
                (checkout / "shared-drift.txt").write_text(
                    "ordinary repair\n", encoding="utf-8"
                )
                if drift_boundary in {"development", "development_double"}:
                    advance_default()
            else:
                assert request["repair_source"] == "merge_conflict"
                assert "shared-drift.txt" in str(request["merge_conflict_evidence"])
                assert subprocess.run(
                    ["git", "rev-parse", "MERGE_HEAD"],
                    cwd=checkout,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip() == advanced_default[-1]
                (checkout / "shared-drift.txt").write_text(
                    "resolved latest boundary\n", encoding="utf-8"
                )
                if drift_boundary == "development_double" and len(
                    self.development_requests
                ) == 2:
                    advance_default(again=True)
            return DevelopmentResult(
                thread_id="run-repair-developer",
                summary="Resolved the repair at its current default boundary.",
            )

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            candidate_review_count = sum(
                previous.get("candidate_acceptance") is True
                for previous in self.review_requests
            )
            if request.get("candidate_acceptance") is not True:
                artifact = _repair_artifact()
            else:
                if (
                    drift_boundary == "candidate_validation"
                    and candidate_review_count == 1
                ):
                    advance_default()
                artifact = _passing_artifact()
            return ReviewResult(
                thread_id=f"run-reviewer-{len(self.review_requests)}",
                artifact=artifact,
            )

        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            self.publication_requests.append(request)
            if drift_boundary == "publication" and not advanced_default:
                advance_default()
            return {
                "commit_message": "fix(run): reconcile repair with latest default",
                "pr_title": "fix(run): reconcile repair with latest default",
                "pr_body_markdown": (
                    "## What Problem This Solves\n\nThe repair conflicted with default.\n\n"
                    "## Why This Change Was Made\n\nThe exact conflict was resolved.\n\n"
                    "## User Impact\n\nThe Run remains mergeable.\n\n"
                    "## Evidence\n\nThe latest two-parent Candidate passed validation."
                ),
            }

    agents = ConflictConvertingAgents()
    current_default = initial_default
    crash_observed = False
    for _ in range(6):
        try:
            result = RunAcceptanceEngine(
                git=execution_git,
                states=states,
                agents=agents,
                github=publisher,
                default_head_sha=current_default,
                currentness_reader=FixtureGitHubReader(fixture),
            ).accept(str(state["run_id"]))
        except RuntimeError as error:
            assert conversion_crash and not crash_observed
            assert str(error) == "crash after preparing squash conflict conversion"
            crash_observed = True
            persisted = states.load_current_run(str(state["run_id"]))
            assert persisted is not None
            persisted_job = persisted["run_acceptance"]["repair_job"]
            assert persisted_job["integration_squash_conversion_required"] is True
            assert persisted_job["repair_mode"] == "merge_resolution"
            prepared_default = advanced_default[-1]
            assert subprocess.run(
                ["git", "rev-parse", "MERGE_HEAD"],
                cwd=repair_checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip() == prepared_default
            if post_crash_drift:
                advance_default(again=True)
            execution_git = git
            publisher = FixtureGitHubPublisher(fixture, execution_git)
            continue
        if result["status"] == "run_publication_pending":
            break
        assert result["status"] == "run_acceptance_pending", result
        current_default = advanced_default[-1] if advanced_default else initial_default

    assert result["status"] == "run_publication_pending", result
    assert crash_observed is conversion_crash
    run = result["run_acceptance"]
    latest_default = advanced_default[-1]
    candidate = str(run["candidate_sha"])
    integrated = str(run["integrated_sha"])
    assert run["repair_cycle"]["generation"] == 1
    assert run["development_thread_history"] == ["run-repair-developer"]
    assert len(agents.development_requests) == (
        3 if drift_boundary == "development_double" else 2
    )
    assert {Path(str(request["checkout"])) for request in agents.development_requests} == {
        repair_checkout
    }
    assert git.commit_parents(candidate) == [run_head, latest_default]
    assert git.resolve(f"{candidate}^{{tree}}") == git.resolve(
        f"{integrated}^{{tree}}"
    )
    assert git.expected_merge_tree(
        default_head_sha=latest_default,
        run_head_sha=integrated,
    ) == git.resolve(f"{candidate}^{{tree}}")
    assert git.is_ancestor(latest_default, integrated)
    assert not repair_checkout.exists()
