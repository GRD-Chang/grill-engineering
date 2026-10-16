"""通知测试的有限前置；历史 Ticket 与被测 Run 审核分开准备。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.notifications import Notifications
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import StateStore
from agent_run.state_observation import CurrentRunObserver
from agent_run.user_defaults import UserDefaultsStore, notification_snapshot
from support.published_run import prepare_completed_run
from support.state import persist_run_fixture
from support.workspace import managed_repo, managed_state


def prepare_blocked_run_acceptance(
    repo: Path, agents: Path,
) -> tuple[Path, dict[str, Any]]:
    """从合法已完成 Ticket 起点产生真正的受阻 Reviewer 与通知事实。

    不补造 Invocation、Attempt 或执行段；被测 Run Acceptance、通知查询和
    提交后 observer 均真实执行。完整 Ticket 前序与发送进程的成功/取消
    代表保留在 test_cli_delivery_sends_cards_and_queries_never_send；Ticket
    和 Parent-only 的受阻前序仍由各自的真实 CLI run/resume 用例覆盖。
    """
    fixture, state = prepare_completed_run(repo)
    states = StateStore(managed_state(repo))
    run_id = str(state["run_id"])
    state["notifications"] = notification_snapshot(
        UserDefaultsStore().load().get("notifications"), mode="detailed",
    )
    persist_run_fixture(states, run_id, state)
    git = GitRepository(managed_repo(repo))
    backend = ProfiledAgentBackend(
        FixtureAgentBackend(agents), AgentProfileStore(states.root), run_id=run_id,
    )
    sender = Notifications(
        states.root, state,
        query_state=lambda observation: states.load_notification_run(run_id, observation),
    )
    states.run_saved_observer = CurrentRunObserver(sender.observe)
    try:
        blocked = RunAcceptanceEngine(
            git=git, states=states, agents=backend,
            github=FixtureGitHubPublisher(fixture, git),
            currentness_reader=FixtureGitHubReader(fixture),
            default_head_sha=git.resolve("main"),
        ).accept(run_id)
        assert blocked["status"] == "ready_for_human"
    finally:
        states.run_saved_observer = None
        sender.close()
    durable = states.load_run(run_id)
    assert durable is not None
    return fixture, durable
