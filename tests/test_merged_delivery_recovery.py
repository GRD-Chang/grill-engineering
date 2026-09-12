from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from conftest import write_fixture
from test_delivery import ScriptedAgents, issue


COMMIT_MESSAGE = "fix: 恢复完整交付\n\n问题：合并完成后继续收尾。\n\n证据：保留多段正文与中文引号‘验收’。"


class RecoveryAgents(ScriptedAgents):
    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        result = super().publication(request)
        result["commit_message"] = COMMIT_MESSAGE
        return result


class InterruptedMergePublisher(FixtureGitHubPublisher):
    """Use real Git merge facts and production ref checks at recovery."""

    check_remote_refs = False
    interrupt_merge = True

    def ensure_change_branch(self, **kwargs: Any) -> None:
        if self.check_remote_refs:
            GhGitHubPublisher(str(self.data["repository"]), self.git).ensure_change_branch(
                **kwargs
            )
        else:
            super().ensure_change_branch(**kwargs)

    def ensure_ticket_branch(self, **kwargs: Any) -> None:
        if self.check_remote_refs:
            kwargs.pop("ticket_number")
            self.ensure_change_branch(**kwargs)
        else:
            super().ensure_ticket_branch(**kwargs)

    def squash_merge(self, **kwargs: Any) -> str:
        integrated = super().squash_merge(**kwargs)
        if self.interrupt_merge:
            self.interrupt_merge = False
            raise OSError("lost response after remote merge")
        return integrated

    def mirror_remote(self, remote: Path, *, missing_branch: str | None = None) -> None:
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=self.git.root, check=True, capture_output=True,
        )
        refs = dict(self.data["delivery"]["published_branches"])
        for pull in self.data["delivery"]["pull_requests"]:
            refs[pull["base_branch"]] = self.git.resolve(pull["base_branch"])
        for branch, head in refs.items():
            if branch != missing_branch:
                subprocess.run(
                    ["git", "push", "origin", f"{head}:refs/heads/{branch}"],
                    cwd=self.git.root, check=True, capture_output=True,
                )
        self.check_remote_refs = True


def test_ticket_recovery_reconciles_successful_merge_before_old_base_ref_check(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    agents = RecoveryAgents(states.root / "worktrees" / run_id / "ticket-3")
    publisher = InterruptedMergePublisher(fixture, git)
    engine = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents)

    with pytest.raises(OSError, match="lost response after remote merge"):
        engine.deliver(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["active_ticket_job"]
    assert job["phase"] == "merging"
    assert "integrated_sha" not in job
    assert publisher.live_pull_request(job["pr_number"])["state"] == "MERGED"
    publisher.mirror_remote(tmp_path / "remote.git")
    work_before = tuple(agents.events)
    original_data = json.loads(fixture.read_text())
    original_integrated = original_data["delivery"]["pull_requests"][0]["integrated_sha"]
    # Prepare the accepted/merged delivery once. Each readback case gets the
    # same independent durable snapshot, with no additional Agent work.
    for identity_error in (
        {"head_repository": "foreign/project"}, {"head_ref": "foreign-branch"},
        {"base_repository": "foreign/project"}, {"commit_body": "正文被替换"}, {},
    ):
        states.save_run(run_id, deepcopy(interrupted))
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{state['run_branch']}", original_integrated],
            cwd=git_repo, check=True, capture_output=True,
        )
        data = deepcopy(original_data)
        if "commit_body" in identity_error:
            changed = subprocess.run(
                ["git", "commit-tree", git.resolve(f"{original_integrated}^{{tree}}"),
                 "-p", job["base_sha"]],
                input="fix: 恢复完整交付\n\n正文被替换\n", cwd=git_repo,
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            data["delivery"]["pull_requests"][0]["integrated_sha"] = changed
        else:
            data["delivery"]["pull_requests"][0].update(identity_error)
        fixture.write_text(json.dumps(data))
        publisher = InterruptedMergePublisher(fixture, git)
        publisher.check_remote_refs = True
        engine = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents)

        recovered = engine.deliver(run_id)

        require_current_run_state(states.load_current_run(run_id))
        assert tuple(agents.events) == work_before
        if identity_error:
            assert recovered["status"] == "blocked", identity_error
            assert recovered["diagnostics"][0]["code"] == "merged_result_mismatch"
            assert json.loads(fixture.read_text())["delivery"]["closed_issues"] == []
        else:
            assert recovered["status"] == "ticket_completed"
            assert json.loads(fixture.read_text())["delivery"]["closed_issues"] == [3]
            assert git.commit_parents(recovered["active_ticket_job"]["integrated_sha"]) == [
                job["base_sha"]
            ]



def test_run_repair_recovers_merge_without_a_saved_integrated_sha(
    git_repo: Path, tmp_path: Path,
) -> None:
    from agent_run.run_acceptance import RunAcceptanceEngine
    from run_acceptance_test_support import ScriptedRunAgents, _completed_run

    state, states, git = _completed_run(git_repo)
    run_id = str(state["run_id"])
    publisher = InterruptedMergePublisher(git_repo / "github.json", git)
    class RepairAgents(ScriptedRunAgents):
        def publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = super().publication(request)
            result["commit_message"] = COMMIT_MESSAGE
            return result

    agents = RepairAgents()
    engine = RunAcceptanceEngine(git=git, states=states, github=publisher, agents=agents)

    with pytest.raises(OSError, match="lost response after remote merge"):
        engine.accept(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["run_acceptance"]["repair_job"]
    assert job["phase"] == "merging"
    assert "integrated_sha" not in job
    publisher.mirror_remote(tmp_path / "remote.git")
    work_before = (len(agents.development_requests), len(agents.review_requests))

    recovered = engine.accept(run_id)

    assert recovered["status"] == "run_publication_pending"
    require_current_run_state(states.load_current_run(run_id))
    assert (len(agents.development_requests), len(agents.review_requests)) == work_before
    assert len(recovered["run_acceptance"]["completed_repair_jobs"]) == 1


def test_parent_closeout_recovers_with_deleted_source_ref(
    git_repo: Path, tmp_path: Path,
) -> None:
    from agent_run.parent_delivery import ParentDeliveryEngine

    fixture = write_fixture(
        git_repo / "github.json", issues={},
        delivery={"crash_after_close_parent_issue_once": True},
    )
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    publisher = InterruptedMergePublisher(fixture, git)
    agents = RecoveryAgents(states.root / "worktrees" / run_id / "parent")
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    assert engine.deliver(run_id)["status"] == "parent_approval_pending"
    with pytest.raises(OSError, match="lost response"):
        engine.approve(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["parent_job"]
    assert job["phase"] == "merging"
    publisher.mirror_remote(tmp_path / "remote.git", missing_branch=job["parent_branch"])
    work_before = tuple(agents.events)

    recovered = engine.recover_closeout(run_id)

    assert recovered["status"] == "completed"
    require_current_run_state(states.load_current_run(run_id))
    assert tuple(agents.events) == work_before
    assert recovered["parent_job"]["integrated_sha"] == job["integrated_sha"]
    assert json.loads(fixture.read_text())["delivery"]["closed_issues"] == [1]


def test_parent_revision_change_after_merge_preserves_facts_and_pauses(
    git_repo: Path,
) -> None:
    from agent_run.parent_delivery import ParentDeliveryEngine
    from agent_run.run_driver import DirectRunOperations

    fixture = write_fixture(
        git_repo / "github.json", issues={},
        delivery={"crash_after_close_parent_issue_once": True},
    )
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    state, _ = controller.start(1)
    run_id = str(state["run_id"])
    agents = RecoveryAgents(states.root / "worktrees" / run_id / "parent")
    publisher = FixtureGitHubPublisher(fixture, git)
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    assert engine.deliver(run_id)["status"] == "parent_approval_pending"
    with pytest.raises(OSError, match="lost response"):
        engine.approve(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["parent_job"]
    facts = {key: job[key] for key in ("pr_number", "integrated_sha", "merge_intent")}
    work_before = tuple(agents.events)
    data = json.loads(fixture.read_text())
    data["parent"]["body"] += "\nNew requirement after the approved merge."
    fixture.write_text(json.dumps(data))

    recovered = DirectRunOperations(
        controller=controller, states=states, git=git, github_reader=reader,
        publisher_factory=lambda: FixtureGitHubPublisher(fixture, git), agents=agents,
    ).deliver(run_id).state

    assert recovered["status"] == "blocked"
    assert recovered["diagnostics"][0]["code"] == "effective_revision_mismatch"
    assert {key: recovered["parent_job"].get(key) for key in facts} == facts
    assert tuple(agents.events) == work_before
    require_current_run_state(states.load_current_run(run_id))
    assert json.loads(fixture.read_text())["delivery"]["mutations"] == data["delivery"]["mutations"]
