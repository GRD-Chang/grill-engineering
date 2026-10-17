"""CLI 发布/恢复场景的有限前置：已验收 Ticket，尚未执行 Publication。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agent_profiles import AgentProfileStore
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.state import StateStore
from support.cli import stdout_json
from support.seeding import seed_run
from support.ticket_scenarios import accepted_ticket
from support.workspace import managed_repo, managed_state


def prepare_accepted_ticket(
    repo: Path, fixture: Path, *, ticket_number: int = 3,
) -> dict[str, Any]:
    """保留调用方需求与故障，仅省去本例不负责的首次开发和审核。

    Controller 真实创建 Run、Profile、Locator 及明确 idle ownership；
    Candidate 与审核累计事实复用 ticket_scenarios 的有限构造。该准备
    通过真实 Publisher 准备正常开发前已有的 Run/Ticket base refs，
    不发布 Candidate，不调用 Publication、不补造 Invocation/Semantic
    Attempt，也不创建 PR、Required Checks、等待历史或人工授权。
    后续业务动作必须由调用方的真实 CLI 执行。
    """
    assert ticket_number in {2, 3}
    seeded = seed_run(repo, fixture, idle_control=True)
    assert seeded.returncode == 0, seeded.stderr
    identity = stdout_json(seeded)
    assert identity["result"] == "started"
    run_id = str(identity["run_id"])
    states = StateStore(managed_state(repo))
    prepared, _, _ = accepted_ticket(
        managed_repo(repo), fixture=fixture, states=states, ticket_number=ticket_number,
    )
    assert prepared["run_id"] == run_id
    # 历史线程随本轮首次固定的真实 Profile 保存归属。Publication 继续
    # 复用 Development Thread 及其 model/effort，不借 fresh 标志绕开
    # Thread 合同；这里没有生成任何执行事件或完成的 Attempt。
    profiles = AgentProfileStore(states.root)
    job = prepared["active_ticket_job"]
    base = str(job["base_sha"])
    FixtureGitHubPublisher(fixture, GitRepository(managed_repo(repo))).ensure_ticket_branch(
        ticket_number=ticket_number, branch=str(job["ticket_branch"]),
        base_branch=str(prepared["run_branch"]), expected_base_sha=base,
        expected_remote_sha=base, recovery_remote_sha=base,
    )
    profiles.bind_thread(run_id, role="development", thread_id=job["development_thread_id"])
    for reviewer in job["reviewer_thread_ids"]:
        profiles.bind_thread(run_id, role="review", thread_id=reviewer)
    loaded = StateStore(states.root).load_current_run(run_id)
    assert loaded is not None
    assert loaded["active_ticket_job"]["phase"] == "accepted"
    return loaded
