from __future__ import annotations

from support.run_artifacts import _passing_artifact

import json
from pathlib import Path
import subprocess

import pytest

from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.git import GitError
from agent_run.state_errors import MissingRunMaterialsError
from agent_run.github_fixture import FixtureGitHubPublisher

from support.merge_faults import (
    CandidateSaveCrashGit,
    CleanReprepareCrashGit,
    FindingSnapshotCrashGit,
    FindingReplayCrashGit,
    FindingReplaySaveCrashGit,
)
from support.merge_resolution import (
    MergeResolutionFixture,
    _git_output,
)
from support.workspace import prepare_workspace
from support.seeding import seed_idle_control
from support.cli import run_cli


def test_merge_resolution_creates_exact_two_parent_candidate(git_repo: Path) -> None:
    fixture = MergeResolutionFixture(git_repo, "happy_path")

    result = fixture.accept()

    fixture.assert_completed(result)


def test_merge_resolution_rejects_an_unmodified_conflict_scene(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "unresolved_conflict")

    with pytest.raises(
        GitError, match="unresolved conflict paths were not repaired"
    ):
        fixture.accept()

    persisted = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert persisted is not None
    job = persisted["run_acceptance"]["repair_job"]
    assert job["phase"] == "committing_candidate"
    assert "candidate_sha" not in job
    assert fixture.agents.review_requests == []
    checkout = Path(str(job["repair_checkout"]))
    assert _git_output(checkout, "diff", "--name-only", "--diff-filter=U") == (
        "shared.txt"
    )


def test_merge_resolution_rejects_conflict_markers_staged_without_a_repair(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "staged_unresolved_conflict")

    with pytest.raises(
        GitError, match="unresolved conflict paths were not repaired"
    ):
        fixture.accept()

    persisted = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert persisted is not None
    job = persisted["run_acceptance"]["repair_job"]
    assert job["phase"] == "committing_candidate"
    assert "candidate_sha" not in job
    assert fixture.agents.review_requests == []


def test_merge_resolution_rejects_edited_content_that_retains_conflict_markers(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "edited_unresolved_markers")

    with pytest.raises(GitError, match="retain conflict markers"):
        fixture.accept()

    persisted = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert persisted is not None
    job = persisted["run_acceptance"]["repair_job"]
    assert job["phase"] == "committing_candidate"
    assert "candidate_sha" not in job
    assert fixture.agents.review_requests == []


def test_merge_resolution_accepts_a_deleted_conflict_path(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "delete_conflict_path")

    result = fixture.accept()

    fixture.assert_completed(result)
    candidate = str(result["run_acceptance"]["candidate_sha"])
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{candidate}:shared.txt"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0


def test_merge_resolution_candidate_finding_reuses_repair_cycle(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_finding")

    result = fixture.accept()

    fixture.assert_completed(result)


@pytest.mark.parametrize("tampered_intent", [False, True], ids=["original-intent", "wrong-intent"])
def test_merge_resolution_candidate_save_crash_recovers_append_only_candidate(
    git_repo: Path, tampered_intent: bool,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_save_crash")
    crashing_git = CandidateSaveCrashGit(git_repo)

    with pytest.raises(RuntimeError, match="crash after creating"):
        fixture._engine(
            crashing_git,
            FixtureGitHubPublisher(fixture.fixture, crashing_git),
            fixture.default_head,
        ).accept(str(fixture.state["run_id"]))
    run_id = str(fixture.state["run_id"])
    interrupted = fixture.states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["run_acceptance"]["repair_job"]
    checkout = Path(job["repair_checkout"])
    committed = fixture.git.checkout_head(checkout)
    attempt_id = job["pending_semantic_attempt"]["attempt_id"]
    calls_before = len(fixture.agents.development_requests)
    used = job["review_budget"]["development_attempts"]
    if tampered_intent:
        job["candidate_commit_intent"]["token"] = "unrelated-intent"
        fixture.states.save_run(run_id, interrupted)
        state_file = fixture.states.runs_directory / f"{run_id}.json"
        before = state_file.read_bytes()
        with pytest.raises(MissingRunMaterialsError, match="HEAD 不符"):
            fixture.accept()
        assert state_file.read_bytes() == before
        assert fixture.git.checkout_head(checkout) == committed
        assert len(fixture.agents.development_requests) == calls_before
        assert fixture.agents.review_requests == []
        return
    result = fixture.accept()

    fixture.assert_completed(result)
    assert result["run_acceptance"]["candidate_sha"] == committed
    assert len(fixture.agents.development_requests) == calls_before
    assert result["run_acceptance"]["review_budget"]["development_attempts"] == used
    retired = result["retired_semantic_attempt_owners"][-1]
    attempts = retired["semantic_attempt_history"]
    assert sum(attempt["attempt_id"] == attempt_id for attempt in attempts) == 1


@pytest.mark.parametrize(
    "scenario",
    ["candidate_default_drift", "candidate_default_drift_clean"],
    ids=["conflicting", "clean"],
)
def test_merge_resolution_candidate_validation_default_drift_reprepares_boundary(
    git_repo: Path, scenario: str
) -> None:
    fixture = MergeResolutionFixture(git_repo, scenario)

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_publication_default_drift_reprepares_boundary(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "publication_default_drift")

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


@pytest.mark.parametrize(
    "scenario",
    ["development_default_drift", "development_default_drift_clean"],
    ids=["conflicting", "clean"],
)
def test_merge_resolution_development_default_drift_reprepares_boundary(
    git_repo: Path, scenario: str
) -> None:
    fixture = MergeResolutionFixture(git_repo, scenario)

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_clean_reprepare_candidate_commit_crash_recovers(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "development_default_drift_clean_crash")
    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    repair_checkout = Path(
        str(interrupted["run_acceptance"]["repair_job"]["repair_checkout"])
    )
    crashing_git = CleanReprepareCrashGit(git_repo)

    with pytest.raises(
        RuntimeError, match="crash before creating clean reprepare Candidate"
    ):
        fixture._engine(
            crashing_git,
            FixtureGitHubPublisher(fixture.fixture, crashing_git),
            fixture.advanced_default_heads[-1],
        ).accept(str(fixture.state["run_id"]))
    crashed = fixture.states.load_run(str(fixture.state["run_id"]))
    assert crashed is not None
    crashed_job = crashed["run_acceptance"]["repair_job"]
    assert crashed_job["phase"] == "committing_candidate"
    assert crashed_job["pending_attempt"] == 1
    assert crashed_job["modification_attempts"] == 0
    assert "integration_reprepare_required" not in crashed_job
    assert _git_output(repair_checkout, "rev-parse", "MERGE_HEAD") == (
        fixture.advanced_default_heads[-1]
    )

    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_second_default_drift_rebinds_same_repair_cycle(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "development_double_default_drift")
    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    repair_checkout = Path(
        str(interrupted["run_acceptance"]["repair_job"]["repair_checkout"])
    )

    fixture.advance_default()
    rebound = fixture.resume()
    rebound_job = rebound["run_acceptance"]["repair_job"]
    assert rebound["status"] == "run_acceptance_pending"
    assert rebound_job["phase"] == "repairing"
    assert rebound_job["pending_attempt"] == 1
    assert rebound_job["integration_reprepare_required"] is True
    assert rebound_job["default_base_sha"] == fixture.advanced_default_heads[-1]
    assert _git_output(repair_checkout, "rev-parse", "MERGE_HEAD") == (
        fixture.advanced_default_heads[-2]
    )

    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_candidate_finding_default_drift_preserves_finding_repair(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_finding_default_drift")

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_finding_delta_does_not_revive_reverted_default_content(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(
        git_repo, "candidate_finding_default_drift_policy_reversal"
    )

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_replays_finding_change_to_a_default_added_file(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(
        git_repo, "candidate_finding_default_drift_d1_file"
    )

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_replays_overlapping_finding_as_latest_boundary_conflict(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(
        git_repo, "candidate_finding_default_drift_overlap"
    )

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_clean_latest_scene_surfaces_finding_replay_conflict(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(
        git_repo, "candidate_finding_default_drift_clean_replay_conflict"
    )

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_clean_latest_scene_replaces_stage_zero_for_replay_conflict(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(
        git_repo, "candidate_finding_default_drift_clean_replay_modify_conflict"
    )

    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    resumed = fixture.resume()

    fixture.assert_thread_and_worktree_reused(interrupted, resumed)
    fixture.assert_completed(resumed)


def test_merge_resolution_finding_snapshot_crash_resumes_before_candidate_commit(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_finding_default_drift")
    crashing_git = FindingSnapshotCrashGit(git_repo)

    with pytest.raises(RuntimeError, match="crash after preserving finding repair"):
        fixture._engine(
            crashing_git,
            FixtureGitHubPublisher(fixture.fixture, crashing_git),
            fixture.default_head,
        ).accept(str(fixture.state["run_id"]))
    crashed = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert crashed is not None
    crashed_job = crashed["run_acceptance"]["repair_job"]
    assert crashed_job["phase"] == "committing_candidate"
    assert crashed_job["pending_attempt"] == 2
    assert crashed_job["modification_attempts"] == 1

    rebound = fixture.resume()
    fixture.assert_reprepare_pending(rebound)
    resumed = fixture.resume()

    fixture.assert_completed(resumed)


def test_merge_resolution_finding_replay_crash_resumes_exact_scene(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_finding_default_drift")
    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    crashing_git = FindingReplayCrashGit(git_repo)

    with pytest.raises(RuntimeError, match="crash before replaying finding delta"):
        fixture._engine(
            crashing_git,
            FixtureGitHubPublisher(fixture.fixture, crashing_git),
            fixture.advanced_default_heads[-1],
        ).accept(str(fixture.state["run_id"]))
    crashed = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert crashed is not None
    crashed_job = crashed["run_acceptance"]["repair_job"]
    repair_checkout = Path(str(crashed_job["repair_checkout"]))
    assert crashed_job["integration_reprepare_required"] is True
    assert _git_output(repair_checkout, "rev-parse", "HEAD") == fixture.run_head
    assert _git_output(repair_checkout, "rev-parse", "MERGE_HEAD") == (
        fixture.advanced_default_heads[-1]
    )

    resumed = fixture.resume()

    fixture.assert_completed(resumed)


def test_merge_resolution_finding_replay_save_crash_is_idempotent(
    git_repo: Path,
) -> None:
    fixture = MergeResolutionFixture(git_repo, "candidate_finding_default_drift")
    interrupted = fixture.accept()
    fixture.assert_reprepare_pending(interrupted)
    crashing_git = FindingReplaySaveCrashGit(git_repo)

    with pytest.raises(RuntimeError, match="crash after replaying finding delta"):
        fixture._engine(
            crashing_git,
            FixtureGitHubPublisher(fixture.fixture, crashing_git),
            fixture.advanced_default_heads[-1],
        ).accept(str(fixture.state["run_id"]))

    resumed = fixture.resume()

    fixture.assert_completed(resumed)


def test_merge_resolution_staged_development_interruption_resumes_same_thread(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    fixture = MergeResolutionFixture(repo, "development_staged_interruption", state_root=workspace.state_root)
    seed_idle_control(
        TaskControlStore(fixture.states.root),
        TaskKey(repo, str(fixture.state["repository"]), 1),
        str(fixture.state["run_id"]),
        state_dir=fixture.states.root,
    )

    with pytest.raises(
        RuntimeError, match="controller interrupted staged resolution"
    ):
        fixture.accept()
    interrupted = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert interrupted is not None
    job = interrupted["run_acceptance"]["repair_job"]
    repair_checkout = Path(str(job["repair_checkout"]))
    assert interrupted["status"] == "execution_failed"
    assert job["phase"] == "developing"
    assert job["pending_attempt"] == 1
    assert _git_output(repair_checkout, "rev-parse", "MERGE_HEAD") == (
        fixture.default_head
    )
    assert _git_output(
        repair_checkout, "diff", "--name-only", "--diff-filter=U"
    ) == ""
    resume_agents = fixture.git_repo / "resume-staged-conflict.json"
    resume_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": "integration-repair-developer",
                        "thread_id": "integration-repair-developer",
                        "summary": "Recovered the staged conflict resolution.",
                        "expected_files": {"shared.txt": "resolved\n"},
                    }
                ],
                "publications": [
                    {
                        **fixture.agents.publication({}),
                        "expected_thread_id": "integration-repair-developer",
                        "thread_id": "integration-repair-developer",
                    }
                ],
                "run_reviews": [
                    {
                        "thread_id": "integration-repair-reviewer-1",
                        "artifact": _passing_artifact(),
                    }
                ],
                "run_publications": [fixture.agents.publication({})],
            }
        ),
        encoding="utf-8",
    )

    resumed_cli = run_cli(
        fixture.git_repo,
        fixture.fixture,
        "resume",
        str(fixture.state["run_id"]),
        "--agent-fixture",
        str(resume_agents),
    )

    assert resumed_cli.returncode == 0, resumed_cli.stderr
    result = fixture.states.load_current_run(str(fixture.state["run_id"]))
    assert result is not None
    completed_development = [
        invocation
        for invocation in result["agent_invocation_history"]
        if invocation["role"] == "development"
        and invocation["status"] == "completed"
    ][-1]
    assert completed_development["mode"] == "resume"
    assert completed_development["requested_thread_id"] == (
        "integration-repair-developer"
    )
    assert completed_development["reported_thread_id"] == (
        "integration-repair-developer"
    )
    fixture.assert_completed(result, expected_status="run_approval_pending")
