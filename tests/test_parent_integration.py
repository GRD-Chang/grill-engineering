"""Parent snapshot integration against a local bare remote."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.change_delivery import ChangeDeliveryEngine, ChangeJobContract
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitRepository
from agent_run.github_publish import GhGitHubPublisher
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    prepare_parent_snapshot,
)
from agent_run.parent_only_integration import prepare_parent_integration
from agent_run.review_budget import new_budget
from support.cli import load_only_run_state, run_internal_stage, stdout_json
from support.cli_payloads import parent_publication, passing_acceptance
from support.seeding import seed_run, write_fixture
from support.workspace import managed_repo


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, name: str, contents: str) -> str:
    (root / name).write_text(contents, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def _setup(git_repo: Path, tmp_path: Path) -> tuple[Path, GitRepository, GhGitHubPublisher, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "push", "origin", f"{base}:refs/heads/parent")
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Collaborator")
    _git(collaborator, "config", "user.email", "collaborator@example.invalid")
    git = GitRepository(git_repo)
    return collaborator, git, GhGitHubPublisher("example/project", git), base


def test_parent_snapshot_preserves_local_and_both_remote_heads(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "parent")
    source = _commit(collaborator, "human.txt", "human\n")
    _git(collaborator, "push", "origin", "parent")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "target.txt", "target\n")
    _git(collaborator, "push", "origin", "main")
    policy = DeliveryPolicy().snapshot()
    subject: dict = {"base_sha": base, "policy_snapshot": policy}
    saved: list[int] = []

    result = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=candidate,
        policy_snapshot=policy, record_key="parent_integration",
        save=lambda: saved.append(1),
    )

    assert result.status == "clean"
    assert result.record is not None
    assert (result.record["source_sha"], result.record["target_sha"]) == (source, target)
    assert all(git.is_ancestor(sha, result.record["head_sha"]) for sha in (candidate, source, target))
    assert subject["integration_resync"]["used"] == 0
    assert saved
    unchanged = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main",
        candidate_sha=result.record["head_sha"], policy_snapshot=policy,
        record_key="parent_integration", save=lambda: saved.append(1),
    )
    assert unchanged.record == result.record
    assert subject.get("parent_integration_history") is None
    _git(git_repo, "push", "origin", f"{result.record['head_sha']}:refs/heads/parent")
    subject["published_sha"] = result.record["head_sha"]
    readback = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main",
        candidate_sha=result.record["head_sha"], policy_snapshot=policy,
        record_key="parent_integration", save=lambda: saved.append(1),
        own_source_write=result.record["head_sha"],
    )
    assert readback.record == result.record
    assert subject["integration_resync"]["used"] == 0


@pytest.mark.parametrize("remote_change", ["rollback", "fork", "append"])
def test_published_parent_source_requires_append_history(
    git_repo: Path, tmp_path: Path, remote_change: str
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    policy = DeliveryPolicy().snapshot()
    subject: dict = {"base_sha": base, "policy_snapshot": policy}
    first = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=candidate,
        policy_snapshot=policy, record_key="parent_integration", save=lambda: None,
    )
    assert first.record is not None
    published = str(first.record["head_sha"])
    _git(git_repo, "push", "origin", f"{published}:refs/heads/parent")
    subject["published_sha"] = published
    _git(collaborator, "checkout", "parent")
    if remote_change == "append":
        _git(collaborator, "fetch", "origin", "parent")
        _git(collaborator, "reset", "--hard", published)
        observed = _commit(collaborator, "human.txt", "human\n")
        _git(collaborator, "push", "origin", "parent")
    elif remote_change == "fork":
        observed = _commit(collaborator, "human.txt", "fork\n")
        _git(collaborator, "push", "--force", "origin", "parent")
    else:
        observed = base
        _git(collaborator, "push", "--force", "origin", f"{base}:refs/heads/parent")

    prepare = lambda: prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=published,
        policy_snapshot=policy, record_key="parent_integration", save=lambda: None,
        own_source_write=published,
    )
    if remote_change == "append":
        result = prepare()
        assert result.record is not None
        assert result.record["source_sha"] == observed
        assert git.is_ancestor(published, result.record["head_sha"])
        assert git.is_ancestor(observed, result.record["head_sha"])
    else:
        with pytest.raises(ParentHistoryRewritten) as error:
            prepare()
        assert error.value.previous == published
        assert error.value.observed == observed
        assert github.live_branch_heads("parent", "main")[0] == observed


def test_parent_conflict_resolution_keeps_both_histories(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "README.md", "agent\n")
    _git(collaborator, "checkout", "parent")
    source = _commit(collaborator, "README.md", "human\n")
    _git(collaborator, "push", "origin", "parent")
    policy = DeliveryPolicy().snapshot()
    subject: dict = {"base_sha": base, "policy_snapshot": policy}

    pending = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=candidate,
        policy_snapshot=policy, record_key="parent_integration", save=lambda: None,
    )
    assert pending.status == "conflict"
    assert pending.conflict is not None
    assert pending.conflict["paths"] == ["README.md"]
    _git(collaborator, "checkout", "main")
    later_target = _commit(collaborator, "later.txt", "later\n")
    _git(collaborator, "push", "origin", "main")
    resumed = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=candidate,
        policy_snapshot=policy, record_key="parent_integration", save=lambda: None,
    )
    assert resumed.conflict == pending.conflict
    (git_repo / "README.md").write_text("agent and human\n", encoding="utf-8")
    done = finish_parent_snapshot_conflict(
        git=git, subject=subject, checkout=git_repo,
        record_key="parent_integration", save=lambda: None,
    )
    assert done.status == "clean"
    assert done.record is not None
    assert done.record["conflict_paths"] == ["README.md"]
    assert git.is_ancestor(candidate, done.record["head_sha"])
    assert git.is_ancestor(source, done.record["head_sha"])
    assert not git.is_ancestor(later_target, done.record["head_sha"])


def test_parent_only_new_combination_gets_separate_review_window(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "target.txt", "target\n")
    _git(collaborator, "push", "origin", "main")
    policy = DeliveryPolicy().snapshot()
    demand_record = {"reviewed_base_sha": base, "reviewed_candidate_sha": candidate}
    job = {
        "base_sha": base,
        "candidate_sha": candidate,
        "phase": "accepted",
        "policy_snapshot": policy,
        "acceptance_record": demand_record,
        "acceptance_artifact": {"e2e": "pass"},
        "review_budget": new_budget(development_attempts=1, reviewer_invocations=1),
        "review_budget_history": [],
        "modification_attempts": 1,
        "validation_attempts": 1,
    }
    state = {"policy_snapshot": policy}
    stage = SimpleNamespace(
        git=git, github=github,
        contract=SimpleNamespace(branch="parent", base_branch="main"),
        save=lambda _state: None,
    )

    assert prepare_parent_integration(stage, state, job, git_repo) is False
    assert job["phase"] == "candidate"
    assert job["base_sha"] == target
    assert job["candidate_sha"] == job["parent_integration"]["head_sha"]
    assert job["previous_parent_acceptance_record"] == demand_record
    assert job["review_budget"]["window"] == 2
    assert job["review_budget"]["reviewer_invocations"] == 0
    assert job["review_budget_history"][0]["review_budget"]["reviewer_invocations"] == 1


def test_parent_ci_candidate_integrates_moving_default_before_its_review(
    git_repo: Path, tmp_path: Path
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    _git(collaborator, "checkout", "main")
    _commit(collaborator, "first.txt", "first\n")
    _git(collaborator, "push", "origin", "main")
    policy = DeliveryPolicy().snapshot()
    job = {
        "base_sha": base, "candidate_sha": candidate, "phase": "accepted",
        "policy_snapshot": policy,
        "acceptance_record": {"reviewed_candidate_sha": candidate},
        "review_budget": new_budget(development_attempts=1, reviewer_invocations=1),
        "review_budget_history": [],
        "modification_attempts": 1, "validation_attempts": 1,
    }
    state = {"policy_snapshot": policy}
    stage = SimpleNamespace(
        git=git, github=github,
        contract=SimpleNamespace(branch="parent", base_branch="main"),
        save=lambda _state: None,
    )
    assert prepare_parent_integration(stage, state, job, git_repo) is False
    assert job["parent_integration_stage"] is True
    assert prepare_parent_integration(stage, state, job, git_repo) is True
    first_window = job["review_budget"]["window"]
    first_head = job["candidate_sha"]
    ci_candidate = _commit(git_repo, "ci-fix.txt", "fixed\n")
    assert git.is_ancestor(first_head, ci_candidate)
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "later.txt", "later\n")
    _git(collaborator, "push", "origin", "main")
    job.update({
        "candidate_sha": ci_candidate, "phase": "candidate", "attempt_kind": "ci_repair",
        "acceptance_record": {"reviewed_candidate_sha": first_head},
    })
    ci_budget = {"rounds_used": 1, "repairs_used": 1}
    job["ci_recovery"] = ci_budget

    assert prepare_parent_integration(stage, state, job, git_repo) is False
    assert job["phase"] == "candidate"
    assert job["review_budget"]["window"] == first_window + 1
    assert job["integration_resync"]["used"] == 1
    assert job["ci_recovery"] == ci_budget
    record = job["parent_integration"]
    assert job["candidate_sha"] == record["head_sha"]
    assert job["base_sha"] == target
    assert all(git.is_ancestor(sha, record["head_sha"]) for sha in (ci_candidate, target))
    assert prepare_parent_integration(stage, state, job, git_repo) is True
    assert job["review_budget"]["window"] == first_window + 1


def test_parent_ci_candidate_after_unchanged_first_integration_reviews_new_base(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "agent.txt", "agent\n")
    policy = DeliveryPolicy().snapshot()
    job = {
        "base_sha": base, "candidate_sha": candidate, "phase": "accepted",
        "policy_snapshot": policy,
        "acceptance_record": {"reviewed_candidate_sha": candidate},
        # Demand acceptance may use more reviews than the integration window.
        "review_budget": new_budget(development_attempts=6, reviewer_invocations=6),
        "review_budget_history": [],
        "modification_attempts": 6, "validation_attempts": 6,
    }
    state = {"policy_snapshot": policy}
    engine = ChangeDeliveryEngine(
        git=git, github=github, agents=None,
        contract=ChangeJobContract(label="parent-only", branch="parent", base_branch="main"),
        adapter=None, publisher=None,
        state_store=SimpleNamespace(save=lambda value: value),
    )

    assert prepare_parent_integration(engine, state, job, git_repo) is True
    assert "parent_integration_stage" not in job
    assert job["review_budget"]["window"] == 1

    repaired_candidate = _commit(git_repo, "ci-fix.txt", "fixed\n")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "later.txt", "later\n")
    _git(collaborator, "push", "origin", "main")
    job.update({
        "candidate_sha": repaired_candidate, "phase": "candidate",
        "attempt_kind": "ci_repair", "repair_source": "required_checks",
        "ci_recovery": new_ci_recovery_budget(),
    })
    job["ci_recovery"].update(rounds_used=1, repairs_used=1, failure_head=candidate,
                              repair_pending=True, review_required=True)
    reviewed: list[tuple[str, str]] = []

    def capture_review(_state: dict, current: dict, _checkout: Path) -> None:
        reviewed.append((current["base_sha"], current["candidate_sha"]))
        current["phase"] = "blocked"

    monkeypatch.setattr(engine, "_review", capture_review)
    engine.run(state, job, git_repo)

    assert reviewed == [(target, job["parent_integration"]["head_sha"])]
    assert job["parent_integration_stage"] is True
    assert job["review_budget"]["window"] == 2
    assert job["review_budget_history"][0]["review_budget"]["reviewer_invocations"] == 6
    assert all(git.is_ancestor(sha, reviewed[0][1]) for sha in (repaired_candidate, target))


def test_second_conflict_stage_requires_its_own_resolution(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborator, git, github, base = _setup(git_repo, tmp_path)
    _git(git_repo, "checkout", "-b", "parent")
    candidate = _commit(git_repo, "README.md", "agent\n")
    _git(collaborator, "checkout", "parent")
    source = _commit(collaborator, "README.md", "human\n")
    _git(collaborator, "push", "origin", "parent")
    _git(collaborator, "checkout", "main")
    target = _commit(collaborator, "README.md", "target\n")
    _git(collaborator, "push", "origin", "main")
    policy = DeliveryPolicy().snapshot()
    subject: dict = {"base_sha": base, "policy_snapshot": policy}
    first = prepare_parent_snapshot(
        git=git, github=github, subject=subject, checkout=git_repo,
        source_branch="parent", target_branch="main", candidate_sha=candidate,
        policy_snapshot=policy, record_key="parent_integration", save=lambda: None,
    )
    assert first.conflict is not None and first.conflict["stage"] == "source"
    (git_repo / "README.md").write_text("agent and human\n", encoding="utf-8")
    subject["parent_integration_conflict"]["development_summary"] = "resolved source"
    second = finish_parent_snapshot_conflict(
        git=git, subject=subject, checkout=git_repo,
        record_key="parent_integration", save=lambda: None,
    )
    assert second.conflict is not None and second.conflict["stage"] == "target"
    assert "development_summary" not in second.conflict
    assert second.conflict["attempts"] == 0
    (git_repo / "README.md").write_text("all three\n", encoding="utf-8")
    def crash_after_resolution(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash")

    with monkeypatch.context() as patcher:
        patcher.setattr(git, "integrate_remote_heads", crash_after_resolution)
        with pytest.raises(OSError, match="crash"):
            finish_parent_snapshot_conflict(
                git=git, subject=subject, checkout=git_repo,
                record_key="parent_integration", save=lambda: None,
            )
    assert subject["parent_integration_conflict"]["resolved_stages"] == ["source", "target"]
    done = finish_parent_snapshot_conflict(
        git=git, subject=subject, checkout=git_repo,
        record_key="parent_integration", save=lambda: None,
    )
    assert done.record is not None
    assert all(git.is_ancestor(sha, done.record["head_sha"]) for sha in (candidate, source, target))


def test_parent_only_delivery_reviews_integrated_remote_changes_once(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    started = load_only_run_state(git_repo)
    managed = managed_repo(git_repo)
    base = str(started["base"]["sha"])
    branch = str(started["parent_branch"])
    collaborator = tmp_path / "collaborator"
    _git(managed, "worktree", "add", "--detach", str(collaborator), base)
    try:
        (collaborator / "human.txt").write_text("source contribution\n", encoding="utf-8")
        _git(collaborator, "add", "human.txt")
        _git(collaborator, "-c", "user.name=Collaborator", "-c", "user.email=c@example.invalid", "commit", "-m", "source update")
        source = _git(collaborator, "rev-parse", "HEAD")
        _git(collaborator, "reset", "--hard", base)
        (collaborator / "target.txt").write_text("target contribution\n", encoding="utf-8")
        _git(collaborator, "add", "target.txt")
        _git(collaborator, "-c", "user.name=Collaborator", "-c", "user.email=c@example.invalid", "commit", "-m", "target update")
        target = _git(collaborator, "rev-parse", "HEAD")
    finally:
        _git(managed, "worktree", "remove", "--force", str(collaborator))
    _git(managed, "update-ref", "refs/heads/main", target, base)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).setdefault("published_branches", {})[branch] = source
    data["delivery"]["published_branches"]["main"] = target
    data["default_head_sha"] = target
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = git_repo / "parent-integration-agents.json"
    agents.write_text(json.dumps({
        "developments": [{
            "expected_thread_id": None, "thread_id": "parent-developer",
            "summary": "Implemented Parent demand.",
            "write_files": {"parent-feature.txt": "done\n"},
        }],
        "reviews": [
            passing_acceptance("demand-reviewer", "demand accepted"),
            passing_acceptance("integrated-reviewer", "combined result accepted"),
        ],
        "publications": [parent_publication()],
    }), encoding="utf-8")

    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    job = load_only_run_state(git_repo)["parent_job"]
    assert job["phase"] == "ready_for_approval"
    assert job["review_budget"]["window"] == 2
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert job["review_budget_history"][0]["review_budget"]["reviewer_invocations"] == 1
    record = job["parent_integration"]
    assert (record["source_sha"], record["target_sha"]) == (source, target)
    assert all(GitRepository(managed).is_ancestor(sha, record["head_sha"]) for sha in (source, target, job["review_budget_history"][0]["candidate_sha"]))
    assert job["acceptance_record"]["reviewed_candidate_sha"] == record["head_sha"]
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1
    repeated = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert repeated.returncode == 0
    same = load_only_run_state(git_repo)["parent_job"]
    assert same["review_budget"]["reviewer_invocations"] == 1
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1
