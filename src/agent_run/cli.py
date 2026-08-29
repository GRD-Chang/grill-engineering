from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from agent_run import cli_presentation, cli_surface
from agent_run import doctor
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_profiles import (
    AgentProfileStore,
    ProfileOverrides,
    ProfiledAgentBackend,
    validate_profile_options,
)
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_policy import (
    DeliveryPolicy,
    DeliveryPolicyError,
    DeliveryPolicyStore,
    resolve_delivery_policy,
)
from agent_run.git import DirtyManagedCheckoutError, GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_auth_profile import (
    GitHubAuthProfileError,
    GitHubAppProfileStore,
)
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.semantic_attempt import invocation_attempt_is_pending
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.error_safety import bounded_error
from agent_run.external_supervision import (
    ExternalSupervisor,
    is_github_refresh_wait,
    is_proven_github_state_contradiction,
)
from agent_run.run_driver import DirectRunOperations, RunDriver
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
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{start,run,resume,requeue,approve,revise,abandon,status,history,configure,policy,auth,doctor}",
    )
    start = subcommands.add_parser(
        "start", help="创建或返回交付运行及受管 Run Branch（不推进工作流）"
    )
    start.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(start)
    _add_profile_options(start)
    _add_policy_options(start)
    start.add_argument("--new-run", action="store_true", help=argparse.SUPPRESS)
    run = subcommands.add_parser(
        "run", help="推进正常 Job Loop，停在需要操作者处理的边界"
    )
    run.add_argument("parent", type=_positive_integer, help="Parent Issue 编号")
    _add_common_options(run)
    _add_profile_options(run)
    _add_policy_options(run)
    run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume = subcommands.add_parser(
        "resume", help="恢复失败/Human Blocker Invocation 或监督超时窗口"
    )
    resume.add_argument("run_id", help="交付运行标识")
    _add_common_options(resume)
    _add_policy_options(resume)
    resume.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume.add_argument(
        "--new-thread",
        action="store_true",
        help="为当前失败或人工阻塞的 Agent 阶段新开 Thread（监督超时不可用）",
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
    abandon.add_argument(
        "--discard-worktree",
        action="store_true",
        help="不可恢复地丢弃该 Run 的 dirty Managed Development Checkout",
    )
    _add_common_options(abandon)
    status = subcommands.add_parser("status", help="显示当前状态与下一条允许的操作")
    status.add_argument("run_id", help="交付运行标识")
    _add_common_options(status)
    status.add_argument("--json", action="store_true", dest="as_json")
    history = subcommands.add_parser("history", help="显示有界 Invocation 与状态时间线")
    history.add_argument("run_id", help="交付运行标识")
    _add_common_options(history)
    history.add_argument("--json", action="store_true", dest="as_json")
    configure = subcommands.add_parser(
        "configure",
        aliases=["config", "profile"],
        help="为未来创建的顶层 Codex Thread 创建新的 Agent Profile Revision",
    )
    configure.add_argument("run_id", help="交付运行标识")
    _add_common_options(configure)
    _add_profile_options(configure)
    configure.add_argument("--json", action="store_true", dest="as_json")
    policy = subcommands.add_parser(
        "policy",
        help="查看或配置用户级 Delivery Policy 默认值",
    )
    policy.add_argument("--json", action="store_true", dest="as_json")
    policy_commands = policy.add_subparsers(
        dest="policy_command", metavar="{show,configure}"
    )
    policy_show = policy_commands.add_parser("show", help="显示当前生效策略")
    policy_show.add_argument("--json", action="store_true", dest="as_json")
    policy_configure = policy_commands.add_parser(
        "configure", help="保存用户级 Delivery Policy 默认值"
    )
    _add_policy_options(policy_configure, dest_prefix="policy_")
    policy_configure.add_argument("--json", action="store_true", dest="as_json")
    auth = subcommands.add_parser("auth", help="配置 Worker 的 GitHub 只读身份")
    auth_commands = auth.add_subparsers(
        dest="auth_command", required=True, metavar="{status,app}"
    )
    auth_commands.add_parser("status", help="显示当前 Worker GitHub 只读身份")
    auth_app = auth_commands.add_parser("app", help="管理专用只读 GitHub App")
    auth_app_commands = auth_app.add_subparsers(
        dest="auth_app_command", required=True, metavar="{configure,remove}"
    )
    configure_app = auth_app_commands.add_parser(
        "configure", help="保存 GitHub App ID、Installation ID 和私钥路径"
    )
    configure_app.add_argument("--app-id", required=True, help="GitHub App ID")
    configure_app.add_argument(
        "--installation-id", required=True, help="GitHub App Installation ID"
    )
    configure_app.add_argument(
        "--private-key", required=True, help="仓库外私钥文件的绝对路径"
    )
    auth_app_commands.add_parser("remove", help="移除专用 App 并恢复宿主 gh")
    doctor_command = subcommands.add_parser(
        "doctor", help="只读检查本机依赖、Active Runner、PATH 与 Worker read provider"
    )
    doctor_command.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    supplied_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    return _main_with_parser(build_parser(), supplied_arguments)


def _main_with_parser(
    parser: argparse.ArgumentParser, arguments: Sequence[str] | None = None
) -> int:
    supplied_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    parsed = parser.parse_args(supplied_arguments)
    controller: Controller | None = None
    states: StateStore | FaultInjectingStateStore | None = None
    github: Any = None
    precondition_failed = False
    try:
        _validate_explicit_policy_options(parsed)
        delivery_policy_provider = (
            (lambda: _resolve_delivery_policy(parsed))
            if parsed.command in {"start", "run", "resume"}
            else None
        )
        creation_profile = (
            _profile_configuration(parsed)
            if parsed.command in {"start", "run"}
            else None
        )
        if parsed.command == "auth":
            return _auth_command(parsed)
        if parsed.command == "doctor":
            return doctor.run(as_json=parsed.as_json)
        if parsed.command == "policy":
            return _policy_command(parsed)
        if parsed.command in {"configure", "config", "profile"}:
            return _configure_profile(parsed)
        if parsed.command in {"status", "history"}:
            state = _load_read_only_run(parsed)
            if parsed.command == "status":
                cli_presentation._print_status(state, as_json=parsed.as_json)
            else:
                cli_presentation._print_history(state, as_json=parsed.as_json)
            return 0
        git = GitRepository.discover(Path.cwd())
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
        profiles = AgentProfileStore(state_root)
        controller = Controller(
            github,
            git,
            states,
            locator=RunLocatorIndex.default(),
            profiles=profiles,
            delivery_policy_provider=delivery_policy_provider,
        )
        if fixture_path is None and not _running_active_runner():
            raise ValueError(
                "self-hosting lifecycle commands require an installed Active Runner"
            )
        if parsed.command in {"resume", "requeue", "approve", "revise", "abandon"}:
            _require_profile(profiles, parsed.run_id)
        if cli_surface._is_lifecycle_action(parsed.command):
            local_state = cli_surface._load_local_run(states, parsed.run_id)
            if not cli_surface._command_is_ready(local_state, parsed.command):
                cli_presentation._print_precondition_failure(local_state)
                return 2
        if parsed.command == "run":
            driver = _run_driver(parsed, states, controller, git, github, profiles)
            state, resumed = cli_surface._run_to_human_gate(
                parsed,
                states,
                controller,
                driver,
                initialize_profile=lambda value, resumed_run: _initialize_profile(
                    profiles, value, creation_profile, allow_create=not resumed_run
                ),
            )
        elif parsed.command == "start":
            state, resumed = controller.start(
                parsed.parent, reuse_existing=not parsed.new_run
            )
            _initialize_profile(
                profiles, state, creation_profile, allow_create=not resumed
            )
        elif parsed.command == "resume":
            current = cli_surface._load_local_run(states, parsed.run_id)
            if not cli_surface._resume_is_ready(current):
                cli_presentation._print_precondition_failure(current)
                return 2
            if current.get("status") == "supervision_timeout" and (
                parsed.new_thread or parsed.message is not None
            ):
                cli_presentation._print_precondition_failure(current)
                return 2
            budget_checkpoint_resume = (
                cli_surface._review_budget_checkpoint_count(current) == 1
            )
            state, resumed = controller.resume(
                parsed.run_id,
                resume_human_blocker=current.get("status") != "supervision_timeout",
                new_thread=parsed.new_thread,
                human_response=parsed.message,
                explicit_resume=True,
                resume_budget_checkpoint=budget_checkpoint_resume,
            )
            if state.get("status") in {
                "unsupported_scope_change",
                "deterministic_contradiction",
            }:
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
                if current.get("status") == "supervision_timeout":
                    state = _run_driver(
                        parsed, states, controller, git, github, profiles
                    ).advance(state)
                else:
                    publisher = (
                        FixtureGitHubPublisher(Path(parsed.github_fixture), git)
                        if parsed.github_fixture
                        else GhGitHubPublisher(str(state["repository"]), git)
                    )
                    agent_fixture = getattr(parsed, "agent_fixture", None)
                    agents = _agent_backend(parsed, profiles)
                    publication_retried = (
                        _has_resumed_agent_phase(state) or budget_checkpoint_resume
                    )
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
            state = _run_driver(
                parsed, states, controller, git, github, profiles
            ).operations.requeue(parsed.run_id).state
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
            agents = _agent_backend(parsed, profiles)
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
                refreshed.get("status")
                in {"unsupported_scope_change", "deterministic_contradiction"}
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
                ).abandon(
                    parsed.run_id,
                    discard_worktree=parsed.discard_worktree,
                )
            else:
                state = (
                    refreshed
                    if parsed.command != "abandon"
                    else publication.abandon(
                        parsed.run_id,
                        discard_worktree=parsed.discard_worktree,
                    )
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
            "delivery_cleanup": cli_presentation._public_delivery_cleanup(state),
            "next_action": cli_presentation._next_action(state),
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if precondition_failed:
            return 2
        return 0 if state["status"] in _SUCCESSFUL_FOREGROUND_STATUSES else 2
    except KeyboardInterrupt:
        run_id = getattr(parsed, "run_id", None)
        if (
            not isinstance(run_id, str)
            and parsed.command == "run"
            and states is not None
            and github is not None
        ):
            repository = github.repository()
            interrupted = states.find_run(repository.name_with_owner, parsed.parent)
            if isinstance(interrupted, dict):
                run_id = interrupted.get("run_id")
        if controller is not None and isinstance(run_id, str):
            controller.record_execution_failure(run_id, "controller_interrupted")
        durable = (
            states.load_run(run_id)
            if states is not None and isinstance(run_id, str)
            else None
        )
        print(
            json.dumps(
                {
                    "result": "interrupted",
                    "run_id": run_id,
                    "status": "execution_failed",
                    "diagnostics": [
                        {
                            "code": "controller_interrupted",
                            "message": "控制器被中断；已保留 Managed Development Checkout 与当前 Semantic Agent Attempt",
                        }
                    ],
                    "next_action": (
                        cli_presentation._next_action(durable)
                        if isinstance(durable, dict)
                        else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 130
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
            and not isinstance(error, DeliveryPolicyError)
            and not isinstance(error, DirtyManagedCheckoutError)
            and controller is not None
            and isinstance(run_id, str)
        ):
            if isinstance(error, GitHubReadError) and is_proven_github_state_contradiction(
                error.code
            ):
                failure_recorded = controller.record_deterministic_contradiction(
                    run_id, error.code, error.message
                )
            else:
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
                "dirty_managed_checkout"
                if isinstance(error, DirtyManagedCheckoutError)
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
        )
        diagnostic_message = (
            str(error)
            if locator_error or isinstance(error, DirtyManagedCheckoutError)
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
                                "deterministic_contradiction"
                                if durable_status == "deterministic_contradiction"
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
                        or isinstance(error, DirtyManagedCheckoutError)
                        or incompatible_state
                        or durable_status not in {
                            "blocked",
                            "deterministic_contradiction",
                        }
                        or durable_diagnostics is None
                        else durable_diagnostics
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 2


def _running_active_runner() -> bool:
    data_home = os.environ.get("XDG_DATA_HOME")
    if not data_home:
        data_home = str(Path.home() / ".local" / "share")
    active_current = (
        Path(data_home).expanduser() / "agent-run" / "active" / "current"
    ).resolve()
    current_file = Path(__file__).resolve()
    try:
        current_file.relative_to(active_current)
    except ValueError:
        return False
    return True


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
    profiles: AgentProfileStore,
) -> RunDriver:
    agents = _agent_backend(parsed, profiles)
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
            profiles=profiles,
        ),
        states=states,
        supervisor=_foreground_supervisor(parsed),
    )


def _agent_backend(
    parsed: argparse.Namespace, profiles: AgentProfileStore
) -> ProfiledAgentBackend:
    fixture = getattr(parsed, "agent_fixture", None)
    backend = FixtureAgentBackend(Path(fixture)) if fixture else CodexCliBackend()
    run_id = getattr(parsed, "run_id", None)
    return ProfiledAgentBackend(
        backend,
        profiles,
        run_id=run_id if isinstance(run_id, str) else None,
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


def _add_profile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preset",
        "--profile-preset",
        dest="profile_preset",
        help="Agent Execution Preset（默认 economy）",
    )
    parser.add_argument(
        "--development-model",
        "--dev-model",
        dest="development_model",
        help="Development 顶层 Codex 的 model",
    )
    parser.add_argument(
        "--development-effort",
        "--development-reasoning-effort",
        "--dev-effort",
        dest="development_effort",
        help="Development 顶层 Codex 的 reasoning effort",
    )
    parser.add_argument(
        "--review-model", dest="review_model", help="Review 顶层 Codex 的 model"
    )
    parser.add_argument(
        "--review-effort",
        "--review-reasoning-effort",
        dest="review_effort",
        help="Review 顶层 Codex 的 reasoning effort",
    )
    parser.add_argument(
        "--publication-model",
        dest="publication_model",
        help="Publication 顶层 Codex 的 model",
    )
    parser.add_argument(
        "--publication-effort",
        "--publication-reasoning-effort",
        dest="publication_effort",
        help="Publication 顶层 Codex 的 reasoning effort",
    )
    parser.add_argument(
        "--publication-from-development",
        "--publication-reference-development",
        "--publication-use-development",
        action="store_true",
        default=None,
        dest="publication_from_development",
        help="让 Publication 恢复引用 Development Profile",
    )
    parser.add_argument(
        "--publication-reference",
        choices=("development",),
        dest="publication_reference",
        help=argparse.SUPPRESS,
    )


def _add_policy_options(
    parser: argparse.ArgumentParser, *, dest_prefix: str = ""
) -> None:
    parser.add_argument(
        "--ticket-review-rounds",
        "--ticket-review-round",
        dest=f"{dest_prefix}ticket_review_rounds",
        type=_positive_integer,
        help="Ticket Review 语义轮数（推导 Development=N+1）",
    )
    for role, label in (
        ("development", "Development"),
        ("review", "Review"),
        ("publication", "Publication"),
    ):
        parser.add_argument(
            f"--{role}-deadline",
            f"--{role}-duration",
            f"--{role}-timeout",
            dest=f"{dest_prefix}{role}_deadline",
            type=_positive_duration_argument,
            help=f"{label} Invocation 正 duration（可用秒或 s/m/h/d）",
        )


def _policy_overrides(parsed: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key in ("ticket_review_rounds",):
        value = getattr(parsed, f"policy_{key}", None)
        if value is None:
            value = getattr(parsed, key, None)
        if value is not None:
            overrides[key] = value
    deadlines: dict[str, Any] = {}
    for role in ("development", "review", "publication"):
        value = getattr(parsed, f"policy_{role}_deadline", None)
        if value is None:
            value = getattr(parsed, f"{role}_deadline", None)
        if value is not None:
            deadlines[role] = value
    if deadlines:
        overrides["invocation_deadlines"] = deadlines
    return overrides


def _resolve_delivery_policy(parsed: argparse.Namespace) -> DeliveryPolicy:
    store = DeliveryPolicyStore()
    return resolve_delivery_policy(
        user_defaults=store.load(),
        command_overrides=_policy_overrides(parsed),
    )


def _validate_explicit_policy_options(parsed: argparse.Namespace) -> None:
    overrides = _policy_overrides(parsed)
    if overrides:
        resolve_delivery_policy(command_overrides=overrides)


def _policy_command(parsed: argparse.Namespace) -> int:
    store = DeliveryPolicyStore()
    command = getattr(parsed, "policy_command", None)
    overrides = _policy_overrides(parsed)
    if command == "show" or (command is None and not overrides):
        user_defaults = store.load()
        policy = resolve_delivery_policy(user_defaults=user_defaults)
        result = {
            "result": "policy",
            "policy": policy.snapshot(),
            "user_defaults": user_defaults or {},
        }
    else:
        policy = store.configure(overrides)
        user_defaults = store.load()
        result = {
            "result": "configured",
            "policy": policy.snapshot(),
            "user_defaults": user_defaults or {},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _profile_configuration(
    parsed: argparse.Namespace,
) -> tuple[str | None, ProfileOverrides]:
    publication_from_development = parsed.publication_from_development
    if parsed.publication_reference == "development":
        publication_from_development = True
    overrides: dict[str, str | bool | None] = {
        "development_model": parsed.development_model,
        "development_effort": parsed.development_effort,
        "review_model": parsed.review_model,
        "review_effort": parsed.review_effort,
        "publication_model": parsed.publication_model,
        "publication_effort": parsed.publication_effort,
        "publication_from_development": publication_from_development,
    }
    validate_profile_options(preset=parsed.profile_preset, overrides=overrides)
    return parsed.profile_preset, overrides


def _initialize_profile(
    profiles: AgentProfileStore,
    state: dict[str, object],
    configuration: tuple[str | None, ProfileOverrides] | None,
    *,
    allow_create: bool = True,
) -> None:
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("Delivery Run is missing its Run ID")
    if not allow_create and profiles.load(run_id) is None:
        raise IncompatibleRunStateError(
            "Delivery Run lacks an Agent Execution Profile; create a new Run"
        )
    preset, overrides = configuration or (None, {})
    profiles.initialize(run_id, preset=preset, overrides=overrides)


def _require_profile(profiles: AgentProfileStore, run_id: str) -> None:
    if profiles.load(run_id) is None:
        raise IncompatibleRunStateError(
            "Delivery Run lacks an Agent Execution Profile; create a new Run"
        )


def _configure_profile(parsed: argparse.Namespace) -> int:
    state_root = _profile_state_root(parsed)
    state = StateStore(state_root).load_current_run(parsed.run_id)
    if state is None:
        raise ValueError(f"unknown Delivery Run: {parsed.run_id}")
    preset, overrides = _profile_configuration(parsed)
    document = AgentProfileStore(state_root).configure(
        parsed.run_id, preset=preset, overrides=overrides
    )
    print(
        json.dumps(
            {
                "result": "configured",
                "run_id": parsed.run_id,
                "profile_revision": document["profile_revision"],
                "preset": document["preset"],
                "profiles": document["profiles"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _auth_command(parsed: argparse.Namespace) -> int:
    try:
        store = GitHubAppProfileStore()
        if parsed.auth_command == "status":
            configured = store.load() is not None
            print(
                json.dumps(
                    {
                        "app_profile": "configured"
                        if configured
                        else "not_configured",
                        "provider": "app" if configured else "host",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if parsed.auth_app_command == "configure":
            store.configure(
                app_id=parsed.app_id,
                installation_id=parsed.installation_id,
                private_key_path=parsed.private_key,
            )
            print(json.dumps({"provider": "app", "result": "configured"}, sort_keys=True))
            return 0
        if parsed.auth_app_command == "remove":
            store.remove()
            print(
                json.dumps(
                    {
                        "app_profile": "not_configured",
                        "provider": "host",
                        "result": "removed",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        raise ValueError("未知 GitHub auth 命令")
    except GitHubAuthProfileError as error:
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": "invalid_auth_profile",
                    "diagnostics": [
                        {"code": "github_auth_profile_invalid", "message": bounded_error(str(error))}
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


def _profile_state_root(parsed: argparse.Namespace) -> Path:
    if parsed.state_dir:
        return Path(parsed.state_dir).resolve()
    try:
        git = GitRepository.discover(Path.cwd())
    except GitError:
        git = None
    if git is not None:
        local_root = git.root / ".agent-run"
        if StateStore(local_root).load_run(parsed.run_id) is not None:
            return local_root
    return RunLocatorIndex.default().resolve_state_dir(parsed.run_id)


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _positive_duration_argument(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError("duration 必须为正数，可带 s/m/h/d 后缀")
    return value


def _has_resumed_agent_phase(state: dict[str, object]) -> bool:
    """Whether explicit resume re-entered a blocked or failed Agent phase."""
    invocation = state.get("active_agent_invocation")
    if (
        isinstance(invocation, dict)
        and (
            invocation.get("status") in {"failed", "resuming"}
            or (
                invocation.get("status") == "completed"
                and invocation_attempt_is_pending(state, invocation)
            )
        )
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
