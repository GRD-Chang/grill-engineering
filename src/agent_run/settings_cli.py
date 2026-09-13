"""Public defaults and frozen Run configuration views; no execution side effects."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_run.agent_profiles import AgentProfileStore, ProfileOverrides
from agent_run.delivery_policy import policy_snapshot_for_state
from agent_run.user_defaults import UserDefaultsStore


def add_parser(
    commands: Any,
    common_options: Callable[..., None],
    policy_options: Callable[..., None],
    profile_options: Callable[..., None],
) -> None:
    parser = commands.add_parser("settings", help="查看个人默认或 Run 实际配置，编辑个人默认")
    actions = parser.add_subparsers(dest="settings_command", required=True)
    show = actions.add_parser("show", help="只读查询个人默认；--run 查询已有 Run")
    common_options(show)
    show.add_argument("--run", dest="run_id", help="完整 Run ID")
    show.add_argument("--parent", type=int, help="指定 Parent Issue 的 Run 实际配置")
    show.add_argument("--json", action="store_true", dest="as_json")
    configure = actions.add_parser("configure", help="修改个人默认，仅影响新 Run")
    policy_options(configure)
    profile_options(configure)
    configure.add_argument("--json", action="store_true", dest="as_json")


def execute(
    parsed: argparse.Namespace,
    *,
    policy_overrides: Callable[[argparse.Namespace], dict[str, Any]],
    profile_configuration: Callable[[argparse.Namespace], tuple[str | None, ProfileOverrides]],
    load_run: Callable[[argparse.Namespace], dict[str, Any]],
    profile_root: Callable[[argparse.Namespace], Path],
) -> int:
    if parsed.settings_command == "configure":
        preset, overrides = profile_configuration(parsed)
        profile = {key: value for key, value in overrides.items() if value is not None}
        if preset is not None:
            profile["preset"] = preset
        result = UserDefaultsStore().configure(
            policy=policy_overrides(parsed), profile=profile,
        )
    elif parsed.run_id is not None or parsed.parent is not None:
        state = load_run(parsed)
        parsed.run_id = state["run_id"]
        document = AgentProfileStore(profile_root(parsed)).load(parsed.run_id)
        initializing = document is None and _profile_initialization_pending(state)
        if document is None and not initializing:
            raise ValueError("Delivery Run 缺少 Agent Execution Profile")
        result = {
            "result": "settings", "scope": "run", "run_id": parsed.run_id,
            "policy": policy_snapshot_for_state(state),
            "profile": (
                {key: document[key] for key in ("preset", "profiles", "profile_revision")}
                if document is not None else None
            ),
            "source": "initializing" if initializing else "run_snapshot",
            "bindings": document.get("bindings", []) if document is not None else [],
            "message": (
                "任务正在初始化，Agent 运行配置尚未保存；请稍后重新查询。"
                if initializing else
                "显示 Run 保存的策略及有效 Profile；已有 Thread 使用各自不可变 binding。"
            ),
        }
    else:
        if parsed.repo or parsed.state_dir:
            raise ValueError("查询 Run 配置时请同时指定 --run 或 --parent")
        result = UserDefaultsStore().describe()
    if parsed.as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Run 实际配置" if result.get("scope") == "run" else "个人运行默认配置")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _profile_initialization_pending(state: dict[str, Any]) -> bool:
    """Recognize the persisted creation boundary before profile initialization."""
    return (
        state.get("status") == "starting"
        and state.get("base_resolution_pending") is True
        and state.get("currentness_resolution_pending") is True
        and state.get("action_application_receipt") is None
        and not state.get("active_agent_invocation")
        and not state.get("agent_invocation_history")
        and not any(state.get(key) for key in (
            "active_ticket_job", "ticket_jobs", "parent_job", "run_acceptance", "run_publication",
        ))
    )
