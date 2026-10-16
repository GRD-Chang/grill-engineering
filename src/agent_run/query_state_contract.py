"""核验已读取的当前查询事实，保持历史读取范围独立。"""
from __future__ import annotations

from typing import Any

from agent_run.operator_gate import active_ticket_gate_mirror_is_consistent, operator_gate_identity_is_consistent
from agent_run.state_contract import (
    IncompatibleRunStateError,
    _require_active_invocation_identity,
    _require_base,
    _require_human_blocker_containers,
    _require_invocation,
    _require_parent,
    _require_run_gate_exclusivity,
    _require_ticket_graph,
    raise_materials_error,
    require_currentness_boundaries,
    require_query_state_current,
)


def require_loaded_query_state(state: dict[str, Any]) -> None:
    """Only current and explicitly selected Invocation facts are in scope.

    This validator grants no mutation identity and never materializes a Job
    archive. Complete history continues through require_current_run_state.
    """
    try:
        require_query_state_current(state)
        _require_parent(state["parent"])
        if "base" in state:
            _require_base(state["base"])
        if "ticket_graph" in state:
            _require_ticket_graph(state["ticket_graph"])
        if "accepted_parent_spec_revision" in state or "accepted_ticket_graph_revision" in state:
            require_currentness_boundaries(state)
        _require_run_gate_exclusivity(state)
        if not operator_gate_identity_is_consistent(state):
            raise IncompatibleRunStateError("run state has an inconsistent Operator Gate action identity")
        if not active_ticket_gate_mirror_is_consistent(state):
            raise IncompatibleRunStateError("run state has an inconsistent active Ticket gate mirror")
        _require_human_blocker_containers(state)
        run_id = str(state["run_id"])
        active = state.get("active_agent_invocation")
        if active is not None:
            if not isinstance(active, dict):
                raise IncompatibleRunStateError("run state has an invalid active Agent Invocation")
            _require_invocation(active, "active_agent_invocation", run_id)
            if active["status"] != "completed":
                _require_active_invocation_identity(state, active)
        for key, invocation in state.get("ticket_agent_invocations", {}).items():
            if not isinstance(invocation, dict) or invocation.get("work_subject") != f"ticket:{key}":
                raise IncompatibleRunStateError("run state has an invalid Ticket Agent Invocation owner")
            _require_invocation(invocation, f"ticket_agent_invocations[{key}]", run_id)
            if invocation["status"] != "completed":
                _require_active_invocation_identity(state, invocation)
        for index, invocation in enumerate(state.get("agent_invocation_history", [])):
            if not isinstance(invocation, dict):
                raise IncompatibleRunStateError("run state has an invalid Invocation history entry")
            _require_invocation(invocation, f"agent_invocation_history[{index}]", run_id)
    except (ValueError, KeyError, TypeError) as error:
        raise_materials_error(state, error)
