"""完成 Ticket 重开属于事实冲突，结构 Revision 可以保持不变。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.object_state import object_store
from agent_run.scope_changes import unsupported_scope_change_identity_is_consistent
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.state_errors import IncompatibleRunStateError
from support.run_scenarios import _completed_run


def test_completed_ticket_reopen_survives_sparse_reload_and_rejects_inaccurate_evidence(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text())
    data["issues"]["2"]["state"] = "OPEN"
    fixture.write_text(json.dumps(data))
    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(state["run_id"])
    change = refreshed["unsupported_scope_change"]
    assert change["observed_graph_revision"] == change["accepted_graph_revision"]
    assert refreshed["active_ticket_job"] is None
    assert refreshed["status"] == "unsupported_scope_change"
    reopened = object_store(StateStore(states.root)).load_current_run(state["run_id"])
    assert reopened is not None
    assert reopened["ticket_jobs"]["2"]["phase"] == "completed"
    assert unsupported_scope_change_identity_is_consistent(reopened)
    require_current_run_state(reopened)

    for damage in ("missing_owner", "unfinished_owner", "wrong_diagnostic", "wrong_revision", "wrong_tickets", "graph_change"):
        forged = deepcopy(reopened)
        changed = forged["unsupported_scope_change"]
        if damage == "missing_owner":
            forged["ticket_jobs"].clear()
        elif damage == "unfinished_owner":
            forged["ticket_jobs"]["2"]["phase"] = "developing"
        elif damage == "wrong_diagnostic":
            forged["diagnostics"][0]["code"] = "unsupported_ticket_graph_change"
        elif damage == "wrong_revision":
            changed["observed_graph_revision"] = "different"
        elif damage == "wrong_tickets":
            changed["graph_change_summary"]["reopened_completed_tickets"] = [3]
        else:
            forged["ticket_jobs"].clear()
            changed["graph_change_summary"] = {
                "summary": "Ticket 图变化：新增 0，移除 0，新增依赖 0，移除依赖 0。",
                "added_tickets": [], "removed_tickets": [],
                "added_dependencies": [], "removed_dependencies": [],
            }
            forged["diagnostics"] = [{
                "code": "unsupported_ticket_graph_change",
                "message": "Ticket 集合或 blockedBy 依赖已变化；当前 MVP 不支持吸收或自动重排此范围变化，只能查看状态或放弃当前 Delivery Run",
            }]
        assert not unsupported_scope_change_identity_is_consistent(forged), damage
        with pytest.raises(IncompatibleRunStateError, match="action identity"):
            require_current_run_state(forged)
