from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agent_run import cli_presentation, cli_surface
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller
from agent_run.delivery_cleanup import DeliveryCleanupEngine
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
        description="从 GitHub 父 Issue 启动或恢复本地交付运行",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("start", help="启动或幂等恢复交付运行")
    start.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(start)
    start.add_argument("--new-run", action="store_true", help=argparse.SUPPRESS)
    run = subcommands.add_parser(
        "run", help="自动推进交付运行至需要人工处理的阶段"
    )
    run.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(run)
    run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume = subcommands.add_parser("resume", help="按稳定运行 ID 恢复交付运行")
    resume.add_argument("run_id", help="交付运行标识")
    _add_common_options(resume)
    resume.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    deliver = subcommands.add_parser(
        "deliver", help="交付当前 Active Ticket Job"
    )
    deliver.add_argument("run_id", help="交付运行标识")
    _add_common_options(deliver)
    deliver.add_argument(
        "--agent-fixture",
        help=argparse.SUPPRESS,
    )
    accept_run = subcommands.add_parser(
        "accept-run", help="对完成的交付运行执行独立整体验收"
    )
    accept_run.add_argument("run_id", help="交付运行标识")
    _add_common_options(accept_run)
    accept_run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    publish_run = subcommands.add_parser(
        "publish-run", help="发布已通过整体验收的最终 Run PR"
    )
    publish_run.add_argument("run_id", help="交付运行标识")
    _add_common_options(publish_run)
    publish_run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    approve = subcommands.add_parser("approve", help="显式合并最终 Run PR")
    approve.add_argument("run_id", help="交付运行标识")
    _add_common_options(approve)
    revise = subcommands.add_parser("revise", help="以人工反馈开启新的 Run 修复窗口")
    revise.add_argument("run_id", help="交付运行标识")
    revise.add_argument("--message", required=True, help="未经改写的修订反馈")
    _add_common_options(revise)
    abandon = subcommands.add_parser("abandon", help="放弃交付运行并清理本地临时资源")
    abandon.add_argument("run_id", help="交付运行标识")
    _add_common_options(abandon)
    status = subcommands.add_parser("status", help="显示当前交付运行状态")
    status.add_argument("run_id", help="交付运行标识")
    _add_common_options(status)
    status.add_argument("--json", action="store_true", dest="as_json")
    history = subcommands.add_parser("history", help="显示交付运行时间线")
    history.add_argument("run_id", help="交付运行标识")
    _add_common_options(history)
    history.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    controller: Controller | None = None
    precondition_failed = False
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
        crash_after_save = getattr(parsed, "crash_after_save", None)
        states = (
            FaultInjectingStateStore(
                state_root, crash_after_save=crash_after_save
            )
            if isinstance(crash_after_save, int)
            else StateStore(state_root)
        )
        controller = Controller(github, git, states)
        if parsed.command == "status":
            state = cli_surface._load_local_run(states, parsed.run_id)
            cli_presentation._print_status(state, as_json=parsed.as_json)
            return 0
        if parsed.command == "history":
            state = cli_surface._load_local_run(states, parsed.run_id)
            cli_presentation._print_history(state, as_json=parsed.as_json)
            return 0
        if cli_surface._is_lifecycle_action(parsed.command):
            local_state = cli_surface._load_local_run(states, parsed.run_id)
            if not cli_surface._command_is_ready(local_state, parsed.command):
                cli_presentation._print_precondition_failure(local_state)
                return 2
        if parsed.command == "run":
            state, resumed = cli_surface._run_to_human_gate(parsed, states, controller)
        elif parsed.command == "start":
            state, resumed = controller.start(
                parsed.parent, reuse_existing=not parsed.new_run
            )
        elif parsed.command == "resume":
            state, resumed = controller.resume(
                parsed.run_id, resume_human_blocker=True
            )
            if state.get("status") == "unsupported_scope_change":
                cli_presentation._print_precondition_failure(state)
                return 2
            if state.get("status") == "abandonment_pending":
                cli_presentation._print_precondition_failure(state)
                return 2
            publisher = (
                FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                if parsed.github_fixture
                else GhGitHubPublisher(github.repository().name_with_owner, git)
            )
            agent_fixture = getattr(parsed, "agent_fixture", None)
            agents = (
                FixtureAgentBackend(Path(agent_fixture))
                if agent_fixture
                else CodexCliBackend()
            )
            publication_retried = _has_resumed_agent_phase(state)
            if publication_retried:
                if state.get("delivery_type") == "parent_only":
                    state = ParentDeliveryEngine(
                        git=git, states=states, github=publisher, agents=agents
                    ).deliver(parsed.run_id)
                elif state.get("status") == "active":
                    state = DeliveryRunEngine(
                        controller=controller,
                        tickets=TicketDeliveryEngine(
                            git=git,
                            states=states,
                            github=publisher,
                            agents=agents,
                        ),
                    ).deliver_from_state(parsed.run_id, state)
                elif state.get("status") == "run_acceptance_pending":
                    repository = github.repository()
                    state = RunAcceptanceEngine(
                        git=git,
                        states=states,
                        agents=agents,
                        default_head_sha=git.resolve_base(
                            repository.default_branch, repository.default_head_sha
                        ),
                        github=publisher,
                    ).accept(parsed.run_id)
                elif state.get("status") == "run_publication_pending":
                    repository = github.repository()
                    state = RunPublicationEngine(
                        git=git,
                        states=states,
                        agents=agents,
                        github=publisher,
                        default_branch=repository.default_branch,
                        default_head_sha=git.resolve_base(
                            repository.default_branch, repository.default_head_sha
                        ),
                    ).publish(parsed.run_id)
            parent_job = state.get("parent_job")
            run_publication = state.get("run_publication")
            if (
                state.get("delivery_type") == "parent_only"
                and isinstance(parent_job, dict)
                and parent_job.get("phase") == "merging"
            ):
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=CodexCliBackend(),
                ).recover_closeout(parsed.run_id)
            elif (
                state.get("delivery_type") == "parent_only"
                and state.get("status") == "publication_pending"
                and isinstance(parent_job, dict)
                and parent_job.get("phase") == "publication_pending"
            ):
                agents = (
                    FixtureAgentBackend(Path(parsed.agent_fixture))
                    if parsed.agent_fixture
                    else CodexCliBackend()
                )
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=agents,
                ).deliver(parsed.run_id)
                publication_retried = True
            elif (
                state.get("delivery_type") == "ticket_run"
                and state.get("status") == "publication_pending"
                and isinstance(run_publication, dict)
                and run_publication.get("phase") == "publication_pending"
            ):
                repository = github.repository()
                agents = (
                    FixtureAgentBackend(Path(parsed.agent_fixture))
                    if parsed.agent_fixture
                    else CodexCliBackend()
                )
                state = RunPublicationEngine(
                    git=git,
                    states=states,
                    agents=agents,
                    github=publisher,
                    default_branch=repository.default_branch,
                    default_head_sha=git.resolve_base(
                        repository.default_branch, repository.default_head_sha
                    ),
                ).publish(parsed.run_id)
                publication_retried = True
            elif (
                state.get("delivery_type") == "ticket_run"
                and state.get("status") == "parent_closeout_pending"
                and isinstance(run_publication, dict)
                and run_publication.get("phase") == "merged"
            ):
                repository = github.repository()
                state = RunPublicationEngine(
                    git=git,
                    states=states,
                    agents=CodexCliBackend(),
                    github=publisher,
                    default_branch=repository.default_branch,
                    default_head_sha=git.resolve_base(
                        repository.default_branch, repository.default_head_sha
                    ),
                ).recover_closeout(parsed.run_id)
                publication_retried = True
            elif (
                state.get("delivery_type") == "ticket_run"
                and isinstance(state.get("parent_job"), dict)
            ):
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=CodexCliBackend(),
                ).retire_for_child_flow(parsed.run_id)
            if not publication_retried:
                state = DeliveryCleanupEngine(
                    git=git, states=states, github=publisher
                ).resume(parsed.run_id)
        elif parsed.command == "deliver":
            refreshed, _ = controller.resume(parsed.run_id)
            if refreshed.get("status") in {"completed", "abandoned"}:
                state = refreshed
            elif refreshed.get("status") == "unsupported_scope_change":
                state = refreshed
                precondition_failed = True
            else:
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
                refreshed = DeliveryCleanupEngine(
                    git=git, states=states, github=publisher
                ).resume(parsed.run_id)
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
                        git=git,
                        states=states,
                        github=publisher,
                        agents=agents,
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
            if refreshed.get("status") in {"abandoned", "completed"}:
                state = refreshed
            elif (
                refreshed.get("status") == "unsupported_scope_change"
                and parsed.command != "abandon"
            ):
                # Graph drift is fail-closed and cannot be absorbed by a
                # lifecycle command.
                state = refreshed
                precondition_failed = True
            elif (
                parsed.command == "approve"
                and refreshed.get("delivery_type") == "parent_only"
                and refreshed.get("status") == "parent_approval_pending"
            ):
                state = ParentDeliveryEngine(
                    git=git, states=states, github=publisher, agents=agents
                ).approve(parsed.run_id)
            elif (
                parsed.command == "publish-run"
                and (
                    refreshed.get("status") == "run_publication_pending"
                    or (
                        isinstance(refreshed.get("run_publication"), dict)
                        and refreshed.get("status")
                        in {"waiting_checks", "run_approval_pending"}
                    )
                )
            ):
                state = publication.publish(parsed.run_id)
            elif parsed.command == "approve" and refreshed.get("status") == "run_approval_pending":
                state = publication.approve(parsed.run_id)
            elif parsed.command == "revise" and refreshed.get("status") in {
                "ready_for_human",
                "run_approval_pending",
            }:
                state = publication.revise(parsed.run_id, parsed.message)
            elif (
                parsed.command == "abandon"
                and refreshed.get("delivery_type") == "parent_only"
            ):
                state = ParentDeliveryEngine(
                    git=git,
                    states=states,
                    github=publisher,
                    agents=agents,
                ).abandon(parsed.run_id)
            else:
                state = (
                    refreshed
                    if parsed.command != "abandon"
                    else publication.abandon(parsed.run_id)
                )
                precondition_failed = parsed.command != "abandon"
            resumed = True
        active_ticket_job = state.get("active_ticket_job")
        diagnostics = state.get("diagnostics")
        current_diagnostics = diagnostics if isinstance(diagnostics, list) else []
        output = {
            "result": "resumed" if resumed else "started",
            "run_id": state["run_id"],
            "status": state["status"],
            "run_branch": state.get("run_branch", state.get("parent_branch")),
            "active_ticket": (
                active_ticket_job["ticket_number"]
                if isinstance(active_ticket_job, dict)
                else None
            ),
            "diagnostics": (
                [
                    *current_diagnostics,
                    {
                        "code": "command_precondition",
                        "message": "当前交付运行尚未满足此命令的执行条件",
                    },
                ]
                if precondition_failed
                else current_diagnostics
            ),
            "scope_change": state.get("unsupported_scope_change"),
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if precondition_failed:
            return 2
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
                "waiting_merge",
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
        durable_status = None
        if states is not None and isinstance(run_id, str):
            durable = states.load_run(run_id)
            if isinstance(durable, dict):
                durable_status = durable.get("status")
        diagnostic_code = (
            "multiple_unfinished_runs"
            if str(error).startswith("multiple unfinished Delivery Runs")
            else "command_failed"
        )
        diagnostic_message = (
            "同一父 Issue 存在多个未终止交付运行；候选运行："
            f"{str(error).partition(': ')[2]}。请先人工确定要保留的运行"
            if diagnostic_code == "multiple_unfinished_runs"
            else "命令执行失败；请通过 status 或 history 查看可恢复状态"
        )
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": (
                        "execution_failed"
                        if failure_recorded
                        else (
                            durable_status
                            if durable_status
                            in {"abandonment_pending", "completed", "abandoned"}
                            else "blocked"
                        )
                    ),
                    "diagnostics": [
                        {
                            "code": diagnostic_code,
                            "message": diagnostic_message,
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


def _has_resumed_agent_phase(state: dict[str, object]) -> bool:
    """Whether the explicit resume just re-entered a Human Blocker phase."""
    for key in ("active_ticket_job", "parent_job"):
        job = state.get(key)
        if isinstance(job, dict) and job.get("prior_human_blockers"):
            return True
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        if acceptance.get("prior_human_blockers"):
            return True
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict) and repair.get("prior_human_blockers"):
            return True
    publication = state.get("run_publication")
    return isinstance(publication, dict) and bool(
        publication.get("prior_human_blockers")
    )


if __name__ == "__main__":
    sys.exit(main())
