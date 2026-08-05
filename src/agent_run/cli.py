from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agent_run.agent_fixture import (
    FixtureAgentBackend,
    FixtureScopeImpactAssessor,
)
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import FaultInjectingStateStore, StateStore
from agent_run.worker_sandbox import WorkerSandboxError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-run",
        description="从 GitHub Parent Issue 启动或恢复本地 Delivery Run",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("start", help="启动或幂等恢复 Delivery Run")
    start.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(start)
    resume = subcommands.add_parser("resume", help="按稳定 Run ID 恢复 Delivery Run")
    resume.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(resume)
    confirm_structure = subcommands.add_parser(
        "confirm-structure",
        help="确认当前待处理的 Ticket 图结构变化",
    )
    confirm_structure.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(confirm_structure)
    deliver = subcommands.add_parser(
        "deliver", help="交付当前 Active Ticket Job"
    )
    deliver.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(deliver)
    deliver.add_argument(
        "--agent-fixture",
        help=argparse.SUPPRESS,
    )
    accept_run = subcommands.add_parser(
        "accept-run", help="对完成的 Delivery Run 执行独立整体验收"
    )
    accept_run.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(accept_run)
    accept_run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    publish_run = subcommands.add_parser(
        "publish-run", help="发布已通过整体验收的最终 Run PR"
    )
    publish_run.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(publish_run)
    publish_run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    approve = subcommands.add_parser("approve", help="显式合并最终 Run PR")
    approve.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(approve)
    revise = subcommands.add_parser("revise", help="以人工反馈开启新的 Run 修复窗口")
    revise.add_argument("run_id", help="Delivery Run 标识")
    revise.add_argument("--message", required=True, help="未经改写的修订反馈")
    _add_common_options(revise)
    abandon = subcommands.add_parser("abandon", help="放弃 Delivery Run 并清理本地临时资源")
    abandon.add_argument("run_id", help="Delivery Run 标识")
    _add_common_options(abandon)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    controller: Controller | None = None
    try:
        git = GitRepository.discover(Path.cwd())
        state_root = (
            Path(parsed.state_dir).resolve()
            if parsed.state_dir
            else git.root / ".agent-run"
        )
        fixture_path = (
            Path(parsed.github_fixture) if parsed.github_fixture else None
        )
        github = (
            FixtureGitHubReader(fixture_path)
            if fixture_path is not None
            else GhGitHubReader(parsed.repo)
        )
        scope_assessor = (
            FixtureScopeImpactAssessor(fixture_path)
            if fixture_path is not None
            else CodexCliBackend()
        )
        crash_after_save = getattr(parsed, "crash_after_save", None)
        states = (
            FaultInjectingStateStore(
                state_root, crash_after_save=crash_after_save
            )
            if isinstance(crash_after_save, int)
            else StateStore(state_root)
        )
        controller = Controller(
            github, git, states, scope_assessor=scope_assessor
        )
        if parsed.command == "start":
            state, resumed = controller.start(parsed.parent)
        elif parsed.command == "resume":
            state, resumed = controller.resume(parsed.run_id)
            if (
                state.get("delivery_type") == "parent_only"
                and isinstance(state.get("parent_job"), dict)
                and state["parent_job"].get("phase") == "merging"
            ):
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(github.repository().name_with_owner, git)
                )
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=CodexCliBackend(),
                ).recover_closeout(parsed.run_id)
            elif (
                state.get("delivery_type") == "ticket_run"
                and isinstance(state.get("parent_job"), dict)
            ):
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(github.repository().name_with_owner, git)
                )
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=CodexCliBackend(),
                ).retire_for_child_flow(parsed.run_id)
        elif parsed.command == "confirm-structure":
            before_confirmation = states.load_run(parsed.run_id)
            state, resumed = controller.confirm_structure(parsed.run_id)
            if (
                isinstance(before_confirmation, dict)
                and before_confirmation.get("delivery_type") == "parent_only"
                and state.get("delivery_type") == "ticket_run"
            ):
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(github.repository().name_with_owner, git)
                )
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=CodexCliBackend(),
                ).retire_for_child_flow(parsed.run_id)
        elif parsed.command == "deliver":
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
            refreshed, _ = controller.resume(parsed.run_id)
            if (
                refreshed.get("delivery_type") == "ticket_run"
                and isinstance(refreshed.get("parent_job"), dict)
            ):
                refreshed = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=agents,
                ).retire_for_child_flow(parsed.run_id)
            if refreshed.get("delivery_type") == "parent_only":
                state = ParentDeliveryEngine(
                    git=git, states=states, github=publisher, agents=agents
                ).deliver(parsed.run_id)
            else:
                state = DeliveryRunEngine(
                    controller=controller,
                    tickets=TicketDeliveryEngine(
                        git=git,
                        states=states,
                        github=publisher,
                        agents=agents,
                    ),
                ).deliver_from_state(parsed.run_id, refreshed)
            resumed = True
        elif parsed.command == "accept-run":
            agent_fixture = getattr(parsed, "agent_fixture", None)
            agents = (
                FixtureAgentBackend(Path(agent_fixture))
                if agent_fixture
                else CodexCliBackend()
            )
            refreshed, _ = controller.resume(parsed.run_id)
            if refreshed.get("status") not in {
                "run_acceptance_pending",
                "run_publication_pending",
            }:
                state = refreshed
            else:
                repository = github.repository()
                default_head = git.resolve_base(
                    repository.default_branch, repository.default_head_sha
                )
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(repository.name_with_owner, git)
                )
                state = RunAcceptanceEngine(
                    git=git,
                    states=states,
                    agents=agents,
                    default_head_sha=default_head,
                    github=publisher,
                ).accept(parsed.run_id)
            resumed = True
        else:
            refreshed, _ = controller.resume(parsed.run_id)
            repository = github.repository()
            default_head = git.resolve_base(
                repository.default_branch, repository.default_head_sha
            )
            publisher = (
                FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                if parsed.github_fixture
                else GhGitHubPublisher(repository.name_with_owner, git)
            )
            if parsed.command in {"publish-run", "revise"}:
                agent_fixture = getattr(parsed, "agent_fixture", None)
                agents = (
                    FixtureAgentBackend(Path(agent_fixture))
                    if agent_fixture
                    else CodexCliBackend()
                )
            else:
                agents = CodexCliBackend()
            publication = RunPublicationEngine(
                git=git,
                states=states,
                agents=agents,
                github=publisher,
                default_branch=repository.default_branch,
                default_head_sha=default_head,
            )
            if refreshed.get("status") == "abandoned":
                state = refreshed
            elif (
                refreshed.get("status") == "structure_change_pending"
                and parsed.command != "abandon"
            ):
                # A proposed Parent/Ticket graph is not an accepted delivery
                # boundary. Do not let publication or approval bypass the
                # explicit confirm-structure command.
                state = refreshed
            elif (
                parsed.command == "approve"
                and refreshed.get("delivery_type") == "parent_only"
            ):
                state = ParentDeliveryEngine(
                    git=git, states=states, github=publisher, agents=agents
                ).approve(parsed.run_id)
            elif parsed.command == "publish-run":
                state = publication.publish(parsed.run_id)
            elif parsed.command == "approve":
                state = publication.approve(parsed.run_id)
            elif parsed.command == "revise":
                state = publication.revise(parsed.run_id, parsed.message)
            else:
                state = publication.abandon(parsed.run_id)
            resumed = True
        output = {
            "result": "resumed" if resumed else "started",
            "run_id": state["run_id"],
            "status": state["status"],
            "run_branch": state.get("run_branch", state.get("parent_branch")),
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
            if state["status"]
            in {
                "active",
                "ticket_completed",
                "waiting_checks",
                "parent_delivery_pending",
                "parent_approval_pending",
                "parent_closeout_pending",
                "publication_pending",
                "run_acceptance_pending",
                "run_publication_pending",
                "run_approval_pending",
                "completed",
                "abandoned",
            }
            else 2
        )
    except (
        CodexProcessError,
        GitError,
        GitHubReadError,
        OSError,
        ValueError,
        WorkerSandboxError,
    ) as error:
        run_id = getattr(parsed, "run_id", None)
        failure_recorded = False
        if controller is not None and isinstance(run_id, str):
            failure_recorded = controller.record_execution_failure(
                run_id, str(error)
            )
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": (
                        "execution_failed" if failure_recorded else "blocked"
                    ),
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
    parser.add_argument(
        "--crash-after-save",
        type=_positive_integer,
        help=argparse.SUPPRESS,
    )


def _positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Issue 编号必须是正整数")
    return number


if __name__ == "__main__":
    sys.exit(main())
