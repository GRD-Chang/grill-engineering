from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine

from run_acceptance_test_support import (
    _candidate_finding_artifact,
    _completed_run,
    _passing_artifact,
)


class MergeResolutionAgents:
    def __init__(self, fixture: MergeResolutionFixture) -> None:
        self.fixture = fixture
        self.development_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        fixture = self.fixture
        checkout = Path(str(request["checkout"]))
        development_attempt = len(self.development_requests)
        resolving_conflict = (
            development_attempt == 1
            or (
                fixture.candidate_finding_default_drift
                and development_attempt >= 3
            )
            or (
                fixture.default_drift
                and not fixture.clean_default_drift
                and not fixture.candidate_finding_default_drift
            )
        )
        if resolving_conflict:
            self._resolve_conflict(request, checkout)
        else:
            assert request["repair_source"] == "acceptance"
            assert request["acceptance_artifact"] == _candidate_finding_artifact()
            if fixture.finding_overlaps_conflict:
                (checkout / "shared.txt").write_text(
                    "candidate finding repaired shared\n", encoding="utf-8"
                )
            elif fixture.finding_changes_d1_file:
                (checkout / "policy.txt").write_text(
                    "candidate finding repaired policy\n", encoding="utf-8"
                )
            else:
                (checkout / "follow-up.txt").write_text(
                    "candidate finding repaired\n", encoding="utf-8"
                )
            if fixture.candidate_finding_default_drift:
                fixture.advance_default()
        return DevelopmentResult(
            thread_id="integration-repair-developer",
            summary="Resolved the real three-way Git conflict.",
        )

    def _resolve_conflict(
        self, request: dict[str, Any], checkout: Path
    ) -> None:
        fixture = self.fixture
        assert request["repair_source"] == "merge_conflict"
        replay_conflict = (
            fixture.clean_replay_conflict and len(self.development_requests) >= 3
        )
        conflict_path = "policy.txt" if replay_conflict else "shared.txt"
        assert conflict_path in str(request["merge_conflict_evidence"])
        if fixture.candidate_finding_default_drift and len(
            self.development_requests
        ) >= 3:
            assert "acceptance_artifact" not in request
            if fixture.finding_overlaps_conflict:
                assert "candidate finding repaired shared" in (
                    checkout / "shared.txt"
                ).read_text(encoding="utf-8")
            elif fixture.finding_changes_d1_file:
                policy = (checkout / "policy.txt").read_text(encoding="utf-8")
                if replay_conflict:
                    assert "candidate finding repaired policy" in policy
                else:
                    assert policy == "candidate finding repaired policy\n"
            else:
                assert (checkout / "follow-up.txt").read_text(encoding="utf-8") == (
                    "candidate finding repaired\n"
                )
        expected_default = (
            fixture.advanced_default_heads[-1]
            if fixture.advanced_default_heads
            else fixture.default_head
        )
        checkout_head = _git_output(checkout, "rev-parse", "HEAD")
        assert checkout_head == fixture.run_head
        assert _git_output(checkout, "rev-parse", "MERGE_HEAD") == expected_default
        conflict_status = _git_output(checkout, "status", "--porcelain=v1")
        shared_status = next(
            line
            for line in conflict_status.splitlines()
            if line.endswith(conflict_path)
        )
        assert shared_status[:2] in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
        if fixture.clean_replay_modify_conflict and replay_conflict:
            index_stages = {
                line.split()[2]
                for line in _git_output(
                    checkout, "ls-files", "--stage", "--", "policy.txt"
                ).splitlines()
            }
            assert index_stages == {"1", "2", "3"}
        resolution = (
            "candidate finding repaired after default drift\n"
            if fixture.finding_overlaps_conflict and fixture.advanced_default_heads
            else "resolved after default drift\n"
            if fixture.advanced_default_heads
            else "resolved\n"
        )
        if fixture.staged_unresolved_conflict:
            subprocess.run(["git", "add", "shared.txt"], cwd=checkout, check=True)
            return
        if fixture.edited_unresolved_markers:
            original = (checkout / "shared.txt").read_text(encoding="utf-8")
            (checkout / "shared.txt").write_text(
                "touched but unresolved\n" + original, encoding="utf-8"
            )
            return
        if fixture.unresolved_conflict:
            return
        resolution_path = checkout / conflict_path
        if replay_conflict:
            resolution_path.write_text(
                "candidate finding repaired policy after default drift\n",
                encoding="utf-8",
            )
        elif fixture.delete_conflict_path:
            resolution_path.unlink()
        else:
            resolution_path.write_text(resolution, encoding="utf-8")
        if fixture.staged_interruption:
            subprocess.run(["git", "add", "shared.txt"], cwd=checkout, check=True)
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event(
                "thread_started",
                reported_thread_id="integration-repair-developer",
                attempt_count=1,
            )
            event(
                "failed",
                reported_thread_id="integration-repair-developer",
                attempt_count=1,
                error="controller_interrupted",
            )
            raise RuntimeError("controller interrupted staged resolution")
        if fixture.development_default_drift and not fixture.advanced_default_heads:
            fixture.advance_default()

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        fixture = self.fixture
        checkout = Path(str(request["checkout"]))
        assert request["candidate_acceptance"] is True
        expected_resolution = (
            "run branch\n"
            if fixture.clean_default_drift and fixture.advanced_default_heads
            else (
                "candidate finding repaired after default drift\n"
                if fixture.finding_overlaps_conflict
                and fixture.advanced_default_heads
                else "resolved after default drift\n"
                if fixture.advanced_default_heads
                else "resolved\n"
            )
        )
        if fixture.delete_conflict_path:
            assert not (checkout / "shared.txt").exists()
        else:
            assert (checkout / "shared.txt").read_text(encoding="utf-8") == (
                expected_resolution
            )
        if fixture.clean_replay_conflict and fixture.advanced_default_heads:
            assert (checkout / "policy.txt").read_text(encoding="utf-8") == (
                "candidate finding repaired policy after default drift\n"
            )
        if fixture.candidate_default_drift and not fixture.advanced_default_heads:
            fixture.advance_default()
        artifact = (
            _candidate_finding_artifact()
            if fixture.candidate_requires_follow_up
            and len(self.review_requests) == 1
            else _passing_artifact()
        )
        return ReviewResult(
            thread_id=f"integration-repair-reviewer-{len(self.review_requests)}",
            artifact=artifact,
        )

    def publication(self, _request: dict[str, Any]) -> dict[str, str]:
        return {
            "commit_message": "fix(run): resolve default branch conflict",
            "pr_title": "fix(run): resolve default branch conflict",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe Run could not merge.\n\n"
                "## Why This Change Was Made\n\nThe conflicting behavior is reconciled.\n\n"
                "## User Impact\n\nThe complete delivery is mergeable.\n\n"
                "## Evidence\n\nThe exact merge result passed fresh validation."
            ),
        }


class MergeResolutionFixture:
    def __init__(self, git_repo: Path, scenario: str) -> None:
        self.git_repo = git_repo
        self.scenario = scenario
        self.state, self.states, self.git = _completed_run(git_repo)
        self.fixture = git_repo / "github.json"
        self.run_branch = str(self.state["run_branch"])
        self.candidate_requires_follow_up = scenario in {
            "candidate_finding",
            "candidate_finding_default_drift",
            "candidate_finding_default_drift_policy_reversal",
            "candidate_finding_default_drift_d1_file",
            "candidate_finding_default_drift_overlap",
            "candidate_finding_default_drift_clean_replay_conflict",
            "candidate_finding_default_drift_clean_replay_modify_conflict",
        }
        self.candidate_finding_default_drift = scenario in {
            "candidate_finding_default_drift",
            "candidate_finding_default_drift_policy_reversal",
            "candidate_finding_default_drift_d1_file",
            "candidate_finding_default_drift_overlap",
            "candidate_finding_default_drift_clean_replay_conflict",
            "candidate_finding_default_drift_clean_replay_modify_conflict",
        }
        self.policy_reversal = (
            scenario == "candidate_finding_default_drift_policy_reversal"
        )
        self.finding_changes_d1_file = (
            scenario
            in {
                "candidate_finding_default_drift_d1_file",
                "candidate_finding_default_drift_clean_replay_conflict",
                "candidate_finding_default_drift_clean_replay_modify_conflict",
            }
        )
        self.finding_overlaps_conflict = (
            scenario == "candidate_finding_default_drift_overlap"
        )
        self.clean_replay_conflict = scenario in {
            "candidate_finding_default_drift_clean_replay_conflict",
            "candidate_finding_default_drift_clean_replay_modify_conflict",
        }
        self.clean_replay_modify_conflict = (
            scenario == "candidate_finding_default_drift_clean_replay_modify_conflict"
        )
        self.candidate_default_drift = scenario in {
            "candidate_default_drift",
            "candidate_default_drift_clean",
        }
        self.clean_default_drift = scenario in {
            "candidate_default_drift_clean",
            "development_default_drift_clean",
            "development_default_drift_clean_crash",
            "candidate_finding_default_drift_clean_replay_conflict",
            "candidate_finding_default_drift_clean_replay_modify_conflict",
        }
        self.publication_default_drift = scenario == "publication_default_drift"
        self.development_default_drift = scenario in {
            "development_default_drift",
            "development_default_drift_clean",
            "development_default_drift_clean_crash",
            "development_double_default_drift",
        }
        self.double_default_drift = scenario == "development_double_default_drift"
        self.staged_interruption = scenario == "development_staged_interruption"
        self.unresolved_conflict = scenario == "unresolved_conflict"
        self.staged_unresolved_conflict = scenario == "staged_unresolved_conflict"
        self.edited_unresolved_markers = scenario == "edited_unresolved_markers"
        self.delete_conflict_path = scenario == "delete_conflict_path"
        self.default_drift = (
            self.candidate_default_drift
            or self.publication_default_drift
            or self.development_default_drift
            or self.candidate_finding_default_drift
        )
        self.advanced_default_heads: list[str] = []
        self.run_head, self.default_head = self._prepare_conflicting_heads()
        self.agents = MergeResolutionAgents(self)

    def _prepare_conflicting_heads(self) -> tuple[str, str]:
        subprocess.run(
            ["git", "switch", self.run_branch], cwd=self.git_repo, check=True
        )
        (self.git_repo / "shared.txt").write_text("run branch\n", encoding="utf-8")
        subprocess.run(["git", "add", "shared.txt"], cwd=self.git_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: change shared file on run branch"],
            cwd=self.git_repo,
            check=True,
            capture_output=True,
        )
        run_head = self.git.resolve(self.run_branch)
        subprocess.run(["git", "switch", "main"], cwd=self.git_repo, check=True)
        (self.git_repo / "shared.txt").write_text(
            "default branch\n", encoding="utf-8"
        )
        if self.policy_reversal or self.finding_changes_d1_file:
            (self.git_repo / "policy.txt").write_text(
                "D1-only policy\n", encoding="utf-8"
            )
        paths = ["shared.txt"]
        if self.policy_reversal or self.finding_changes_d1_file:
            paths.append("policy.txt")
        subprocess.run(["git", "add", *paths], cwd=self.git_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: change shared file on default branch"],
            cwd=self.git_repo,
            check=True,
            capture_output=True,
        )
        default_head = self.git.resolve("main")
        self.state["ticket_jobs"]["2"]["integrated_sha"] = run_head
        self.states.save_run(str(self.state["run_id"]), self.state)
        fixture_data = json.loads(self.fixture.read_text(encoding="utf-8"))
        fixture_data["default_head_sha"] = default_head
        fixture_data.setdefault("delivery", {}).setdefault(
            "published_branches", {}
        )[self.run_branch] = run_head
        self.fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        return run_head, default_head

    def advance_default(self) -> None:
        (self.git_repo / "shared.txt").write_text(
            (
                "run branch\n"
                if self.clean_default_drift
                else f"advanced default branch {len(self.advanced_default_heads) + 1}\n"
            ),
            encoding="utf-8",
        )
        if self.policy_reversal or self.clean_replay_conflict:
            if self.clean_replay_modify_conflict:
                (self.git_repo / "policy.txt").write_text(
                    "D2 policy\n", encoding="utf-8"
                )
            else:
                (self.git_repo / "policy.txt").unlink()
        subprocess.run(["git", "add", "shared.txt"], cwd=self.git_repo, check=True)
        if self.policy_reversal or self.clean_replay_conflict:
            subprocess.run(
                ["git", "add", "--update", "policy.txt"],
                cwd=self.git_repo,
                check=True,
            )
        subprocess.run(
            ["git", "commit", "-m", "advance default during repair delivery"],
            cwd=self.git_repo,
            check=True,
            capture_output=True,
        )
        self.advanced_default_heads.append(self.git.resolve("main"))
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        data["default_head_sha"] = self.advanced_default_heads[-1]
        self.fixture.write_text(json.dumps(data), encoding="utf-8")

    def accept(self) -> dict[str, Any]:
        publisher: FixtureGitHubPublisher
        if self.publication_default_drift:
            publisher = PublicationDefaultDriftPublisher(
                self.fixture, self.git, self
            )
        else:
            publisher = FixtureGitHubPublisher(self.fixture, self.git)
        return self._engine(self.git, publisher, self.default_head).accept(
            str(self.state["run_id"])
        )

    def resume(self) -> dict[str, Any]:
        return self._engine(
            self.git,
            FixtureGitHubPublisher(self.fixture, self.git),
            self.advanced_default_heads[-1],
        ).accept(str(self.state["run_id"]))

    def _engine(
        self,
        git: GitRepository,
        publisher: FixtureGitHubPublisher,
        default_head: str,
    ) -> RunAcceptanceEngine:
        return RunAcceptanceEngine(
            git=git,
            states=self.states,
            agents=self.agents,
            github=publisher,
            default_head_sha=default_head,
            currentness_reader=FixtureGitHubReader(self.fixture),
        )

    def assert_reprepare_pending(self, result: dict[str, Any]) -> None:
        run = result["run_acceptance"]
        job = run["repair_job"]
        repair_checkout = Path(str(job["repair_checkout"]))
        assert result["status"] == "run_acceptance_pending"
        assert job["phase"] == "repairing"
        assert "candidate_sha" not in job
        assert job["integration_reprepare_required"] is True
        if self.development_default_drift:
            assert "integration_reprepare_candidate_sha" not in job
            assert job["pending_attempt"] == 1
            assert job["modification_attempts"] == 0
            assert _git_output(repair_checkout, "rev-parse", "MERGE_HEAD") == (
                self.default_head
            )
        elif self.candidate_finding_default_drift:
            assert isinstance(job["integration_reprepare_candidate_sha"], str)
            first_candidate = str(job["integration_reprepare_candidate_sha"])
            snapshot = str(job["integration_finding_snapshot_sha"])
            assert self.git.commit_parents(snapshot) == [first_candidate]
            expected_delta_path = (
                "shared.txt"
                if self.finding_overlaps_conflict
                else "policy.txt"
                if self.finding_changes_d1_file
                else "follow-up.txt"
            )
            assert _git_output(
                self.git_repo,
                "diff",
                "--name-only",
                first_candidate,
                snapshot,
            ) == expected_delta_path
            assert "pending_attempt" not in job
            assert job["modification_attempts"] == 2
            assert job["integration_reprepare_discard_invocation_changes"] is True
            expected_status = (
                "M  shared.txt"
                if self.finding_overlaps_conflict
                else "M  policy.txt"
                if self.finding_changes_d1_file
                else "A  follow-up.txt"
            )
            assert _git_output(
                repair_checkout, "status", "--porcelain=v1"
            ) == expected_status
        else:
            first_candidate = str(job["superseded_candidate_shas"][-1])
            assert self.git.commit_parents(first_candidate) == [
                self.run_head,
                self.default_head,
            ]
            if self.publication_default_drift:
                first_publication = str(job["superseded_publication_shas"][-1])
                assert self.git.commit_parents(first_publication) == [
                    self.run_head,
                    self.default_head,
                ]
                assert self.git.resolve(f"{first_publication}^{{tree}}") == (
                    self.git.resolve(f"{first_candidate}^{{tree}}")
                )
                assert self.git.checkout_head(repair_checkout) == first_publication
        assert repair_checkout.exists()

    def assert_thread_and_worktree_reused(
        self, prior_result: dict[str, Any], resumed: dict[str, Any]
    ) -> None:
        prior_run = prior_result["run_acceptance"]
        prior_job = prior_run["repair_job"]
        assert resumed["run_acceptance"]["repair_generation"] == (
            prior_run["repair_generation"]
        )
        if not self.clean_default_drift:
            assert self.agents.development_requests[-1]["thread_id"] == (
                prior_job["development_thread_id"]
            )
            assert Path(str(self.agents.development_requests[-1]["checkout"])) == (
                Path(str(prior_job["repair_checkout"]))
            )

    def assert_completed(self, result: dict[str, Any]) -> None:
        assert result["status"] == "run_publication_pending", result
        run = result["run_acceptance"]
        candidate = str(run["candidate_sha"])
        publication = str(run["publication_sha"])
        integrated = str(run["integrated_sha"])
        expected_attempts = (
            3
            if self.candidate_finding_default_drift
            else 2
            if self.candidate_requires_follow_up
            or (self.default_drift and not self.clean_default_drift)
            else 1
        )
        if self.staged_interruption:
            assert len(self.agents.development_requests) == 1
            assert len(self.agents.review_requests) == 0
        else:
            assert len(self.agents.development_requests) == expected_attempts
            expected_reviews = (
                1
                if self.development_default_drift
                else 2
                if self.default_drift
                else expected_attempts
            )
            assert len(self.agents.review_requests) == expected_reviews
        expected_code_attempts = (
            3
            if self.candidate_finding_default_drift
            else 1
            if self.development_default_drift or self.staged_interruption
            else expected_attempts
        )
        assert run["repair_cycle"]["code_modification_attempts"] == (
            expected_code_attempts
        )
        accepted_default = (
            self.advanced_default_heads[-1]
            if self.advanced_default_heads
            else self.default_head
        )
        assert self.git.commit_parents(candidate) == [self.run_head, accepted_default]
        assert self.git.commit_parents(publication) == [
            self.run_head,
            accepted_default,
        ]
        assert self.git.commit_parents(integrated) == [self.run_head, publication]
        assert self.git.resolve(f"{candidate}^{{tree}}") == self.git.resolve(
            f"{integrated}^{{tree}}"
        )
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", accepted_default, integrated],
            cwd=self.git_repo,
            check=False,
        ).returncode == 0
        assert self.git.expected_merge_tree(
            default_head_sha=accepted_default,
            run_head_sha=integrated,
        ) == self.git.resolve(f"{candidate}^{{tree}}")
        if self.candidate_finding_default_drift and not (
            self.finding_changes_d1_file or self.finding_overlaps_conflict
        ):
            assert subprocess.run(
                ["git", "cat-file", "-e", f"{candidate}:follow-up.txt"],
                cwd=self.git_repo,
                check=False,
                capture_output=True,
            ).returncode == 0
            assert subprocess.run(
                ["git", "show", f"{candidate}:follow-up.txt"],
                cwd=self.git_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout == "candidate finding repaired\n"
        if self.finding_changes_d1_file:
            expected_policy = (
                "candidate finding repaired policy after default drift\n"
                if self.clean_replay_conflict
                else "candidate finding repaired policy\n"
            )
            assert subprocess.run(
                ["git", "show", f"{candidate}:policy.txt"],
                cwd=self.git_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout == expected_policy
        if self.policy_reversal:
            assert subprocess.run(
                ["git", "cat-file", "-e", f"{candidate}:policy.txt"],
                cwd=self.git_repo,
                check=False,
                capture_output=True,
            ).returncode != 0
        assert not (
            self.states.root
            / "worktrees"
            / str(self.state["run_id"])
            / "run-repair"
        ).exists()


class PublicationDefaultDriftPublisher(FixtureGitHubPublisher):
    def __init__(
        self,
        fixture: Path,
        git: GitRepository,
        scenario: MergeResolutionFixture,
    ) -> None:
        super().__init__(fixture, git)
        self.scenario = scenario

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None:
        super().publish_branch(
            branch,
            head_sha,
            expected_remote_sha=expected_remote_sha,
        )
        if not self.scenario.advanced_default_heads:
            self.scenario.advance_default()

def _git_output(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
