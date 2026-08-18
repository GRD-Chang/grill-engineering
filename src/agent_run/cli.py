from __future__ import annotations

import argparse
import json
import sys
import time
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
from agent_run.requeue import close_superseded_pull_request, remove_superseded_worktree
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.error_safety import bounded_error
from agent_run.external_supervision import ExternalSupervisor, is_github_refresh_wait
from agent_run.run_driver import DirectRunOperations, RunDriver
from agent_run.runner_promotion import (
    codex_cli_version,
    current_immutable_runner,
    promotion_audit_file,
    require_promotion_audit,
    run_promotion_handshake,
    verify_immutable_runner,
)
from agent_run.run_locator import RunLocatorError, RunLocatorIndex
from agent_run.state import FaultInjectingStateStore, StateStore
from agent_run.state_contract import IncompatibleRunStateError
from agent_run.worker_sandbox import WorkerSandboxError

_SUCCESSFUL_FOREGROUND_STATUSES = frozenset(
    {
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
        "waiting_external",
    }
)


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
        "run", help="推进正常 Job Loop，停在需要操作者处理的边界"
    )
    run.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(run)
    run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume = subcommands.add_parser(
        "resume", help="仅恢复当前失败或 Human Blocker 的 Agent Invocation"
    )
    resume.add_argument("run_id", help="交付运行标识")
    _add_common_options(resume)
    resume.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume.add_argument(
        "--new-thread",
        action="store_true",
        help="为当前失败或人工阻塞的 Agent 阶段新开 Thread",
    )
    requeue = subcommands.add_parser(
        "requeue", help="仅从 requeue_required 创建新的 Change Job Generation"
    )
    requeue.add_argument("run_id", help="交付运行标识")
    _add_common_options(requeue)
    requeue.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume.add_argument(
        "--message",
        help="仅用于当前 Human Blocker 的未经改写人工响应（最多 8 KiB）",
    )
    deliver = subcommands.add_parser("deliver", help="交付当前 Active Ticket Job")
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
    approve = subcommands.add_parser(
        "approve", help="显式批准并合并已通过门禁的最终 Run PR"
    )
    approve.add_argument("run_id", help="交付运行标识")
    _add_common_options(approve)
    revise = subcommands.add_parser("revise", help="以 Run 级人工反馈开启新的修复窗口")
    revise.add_argument("run_id", help="交付运行标识")
    revise.add_argument("--message", required=True, help="未经改写的修订反馈")
    _add_common_options(revise)
    abandon = subcommands.add_parser("abandon", help="放弃交付运行并执行受限恢复与清理")
    abandon.add_argument("run_id", help="交付运行标识")
    _add_common_options(abandon)
    status = subcommands.add_parser("status", help="显示当前状态与下一条允许的操作")
    status.add_argument("run_id", help="交付运行标识")
    _add_common_options(status)
    status.add_argument("--json", action="store_true", dest="as_json")
    history = subcommands.add_parser("history", help="显示有界 Invocation 与状态时间线")
    history.add_argument("run_id", help="交付运行标识")
    _add_common_options(history)
    history.add_argument("--json", action="store_true", dest="as_json")
    promotion = subcommands.add_parser(
        "promotion-handshake",
        help="从不可变 Runner 执行一次真实 Structured Outputs promotion handshake",
    )
    promotion.add_argument(
        "runner_sha", help="已合入 origin/main 的完整 40 位 commit SHA"
    )
    promotion.add_argument(
        "--audit-file", required=True, help="新建的脱敏 promotion 审计 JSON 路径"
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    controller: Controller | None = None
    states: StateStore | FaultInjectingStateStore | None = None
    precondition_failed = False
    try:
        if parsed.command in {"status", "history"}:
            state = _load_read_only_run(parsed)
            if parsed.command == "status":
                cli_presentation._print_status(state, as_json=parsed.as_json)
            else:
                cli_presentation._print_history(state, as_json=parsed.as_json)
            return 0
        git = GitRepository.discover(Path.cwd())
        if parsed.command == "promotion-handshake":
            verification = verify_immutable_runner(git.root, parsed.runner_sha)
            record = run_promotion_handshake(
                checkout=git.root,
                audit_file=Path(parsed.audit_file),
                verification=verification,
            )
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
            return 0 if record["handshake_verdict"] == "passed" else 2
        state_root = (
            Path(parsed.state_dir).resolve()
            if parsed.state_dir
            else git.root / ".agent-run"
        )
        fixture_path = Path(parsed.github_fixture) if parsed.github_fixture else None
        github = (
            FixtureGitHubReader(fixture_path)
            if fixture_path is not None
            else GhGitHubReader(parsed.repo, working_directory=git.root)
        )
        crash_after_save = getattr(parsed, "crash_after_save", None)
        states = (
            FaultInjectingStateStore(state_root, crash_after_save=crash_after_save)
            if isinstance(crash_after_save, int)
            else StateStore(state_root)
        )
        controller = Controller(github, git, states, locator=RunLocatorIndex.default())
        runner_verification = current_immutable_runner()
        if runner_verification is None:
            if fixture_path is None:
                raise ValueError(
                    "self-hosting lifecycle commands require an immutable promoted Runner"
                )
        else:
            active_codex_version = codex_cli_version()
            if active_codex_version is None:
                raise ValueError(
                    "could not determine Codex CLI version for promotion audit"
                )
            require_promotion_audit(
                runner_verification,
                promotion_audit_file(runner_verification),
                active_codex_version,
            )
        if cli_surface._is_lifecycle_action(parsed.command):
            local_state = cli_surface._load_local_run(states, parsed.run_id)
            if not cli_surface._command_is_ready(local_state, parsed.command):
                cli_presentation._print_precondition_failure(local_state)
                return 2
        if parsed.command == "run":
            driver = _run_driver(parsed, states, controller, git, github)
            state, resumed = cli_surface._run_to_human_gate(
                parsed, states, controller, driver
            )
        elif parsed.command == "start":
            state, resumed = controller.start(
                parsed.parent, reuse_existing=not parsed.new_run
            )
        elif parsed.command == "resume":
            current = cli_surface._load_local_run(states, parsed.run_id)
            if not cli_surface._resume_is_ready(current):
                cli_presentation._print_precondition_failure(current)
                return 2
            if current.get("status") == "supervision_timeout":
                state, resumed = cli_surface._resume_supervision(
                    parsed,
                    states,
                    _run_driver(parsed, states, controller, git, github),
                )
                active_ticket_job = state.get("active_ticket_job")
                diagnostics = state.get("diagnostics")
                current_diagnostics = (
                    diagnostics if isinstance(diagnostics, list) else []
                )
                print(
                    json.dumps(
                        {
                            "result": "resumed",
                            "run_id": state["run_id"],
                            "status": state["status"],
                            "run_branch": state.get(
                                "run_branch", state.get("parent_branch")
                            ),
                            "active_ticket": (
                                active_ticket_job.get("ticket_number")
                                if isinstance(active_ticket_job, dict)
                                else None
                            ),
                            "diagnostics": current_diagnostics,
                            "scope_change": state.get("unsupported_scope_change"),
                            "next_action": cli_presentation._next_action(state),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0 if state["status"] in _SUCCESSFUL_FOREGROUND_STATUSES else 2
            state, resumed = controller.resume(
                parsed.run_id,
                resume_human_blocker=True,
                new_thread=parsed.new_thread,
                human_response=parsed.message,
            )
            if state.get("status") == "unsupported_scope_change":
                cli_presentation._print_precondition_failure(state)
                return 2
            if state.get("status") == "execution_failed":
                cli_presentation._print_precondition_failure(state)
                return 2
            if state.get("status") == "abandonment_pending":
                cli_presentation._print_precondition_failure(state)
                return 2
            if state.get("status") == "requeue_required":
                cli_presentation._print_precondition_failure(state)
                return 2
            if _is_currentness_human_blocker(state):
                cli_presentation._print_precondition_failure(state)
                return 2
            if not is_github_refresh_wait(state):
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(str(state["repository"]), git)
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
                            git=git,
                            states=states,
                            github=publisher,
                            agents=agents,
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
                            currentness_reader=github,
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
                            currentness_reader=github,
                        ).publish(parsed.run_id)
        elif parsed.command == "requeue":
            state, retired = controller.requeue(parsed.run_id)
            if not is_github_refresh_wait(state):
                transition = state.get("requeue_transition")
                close_nonce = (
                    transition.get("close_nonce")
                    if isinstance(transition, dict)
                    else None
                )
                publisher = (
                    FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                    if parsed.github_fixture
                    else GhGitHubPublisher(str(state["repository"]), git)
                )
                retired_cleanly = close_superseded_pull_request(
                    publisher, retired, close_nonce
                )
                if not retired_cleanly:
                    state = controller.reject_requeue_after_pr_race(parsed.run_id)
                    precondition_failed = not is_github_refresh_wait(state)
                else:
                    remove_superseded_worktree(git, states.root, parsed.run_id, retired)
                    state = controller.finalize_requeue(parsed.run_id)
                subject = str(retired["work_subject"])
                agent_fixture = getattr(parsed, "agent_fixture", None)
                agents = (
                    FixtureAgentBackend(Path(agent_fixture))
                    if agent_fixture
                    else CodexCliBackend()
                )
                if subject.startswith("ticket:") and state.get("status") == "active":
                    state = DeliveryRunEngine(
                        controller=controller,
                        tickets=TicketDeliveryEngine(
                            git=git, states=states, github=publisher, agents=agents
                        ),
                    ).deliver_from_state(parsed.run_id, state)
                elif (
                    subject.startswith("parent-only:")
                    and state.get("status") == "parent_delivery_pending"
                ):
                    state = ParentDeliveryEngine(
                        git=git, states=states, github=publisher, agents=agents
                    ).deliver(parsed.run_id)
                elif (
                    subject.startswith("run-repair:")
                    and state.get("status") == "run_acceptance_pending"
                ):
                    repository = github.repository()
                    state = RunAcceptanceEngine(
                        git=git,
                        states=states,
                        agents=agents,
                        default_head_sha=git.resolve_base(
                            repository.default_branch, repository.default_head_sha
                        ),
                        github=publisher,
                        currentness_reader=github,
                    ).accept(parsed.run_id)
            resumed = True
        elif parsed.command == "deliver":
            refreshed, _ = controller.resume(parsed.run_id)
            if refreshed.get("status") in {"completed", "abandoned"}:
                state = refreshed
            elif is_github_refresh_wait(refreshed):
                state = refreshed
            elif refreshed.get("status") == "requeue_required":
                state = refreshed
                precondition_failed = True
            elif _is_currentness_human_blocker(refreshed):
                state = refreshed
                precondition_failed = True
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
                if refreshed.get("delivery_type") == "ticket_run" and isinstance(
                    refreshed.get("parent_job"), dict
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
            if refreshed.get("status") == "requeue_required":
                state = refreshed
                precondition_failed = True
            elif _is_currentness_human_blocker(refreshed):
                state = refreshed
                precondition_failed = True
            elif is_github_refresh_wait(refreshed):
                state = refreshed
            elif refreshed.get("status") not in {
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
                    currentness_reader=github,
                ).accept(parsed.run_id)
            resumed = True
        else:
            refreshed, _ = controller.resume(parsed.run_id)
            if is_github_refresh_wait(refreshed):
                diagnostics = refreshed.get("diagnostics")
                current_diagnostics = (
                    diagnostics if isinstance(diagnostics, list) else []
                )
                print(
                    json.dumps(
                        {
                            "result": "resumed",
                            "run_id": refreshed["run_id"],
                            "status": refreshed["status"],
                            "run_branch": refreshed.get(
                                "run_branch", refreshed.get("parent_branch")
                            ),
                            "active_ticket": None,
                            "diagnostics": current_diagnostics,
                            "scope_change": refreshed.get(
                                "unsupported_scope_change"
                            ),
                            "next_action": cli_presentation._next_action(refreshed),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
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
                currentness_reader=github,
            )
            if refreshed.get("status") in {"abandoned", "completed"}:
                state = refreshed
            elif refreshed.get("status") == "requeue_required":
                state = refreshed
                precondition_failed = True
            elif (
                _is_currentness_human_blocker(refreshed) and parsed.command != "abandon"
            ):
                state = refreshed
                precondition_failed = True
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
                parsed.command == "approve"
                and refreshed.get("delivery_type") == "parent_only"
                and refreshed.get("status") == "parent_closeout_pending"
            ):
                state = ParentDeliveryEngine(
                    git=git, states=states, github=publisher, agents=agents
                ).recover_closeout(parsed.run_id)
            elif parsed.command == "publish-run" and (
                refreshed.get("status") == "run_publication_pending"
                or (
                    isinstance(refreshed.get("run_publication"), dict)
                    and refreshed.get("status")
                    in {
                        "publication_pending",
                        "waiting_checks",
                        "waiting_external",
                        "run_approval_pending",
                    }
                )
            ):
                state = publication.publish(parsed.run_id)
            elif (
                parsed.command == "approve"
                and refreshed.get("status") == "run_approval_pending"
            ):
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
            "next_action": cli_presentation._next_action(state),
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if precondition_failed:
            return 2
        return 0 if state["status"] in _SUCCESSFUL_FOREGROUND_STATUSES else 2
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
        incompatible_state = isinstance(error, IncompatibleRunStateError)
        if (
            not incompatible_state
            and controller is not None
            and isinstance(run_id, str)
        ):
            failure_recorded = controller.record_execution_failure(
                run_id, bounded_error(str(error))
            )
        durable_status = None
        durable_diagnostics: list[object] | None = None
        if not incompatible_state and states is not None and isinstance(run_id, str):
            durable = states.load_run(run_id)
            if isinstance(durable, dict):
                durable_status = durable.get("status")
                diagnostics = durable.get("diagnostics")
                if isinstance(diagnostics, list):
                    durable_diagnostics = diagnostics
        locator_code = error.code if isinstance(error, RunLocatorError) else None
        locator_error = locator_code is not None
        diagnostic_code = (
            locator_code
            if locator_code is not None
            else (
                "incompatible_run_state"
                if incompatible_state
                else (
                    "multiple_unfinished_runs"
                    if str(error).startswith("multiple unfinished Delivery Runs")
                    else "command_failed"
                )
            )
        )
        diagnostic_message = (
            str(error)
            if locator_error
            else (
                "本地 Run state 不符合当前唯一 Invocation/Generation 契约；"
                "不会迁移、兼容读取或执行任何 mutation，请重新创建或清理该 Run"
                if diagnostic_code == "incompatible_run_state"
                else (
                    "同一父 Issue 存在多个未终止交付运行；候选运行："
                    f"{str(error).partition(': ')[2]}。请先人工确定要保留的运行"
                    if diagnostic_code == "multiple_unfinished_runs"
                    else "命令执行失败；请通过 status 或 history 查看可恢复状态"
                )
            )
        )
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": (
                        "blocked"
                        if locator_error
                        else (
                            "incompatible_run_state"
                            if incompatible_state
                            else (
                                "execution_failed"
                                if failure_recorded
                                else (
                                    durable_status
                                    if durable_status
                                    in {
                                        "abandonment_pending",
                                        "completed",
                                        "abandoned",
                                        "requeue_required",
                                    }
                                    else "blocked"
                                )
                            )
                        )
                    ),
                    "diagnostics": (
                        [
                            {
                                "code": diagnostic_code,
                                "message": diagnostic_message,
                            }
                        ]
                        if locator_error
                        or incompatible_state
                        or durable_status != "blocked"
                        or durable_diagnostics is None
                        else durable_diagnostics
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 2


def _foreground_supervisor(parsed: argparse.Namespace) -> ExternalSupervisor:
    if not parsed.github_fixture:
        return ExternalSupervisor(sleeper=time.sleep)
    fixture_path = Path(parsed.github_fixture)

    def advance_fixture_clock(seconds: float) -> None:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        current = fixture.get("supervision_clock", 0.0)
        if not isinstance(current, (int, float)):
            current = 0.0
        multiplier = fixture.get("supervision_clock_multiplier", 1)
        if not isinstance(multiplier, (int, float)) or multiplier <= 0:
            multiplier = 1
        fixture["supervision_clock"] = float(current) + seconds * multiplier
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    def fixture_now() -> float:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        current = fixture.get("supervision_clock", 0.0)
        return float(current) if isinstance(current, (int, float)) else 0.0

    return ExternalSupervisor(
        now=fixture_now, sleeper=advance_fixture_clock
    )


def _run_driver(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    controller: Controller,
    git: GitRepository,
    github: FixtureGitHubReader | GhGitHubReader,
) -> RunDriver:
    agents = (
        FixtureAgentBackend(Path(parsed.agent_fixture))
        if parsed.agent_fixture
        else CodexCliBackend()
    )
    return RunDriver(
        operations=DirectRunOperations(
            controller=controller,
            states=states,
            git=git,
            github_reader=github,
            publisher_factory=lambda: (
                FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                if parsed.github_fixture
                else GhGitHubPublisher(github.repository().name_with_owner, git)
            ),
            agents=agents,
        ),
        states=states,
        supervisor=_foreground_supervisor(parsed),
    )


def _load_read_only_run(parsed: argparse.Namespace) -> dict[str, object]:
    if parsed.state_dir:
        states = StateStore(Path(parsed.state_dir).resolve())
        return cli_surface._load_local_run(states, parsed.run_id)
    try:
        git = GitRepository.discover(Path.cwd())
    except GitError:
        git = None
    if git is not None:
        local = StateStore(git.root / ".agent-run").load_current_run(parsed.run_id)
        if local is not None:
            return local
    locator = RunLocatorIndex.default()
    state_dir = locator.resolve_state_dir(parsed.run_id)
    state = StateStore(state_dir).load_current_run(parsed.run_id)
    if state is None or state.get("run_id") != parsed.run_id:
        raise RunLocatorError(
            "run_locator_stale",
            f"无法定位 Delivery Run {parsed.run_id!r}：定位索引指向的状态文件无效；"
            "请显式提供 --state-dir <状态目录>。",
        )
    return state


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
    """Whether explicit resume re-entered a blocked or failed Agent phase."""
    invocation = state.get("active_agent_invocation")
    if (
        isinstance(invocation, dict)
        and invocation.get("status") in {"failed", "resuming"}
        and invocation.get("role")
        in {
            "development",
            "fresh_acceptance",
            "publication",
            "final_publication",
            "reviewer",
        }
    ):
        return True
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


def _is_currentness_human_blocker(state: dict[str, object]) -> bool:
    """Whether fresh external-state evidence has stopped this command."""
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )


if __name__ == "__main__":
    sys.exit(main())
