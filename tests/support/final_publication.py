"""CI 读取决策测试的有限合法 Final Publication 前置。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.git import GitRepository
from agent_run.run_publication import RunPublicationEngine
from agent_run.run_publication_approval import prepare_run_approval
from agent_run.state import StateStore
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run
from support.state import persist_run_fixture


def final_publication_context(
    root: Path, *, approved: bool,
) -> tuple[StateStore, GitRepository, dict[str, Any]]:
    """复用历史 completed Ticket；仅运行必要的 Run 验收、文案与批准准备。"""
    state, states, git, publisher = _accepted_run(root)
    published = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert published["status"] == "run_approval_pending"
    state = states.load_current_run(str(state["run_id"]))
    assert state is not None
    if approved:
        prepare_run_approval(state)
    else:
        state["run_publication"]["phase"] = "waiting_checks"
        state.update(status="waiting_checks", terminal_kind="waiting_checks")
    persist_run_fixture(states, str(state["run_id"]), state)
    assert states.load_current_run(str(state["run_id"])) is not None
    return states, git, state
