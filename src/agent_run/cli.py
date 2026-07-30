from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-run",
        description="从 GitHub Parent Spec 启动或恢复本地 Delivery Run",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("start", help="启动或幂等恢复 Delivery Run")
    start.add_argument("parent", type=_positive_integer, help="Parent Spec Issue 编号")
    _add_common_options(start)
    resume = subcommands.add_parser("resume", help="按稳定 Run ID 恢复 Delivery Run")
    resume.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(resume)
    deliver = subcommands.add_parser(
        "deliver", help="交付当前 Active Ticket Job"
    )
    deliver.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(deliver)
    deliver.add_argument(
        "--agent-fixture",
        help=argparse.SUPPRESS,
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    try:
        git = GitRepository.discover(Path.cwd())
        state_root = (
            Path(parsed.state_dir).resolve()
            if parsed.state_dir
            else git.root / ".agent-run"
        )
        github = (
            FixtureGitHubReader(Path(parsed.github_fixture))
            if parsed.github_fixture
            else GhGitHubReader(parsed.repo)
        )
        states = StateStore(state_root)
        controller = Controller(github, git, states)
        if parsed.command == "start":
            state, resumed = controller.start(parsed.parent)
        elif parsed.command == "resume":
            state, resumed = controller.resume(parsed.run_id)
        else:
            existing = states.load_run(parsed.run_id)
            if existing is not None and existing.get("status") == "ticket_completed":
                state = existing
                resumed = True
            else:
                refreshed, _ = controller.resume(parsed.run_id)
                if refreshed.get("active_ticket_job") is None:
                    raise ValueError("Delivery Run has no Active Ticket Job")
                agent_fixture = getattr(parsed, "agent_fixture", None)
                agents = (
                    FixtureAgentBackend(Path(agent_fixture))
                    if agent_fixture
                    else CodexCliBackend()
                )
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(
                        github.repository().name_with_owner,
                        git,
                    )
                )
                state = TicketDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=agents,
                ).deliver(parsed.run_id)
                resumed = True
        output = {
            "result": "resumed" if resumed else "started",
            "run_id": state["run_id"],
            "status": state["status"],
            "run_branch": state["run_branch"],
            "active_ticket": (
                state["active_ticket_job"]["ticket_number"]
                if state["active_ticket_job"]
                else None
            ),
            "diagnostics": state["diagnostics"],
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return (
            0
            if state["status"] in {"active", "ticket_completed", "waiting_checks"}
            else 2
        )
    except (
        CodexProcessError,
        GitError,
        GitHubReadError,
        OSError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": "blocked",
                    "diagnostics": [
                        {
                            "code": "command_failed",
                            "message": str(error),
                        }
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        help="GitHub owner/name；默认由当前 Git remote 推断",
    )
    parser.add_argument(
        "--state-dir",
        help="运行状态目录；默认是仓库根目录下的 .agent-run",
    )
    parser.add_argument(
        "--github-fixture",
        help=argparse.SUPPRESS,
    )


def _positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Issue 编号必须是正整数")
    return number


if __name__ == "__main__":
    sys.exit(main())
