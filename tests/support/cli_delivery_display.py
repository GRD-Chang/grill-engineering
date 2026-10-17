"""仅供 test_cli_delivery：准确 PR/ref 后、首次可选分支展示前的有限前置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.object_state import object_store
from agent_run.state import StateStore
from support.cli_payloads import final_run_publication
from support.delivery_publisher import ScriptedPublisher
from support.parent_scenarios import prepare_approval_ready_parent
from support.run_scenarios import _accepted_run
from support.seeding import write_fixture
from support.state import persist_run_fixture
from support.ticket_scenarios import published_ticket
from support.workspace import managed_repo, managed_state


DisplayScope = Literal["parent_only", "ticket", "final_run"]


class ObservedDisplayPublisher(FixtureGitHubPublisher):
    """只观察外部请求前已落盘的事实；展示响应仍使用既有 GitHub fake。"""

    def __init__(
        self, fixture: Path, git: GitRepository, states: StateStore, run_id: str,
    ) -> None:
        super().__init__(fixture, git)
        self.states = states
        self.run_id = run_id
        self.before_display: list[dict[str, Any]] = []

    def link_issue_branch_display(
        self, *, issue_number: int, branch: str, head_sha: str,
    ) -> str:
        current = StateStore(self.states.root).load_current_run(self.run_id)
        assert current is not None
        self.before_display.append(current)
        return super().link_issue_branch_display(
            issue_number=issue_number, branch=branch, head_sha=head_sha,
        )


@dataclass
class DisplayBoundary:
    scope: DisplayScope
    fixture: Path
    state: dict[str, Any]
    states: StateStore
    github: ObservedDisplayPublisher
    issue_number: int
    branch: str
    head_sha: str

    def job(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.scope == "ticket":
            return state["active_ticket_job"]
        return state["parent_job" if self.scope == "parent_only" else "run_publication"]

    def save(self, state: dict[str, Any]) -> None:
        if self.scope == "ticket":
            state["ticket_jobs"]["3"] = state["active_ticket_job"]
        self.states.save_run(str(state["run_id"]), state)


def prepare_display_boundary(
    repo: Path, *, scope: DisplayScope, outcome: str,
) -> DisplayBoundary:
    """准备独立的真实 Git/SQLite 与准确外部 PR，不调用展示或补造 Attempt。

    Parent/Ticket 的开发审核使用已有有限历史场景，Final Run 使用真实
    Run Acceptance。这里不推进与展示无关的开发、CI、批准或收尾；它们
    由调用文件中的完整流程代表覆盖。每例独占全部可变状态。
    """
    if scope == "parent_only":
        fixture = write_fixture(repo / "github.json", issues={})
        state = prepare_approval_ready_parent(repo, fixture)
        states = StateStore(managed_state(repo))
        git = GitRepository(managed_repo(repo))
        job = state["parent_job"]
        # 该历史场景未真正请求展示。只保留本例目标之前的事实：准确的
        # Candidate、审核、Publication、ref 和 PR；不伪造返回或 Attempt。
        for field in (
            "linked_branch_display", "required_checks_evidence",
            "deterministic_integration_record", "merge_intent",
        ):
            job.pop(field, None)
        job["phase"] = "publishing"
        state.update(status="parent_delivery_pending", terminal_kind=None)
        issue_number, branch, head = 1, job["parent_branch"], job["publication_sha"]
    elif scope == "ticket":
        state, states, _ = published_ticket(repo, publisher=ScriptedPublisher(repo))
        fixture = repo / "github.json"
        git = GitRepository(repo)
        github = FixtureGitHubPublisher(fixture, git)
        job = state["active_ticket_job"]
        branch, head, base = job["ticket_branch"], job["publication_sha"], job["base_sha"]
        github.ensure_ticket_branch(
            ticket_number=3, branch=branch, base_branch=state["run_branch"],
            expected_base_sha=base, expected_remote_sha=base, recovery_remote_sha=base,
        )
        github.publish_branch(branch, head, expected_remote_sha=base)
        publication = job["publication"]
        job["pr_number"] = github.ensure_ticket_pr(
            branch=branch, base_branch=state["run_branch"], primary_ticket=3,
            expected_head_sha=head, expected_base_sha=base,
            title=publication["pr_title"], body=publication["pr_body_markdown"],
        )
        issue_number = 3
    else:
        state, states, git, github = _accepted_run(repo, ticket_number=3)
        fixture = repo / "github.json"
        branch = state["run_branch"]
        head = state["run_acceptance"]["acceptance_record"]["reviewed_head_sha"]
        source, base = github.live_branch_heads(branch, state["base"]["branch"])
        if source is None:
            github.ensure_final_run_ref(branch=branch, expected_head_sha=head)
        elif source != head:
            github.publish_branch(branch, head, expected_remote_sha=source)
        publication = final_run_publication()
        pr_number = github.ensure_run_pr(
            branch=branch, base_branch=state["base"]["branch"],
            expected_head_sha=head, expected_base_sha=base,
            title=publication["pr_title"], body=publication["pr_body_markdown"],
        )
        # 准确的已有 PR 可被后续 Publication 读取；不虚构 Publication
        # Agent、已完成 Semantic Attempt 或人工批准。
        state["run_publication"] = {"phase": "pending", "pr_number": pr_number}
        issue_number = 1

    run_id = str(state["run_id"])
    persist_run_fixture(states, run_id, state)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = data["delivery"]
    assert delivery["linked_branch_display_attempts"] == []
    # Parent 历史前置曾登记旧展示数据，但从未执行可选展示请求。
    delivery["linked_branches"] = {}
    delivery["linked_branch_display_outcomes"] = outcome
    fixture.write_text(json.dumps(data), encoding="utf-8")
    # 首次创建已完成；业务动作必须经过现有对象提交与准确基线，不能用
    # StateStore.save_run 覆盖已有 Run，也不能用 fixture 替换承担保存。
    states = object_store(states, parent_scope=scope != "ticket")
    loaded = states.load_current_run(run_id)
    assert loaded is not None
    return DisplayBoundary(
        scope, fixture, loaded, states,
        ObservedDisplayPublisher(fixture, git, states, run_id),
        issue_number, branch, head,
    )
