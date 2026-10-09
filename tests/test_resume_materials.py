"""恢复材料检查只读且限定于本次工作的受管现场。"""
from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.resume_materials import require_resume_checkout
from agent_run.state_errors import MissingRunMaterialsError


@pytest.mark.parametrize("kind", ["ticket", "parent", "run_repair"])
@pytest.mark.parametrize("commit_available", [False, True])
@pytest.mark.parametrize("pending_with_candidate", [False, True])
def test_missing_scene_reports_exact_work_without_recreating_it(
    git_repo: Path, tmp_path: Path, kind: str, commit_available: bool,
    pending_with_candidate: bool,
) -> None:
    git = GitRepository(git_repo)
    head = git.resolve("HEAD") if commit_available else "0" * 40
    job: dict[str, Any] = {
        "phase": "developing", "managed_checkout_head": head,
        "ticket_number": 2, "ticket_branch": "agent-run/ticket-2",
        "parent_branch": "agent-run/parent", "repair_branch": "agent-run/run-repair",
    }
    if pending_with_candidate:
        job.update(candidate_sha=git.resolve("HEAD"), pending_attempt=2,
                   pending_semantic_attempt={"role": "development", "status": "pending"})
    state: dict[str, Any] = {"run_id": "original", "status": "execution_failed"}
    if kind == "ticket":
        state.update(active_ticket_job=job, ticket_jobs={"2": job})
    elif kind == "parent":
        state["parent_job"] = job
    else:
        state["run_acceptance"] = {"phase": "repairing", "repair_job": job}
    before = deepcopy(state)
    root = tmp_path / "state"
    target = 2 if kind == "ticket" else None
    with pytest.raises(MissingRunMaterialsError) as caught:
        require_resume_checkout(state, root, git, ticket_number=target)
    diagnostic = str(caught.value)
    name = "ticket-2" if kind == "ticket" else "run-repair" if kind == "run_repair" else "parent"
    assert str(root / "worktrees" / "original" / name) in diagnostic
    assert head in diagnostic
    assert job[{"ticket": "ticket_branch", "parent": "parent_branch", "run_repair": "repair_branch"}[kind]] in diagnostic
    assert ("可取得" if commit_available else "提交对象也缺失") in diagnostic
    assert "agent-run resume" in diagnostic
    assert state == before
    assert not root.exists()


def test_resume_does_not_require_completed_or_unselected_ticket_scenes(
    git_repo: Path, tmp_path: Path,
) -> None:
    git = GitRepository(git_repo)
    missing = {"ticket_number": 3, "phase": "blocked", "blocked_reason": "agent_requires_human",
               "managed_checkout_head": git.resolve("HEAD"), "ticket_branch": "agent-run/ticket-3"}
    selected = {"ticket_number": 2, "phase": "completed", "candidate_sha": git.resolve("HEAD")}
    state = {"run_id": "original", "status": "active", "ticket_jobs": {"2": selected, "3": missing}}
    before = deepcopy(state)
    require_resume_checkout(state, tmp_path / "state", git, ticket_number=2)
    assert state == before


def test_global_ticket_resume_checks_later_runnable_scene_but_not_local_pause(
    git_repo: Path, tmp_path: Path,
) -> None:
    git = GitRepository(git_repo)
    head = git.resolve("HEAD")
    root = tmp_path / "state"
    checkout = root / "worktrees" / "original" / "ticket-2"
    git.prepare_ticket_checkout(branch="agent-run/ticket-2", base_sha=head, checkout=checkout)
    jobs = {str(number): {"ticket_number": number, "phase": "developing",
                         "managed_checkout_head": head, "ticket_branch": f"agent-run/ticket-{number}"}
            for number in (2, 3, 4)}
    jobs["4"].update(phase="blocked", blocked_reason="agent_requires_human")
    state = {"run_id": "original", "status": "execution_failed", "frontier": [2, 3, 4],
             "ticket_jobs": jobs, "ticket_graph": {"tickets": {str(n): {} for n in (2, 3, 4)}}}
    before = deepcopy(state)
    with pytest.raises(MissingRunMaterialsError, match="ticket:3"):
        require_resume_checkout(state, root, git)
    git.prepare_ticket_checkout(branch="agent-run/ticket-3", base_sha=head,
                                checkout=checkout.with_name("ticket-3"))
    require_resume_checkout(state, root, git)
    assert state == before
    assert not checkout.with_name("ticket-4").exists()


@pytest.mark.parametrize("phase", ["developing", "committing_candidate"])
@pytest.mark.parametrize("kind", ["parent", "run_parent", "run_parent_saved_result"])
def test_rebuilt_checkout_on_advanced_branch_is_rejected_without_erasing_files(
    git_repo: Path, tmp_path: Path, phase: str, kind: str,
) -> None:
    git = GitRepository(git_repo)
    git.ensure_checkout_identity()
    head = git.resolve("HEAD")
    root = tmp_path / "state"
    name = "parent" if kind == "parent" else "run-integration"
    checkout = root / "worktrees" / "original" / name
    branch = "agent-run/parent"
    git.prepare_ticket_checkout(branch=branch, base_sha=head, checkout=checkout)
    (checkout / "advanced.txt").write_text("manual branch advance\n")
    subprocess.run(["git", "add", "advanced.txt"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "manual reconstruction on different head"],
                   cwd=checkout, check=True, capture_output=True)
    (checkout / "recovered.txt").write_text("trusted recovered content\n")
    advanced_head = git.checkout_head(checkout)
    state = {"run_id": "original", "status": "execution_failed", "parent_job": {
        "phase": phase, "parent_branch": branch, "managed_checkout_head": head,
        "pending_attempt": 1, "candidate_commit_intent": {"expected_head": head, "token": "original-intent"},
        "pending_semantic_attempt": {"role": "development", "status": "pending"}}}
    if kind != "parent":
        job = state.pop("parent_job")
        job["run_integration_conflict"] = {
            "stage": "git_check", "resolved_stages": [], "head_sha": head, "merge_head_sha": None,
        }
        if kind == "run_parent_saved_result":
            job.pop("pending_semantic_attempt")
            job["run_integration_conflict"]["development_summary"] = "saved Development result"
        state.update(run_acceptance=job, run_branch=branch)
    before = deepcopy(state)
    with pytest.raises(MissingRunMaterialsError, match="HEAD 不符") as caught:
        require_resume_checkout(state, root, git)
    assert head in str(caught.value)
    assert state == before
    assert git.checkout_head(checkout) == advanced_head
    assert (checkout / "recovered.txt").read_text() == "trusted recovered content\n"


@pytest.mark.parametrize("kind", ["ticket", "parent", "run_repair"])
@pytest.mark.parametrize("continuation", ["reviewing", "publishing", "accepted", "repairing"])
def test_committed_continuation_can_rebuild_legitimately_cleaned_checkout(
    git_repo: Path, tmp_path: Path, kind: str, continuation: str,
) -> None:
    git = GitRepository(git_repo)
    head = git.resolve("HEAD")
    job = {"phase": "blocked", "human_blocker_phase": continuation,
           "candidate_sha": head, "managed_checkout_head": head, "ticket_number": 2,
           "ticket_branch": "agent-run/ticket-2", "parent_branch": "agent-run/parent",
           "repair_branch": "agent-run/run-repair"}
    state = {"run_id": "original", "status": "ready_for_human"}
    if kind == "ticket":
        state["ticket_jobs"] = {"2": job}
    elif kind == "parent":
        state["parent_job"] = job
    else:
        state["run_acceptance"] = {"phase": "repairing", "repair_job": job}
    root = tmp_path / "state"
    before = deepcopy(state)
    require_resume_checkout(state, root, git, ticket_number=2 if kind == "ticket" else None)
    assert state == before
    assert not root.exists()  # Admission is read-only; the lane rebuilds and revalidates.


@pytest.mark.parametrize("kind", ["parent", "run_repair", "run_parent"])
@pytest.mark.parametrize("stage", ["source", "target", "git_check", "resolved_followup", "missing_checkpoint"])
def test_parent_snapshot_commit_recovery_uses_its_exact_stage_boundary(
    git_repo: Path, tmp_path: Path, kind: str, stage: str,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    source_checkout = tmp_path / "source"
    git.prepare_validation_checkout(head_sha=base, checkout=source_checkout)
    try:
        (source_checkout / "source.txt").write_text("source contribution\n")
        source = git.commit_candidate(source_checkout, ticket_number=3, attempt=1, expected_head=base)
    finally:
        git.remove_worktree(source_checkout)
    root = tmp_path / "state"
    name = {"parent": "parent", "run_repair": "run-repair", "run_parent": "run-integration"}[kind]
    checkout = root / "worktrees" / "original" / name
    branch = f"agent-run/{name}"
    git.prepare_ticket_checkout(branch=branch, base_sha=base, checkout=checkout)
    assert git._run_in(checkout, "merge", "--no-ff", "--no-commit", source).returncode == 0
    message = "chore: resolve Parent integration conflict"
    committed = git.commit_integration_work(checkout, expected_head_sha=base,
                                            expected_merge_sha=source, message=message)
    scene = {"stage": stage, "resolved_stages": [], "head_sha": base, "merge_head_sha": source}
    if stage == "resolved_followup":
        scene.update(stage="source", resolved_stages=["source"], resolution_sha=committed)
        (checkout / "followup.txt").write_text("follow-up contribution\n")
        committed = git.commit_integration_work(checkout, expected_head_sha=committed,
                                                expected_merge_sha=None, message=message)
    if stage == "missing_checkpoint":
        scene.update(stage="source", resolved_stages=["source"])
    scene["development_summary"] = "saved result"
    scene_key = {"parent": "parent_integration_conflict", "run_repair": "run_repair_integration_conflict",
                 "run_parent": "run_integration_conflict"}[kind]
    job = {"phase": "blocked", "execution_resume_phase": "accepted",
           "managed_checkout_head": base, scene_key: scene,
           "pending_semantic_attempt": {"role": "development", "status": "pending"},
           "parent_branch": branch, "repair_branch": branch}
    state = {"run_id": "original", "status": "execution_failed"}
    if kind == "parent":
        state["parent_job"] = job
    elif kind == "run_repair":
        state["run_acceptance"] = {"phase": "repairing", "repair_job": job}
    else:
        job.pop("managed_checkout_head")  # Run integration persists its boundary in the scene.
        state.update(run_acceptance=job, run_branch=branch)
    before = deepcopy(state)
    if stage == "missing_checkpoint":
        with pytest.raises(MissingRunMaterialsError, match="集成提交边界缺损.*resolution_sha"):
            require_resume_checkout(state, root, git)
    else:
        require_resume_checkout(state, root, git)
    assert state == before
    assert git.checkout_head(checkout) == committed
    assert (checkout / "source.txt").read_text() == "source contribution\n"
