from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Sequence

from agent_run import cli_presentation, cli_surface
from agent_run import doctor
from agent_run.cli_parser import ArgumentParser
from agent_run.cli_messages import cli_message, personal_language, error_message, error_detail
from agent_run.messages import selected_language, text
from agent_run.models import same_repository
from agent_run.user_defaults import UserDefaultsStore, notification_snapshot
from agent_run import settings_cli, prompt_cli
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_invocation import record_session_interruption
from agent_run.agent_profiles import (
    AgentProfileStore,
    ProfileOverrides,
    ProfiledAgentBackend,
    validate_profile_options,
)
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.controller import Controller, _validated_human_response
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_cleanup import require_clean_run_worktrees
from agent_run.delivery_policy import (
    DeliveryPolicy,
    DeliveryPolicyError,
    DeliveryPolicyStore,
    parse_policy_snapshot,
    policy_snapshot_for_state,
    resolve_delivery_policy,
)
from agent_run.managed_workspace import workspace_for_root, workspace_state_root, validate_data_root
from agent_run.workspace_cli import open_workspace, selected_repository_root
from agent_run.git import DirtyManagedCheckoutError, GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_auth_profile import (
    GitHubAuthProfileError,
    GitHubAppProfileStore,
)
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.github_retry import MAX_READ_ATTEMPTS
from agent_run.operator_gate import (
    has_local_operator_gate,
    has_non_invocation_execution_failure,
    has_run_operator_gate,
)
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.presentation_helpers import human_next_action
from agent_run.error_safety import bounded_error
from agent_run.external_supervision import (
    ExternalSupervisor,
    is_github_refresh_wait,
    is_proven_github_state_contradiction,
)
from agent_run.executor import DeliveryExecutor
from agent_run.executor_environment import default_executor_runtime_directory
from agent_run.executor_host import (
    BoundExecutorHost,
    ExecutorAgentInterruptedError,
    ExecutorHost,
    ExecutorHostError,
    ExecutorLostError,
    ExecutorSpec,
    ExecutorStartUnknownError,
    FakeExecutorHost,
    _process_start_token,
    validate_executor_exit,
)
from agent_run.runner_lease import default_runner_lock_path, runner_usage_lease
from agent_run.systemd_executor_host import (
    SystemdExecutionReadinessError,
    SystemdUserExecutorHost,
    observe_systemd_executor,
)
from agent_run.run_driver import (
    ControlRunOperation,
    DirectRunOperations,
    RunDriver,
    RunOutcomeKind,
)
from agent_run.final_approval_operation import (
    final_approval_busy_message,
    final_approval_cleanup_pending,
    final_approval_failure,
    has_final_approval,
    has_unfinished_final_receipt,
    is_final_approval_action,
)
from agent_run.run_lifecycle import (
    ActionReceipt,
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
    _unbound_action_matches_run_receipt,
)
from agent_run.resume_feedback import ResumeFeedback
from agent_run.resume_intent import (
    ResumeIntentError,
    bind_resume_intent,
    validate_resume_intent,
)
from agent_run.run_locator import (
    MAX_LOCATOR_ENTRIES,
    LocatorEntry,
    RunLocatorError,
    RunLocatorIndex,
)
from agent_run.state import (
    FaultInjectingStateStore,
    SimulatedProcessCrash,
    StateStore,
)
from agent_run.state_contract import (
    IncompatibleRunStateError,
    human_blocker_subject_count,
    require_current_run_state,
)
from agent_run.task_control import (
    ActionBusyError,
    TASK_CONTROL_PROTOCOL,
    TaskControlError,
    TaskControlStore,
    TaskKey,
    action_receipt_matches,
    payload_digest,
    unresolved_control_target,
)
from agent_run.worker_sandbox import WorkerSandboxError

_SUCCESSFUL_FOREGROUND_STATUSES = frozenset(
    {
        "active",
        "ticket_completed",
        "waiting_checks",
        "parent_delivery_pending",
        "parent_approval_pending",
        "parent_closeout_pending",
        "run_acceptance_pending",
        "run_publication_pending",
        "run_approval_pending",
        "completed",
        "abandoned",
        "waiting_merge",
        "waiting_external",
    }
)


class ExecutionReadinessError(ValueError):
    """A production lifecycle command has no detached Executor Host."""


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="agent-run",
        description=cli_message('cli.description'),
    )
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar=(
            "{run,resume,requeue,approve,revise,stop,abandon,status,history,"
            "runs,configure,settings,prompts,policy,auth,doctor}"
        ),
    )
    run = subcommands.add_parser(
        "run", help=cli_message('cli.run_help')
    )
    run.add_argument("parent", type=_positive_integer, help=cli_message('cli.parent_help'))
    _add_common_options(run)
    _add_profile_options(run)
    _add_policy_options(run)
    run.add_argument("--json", action="store_true", dest="as_json")
    notification_options = run.add_mutually_exclusive_group()
    notification_options.add_argument("--notification-mode", choices=("concise", "detailed"), help=cli_message('cli.notifications_help'))
    notification_options.add_argument("--no-notifications", action="store_true", help=cli_message('cli.no_notifications_help'))
    run.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume = subcommands.add_parser(
        "resume", help=cli_message('cli.resume_help')
    )
    resume.add_argument(
        "run_id",
        help=cli_message('cli.resume_selector_help'),
    )
    _add_common_options(resume)
    _add_policy_options(resume, allow_thread_policy=False)
    resume.add_argument("--json", action="store_true", dest="as_json")
    resume.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume.add_argument(
        "--new-thread",
        action="store_true",
        help=cli_message('cli.new_thread_help'),
    )
    requeue = subcommands.add_parser(
        "requeue", help=cli_message('cli.requeue_help')
    )
    requeue.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    _add_common_options(requeue)
    requeue.add_argument("--json", action="store_true", dest="as_json")
    requeue.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    resume.add_argument(
        "--message",
        help=cli_message('cli.response_help'),
    )
    approve = subcommands.add_parser(
        "approve", help=cli_message('cli.approve_help')
    )
    approve.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    _add_common_options(approve)
    approve.add_argument("--json", action="store_true", dest="as_json")
    approve.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    revise = subcommands.add_parser("revise", help=cli_message('cli.revise_help'))
    revise.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    revise.add_argument("--message", required=True, help=cli_message('cli.feedback_help'))
    _add_common_options(revise)
    revise.add_argument("--json", action="store_true", dest="as_json")
    revise.add_argument("--agent-fixture", help=argparse.SUPPRESS)
    stop = subcommands.add_parser(
        "stop", help=cli_message('cli.stop_help')
    )
    stop.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    _add_common_options(stop)
    stop.add_argument("--json", action="store_true", dest="as_json")
    abandon = subcommands.add_parser("abandon", help=cli_message('cli.abandon_help'))
    abandon.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    abandon.add_argument(
        "--discard-worktree",
        action="store_true",
        help=cli_message('cli.discard_help'),
    )
    _add_common_options(abandon)
    abandon.add_argument("--json", action="store_true", dest="as_json")
    status = subcommands.add_parser("status", help=cli_message('cli.status_help'))
    status.add_argument("run_id", nargs="?", help=cli_message('cli.optional_selector_help'))
    _add_common_options(status)
    status.add_argument("--parent", type=_positive_integer, help=cli_message('cli.parent_selector_help'))
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument(
        "--plain",
        action="store_true",
        help=cli_message('cli.plain_help'),
    )
    history = subcommands.add_parser("history", help=cli_message('cli.history_help'))
    history.add_argument("run_id", nargs="?", help=cli_message('cli.optional_selector_help'))
    _add_common_options(history)
    history.add_argument("--parent", type=_positive_integer, help=cli_message('cli.parent_selector_help'))
    history.add_argument("--json", action="store_true", dest="as_json")
    history.add_argument(
        "--plain",
        action="store_true",
        help=cli_message('cli.history_plain_help'),
    )
    history.add_argument(
        "--details",
        action="store_true",
        help=cli_message('cli.details_help'),
    )
    runs = subcommands.add_parser("runs", help=cli_message('cli.runs_help'))
    _add_common_options(runs)
    runs.add_argument("--parent", type=_positive_integer, help=cli_message('cli.parent_filter_help'))
    runs.add_argument("--json", action="store_true", dest="as_json")
    configure = subcommands.add_parser(
        "configure",
        aliases=["config", "profile"],
        help=cli_message('cli.configure_help'),
    )
    configure.add_argument(
        "run_id",
        metavar="parent-issue-or-run-id",
        help=cli_message('cli.selector_help'),
    )
    _add_common_options(configure)
    _add_profile_options(configure)
    configure.add_argument("--json", action="store_true", dest="as_json")
    policy = subcommands.add_parser(
        "policy",
        help=cli_message('cli.policy_help'),
    )
    policy.add_argument("--json", action="store_true", dest="as_json")
    policy_commands = policy.add_subparsers(
        dest="policy_command", metavar="{show,configure}"
    )
    policy_show = policy_commands.add_parser("show", help=cli_message('cli.policy_show_help'))
    policy_show.add_argument("--json", action="store_true", dest="as_json")
    policy_configure = policy_commands.add_parser(
        "configure", help=cli_message('cli.policy_configure_help')
    )
    _add_policy_options(policy_configure, dest_prefix="policy_")
    policy_configure.add_argument("--json", action="store_true", dest="as_json")
    settings_cli.add_parser(
        subcommands, _add_common_options, _add_policy_options, _add_profile_options
    )
    prompt_cli.add_parser(subcommands)
    auth = subcommands.add_parser("auth", help=cli_message('cli.auth_help'))
    auth_commands = auth.add_subparsers(
        dest="auth_command", required=True, metavar="{status,app}"
    )
    auth_commands.add_parser("status", help=cli_message('cli.auth_status_help'))
    auth_app = auth_commands.add_parser("app", help=cli_message('cli.auth_app_help'))
    auth_app_commands = auth_app.add_subparsers(
        dest="auth_app_command", required=True, metavar="{configure,remove}"
    )
    configure_app = auth_app_commands.add_parser(
        "configure", help=cli_message('cli.auth_configure_help')
    )
    configure_app.add_argument("--app-id", required=True, help="GitHub App ID")
    configure_app.add_argument(
        "--installation-id", required=True, help="GitHub App Installation ID"
    )
    configure_app.add_argument(
        "--private-key", required=True, help=cli_message('cli.private_key_help')
    )
    auth_app_commands.add_parser("remove", help=cli_message('cli.auth_remove_help'))
    doctor_command = subcommands.add_parser(
        "doctor", help=cli_message('cli.doctor_help')
    )
    doctor_command.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    supplied_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    return _main_with_parser(build_parser(), supplied_arguments)


def _main_with_parser(
    parser: argparse.ArgumentParser, arguments: Sequence[str] | None = None
) -> int:
    with ExitStack() as resources:
        return _main_with_parser_resources(parser, arguments, resources)


def _main_with_parser_resources(
    parser: argparse.ArgumentParser,
    arguments: Sequence[str] | None,
    resources: ExitStack,
) -> int:
    supplied_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    parsed = parser.parse_args(supplied_arguments)
    resume_feedback: ResumeFeedback | None = None
    controller: Controller | None = None
    states: StateStore | FaultInjectingStateStore | None = None
    git: GitRepository | None = None
    github: Any = None
    precondition_failed = False
    lifecycle_receipt: ActionReceipt | None = None
    control_failure: str | None = None
    command_dispatched = False
    executor_binding: tuple[str, int] | None = None
    bootstrap_host: SystemdUserExecutorHost | None = None
    runner_lease_fd: int | None = None
    try:
        if getattr(parsed, "repo", None):
            parsed.repo = parsed.repo.lower()
        _validate_explicit_policy_options(parsed)
        delivery_policy_provider = (
            (lambda: _resolve_delivery_policy(parsed))
            if parsed.command in {"run", "resume"}
            else None
        )
        creation_profile = (
            _profile_configuration(parsed)
            if parsed.command == "run"
            else None
        )
        if parsed.command == "auth":
            return _auth_command(parsed)
        if parsed.command == "doctor":
            return doctor.run(as_json=parsed.as_json)
        if parsed.command == "prompts":
            return prompt_cli.execute(parsed)
        if parsed.command == "settings":
            return settings_cli.execute(
                parsed, policy_overrides=_policy_overrides,
                profile_configuration=_profile_configuration,
                load_run=_load_read_only_run, profile_root=_profile_state_root,
            )
        if parsed.command == "policy":
            return _policy_command(parsed)
        if parsed.command == "runs":
            return _list_runs(parsed)
        if parsed.command in {"status", "history"}:
            state = _load_read_only_run(parsed)
            if parsed.command == "status":
                cli_presentation._print_status(
                    state, as_json=parsed.as_json, plain=parsed.plain
                )
            else:
                cli_presentation._print_history(
                    state,
                    as_json=parsed.as_json,
                    plain=parsed.plain,
                    details=parsed.details,
                )
            return 0
        target_root = selected_repository_root(parsed.repo, parsed.github_fixture)
        if (
            parsed.command == "run" and parsed.github_fixture is None
            and target_root is not None and not target_root.exists()
        ):
            validate_data_root()
            if not _running_active_runner():
                raise ExecutionReadinessError(
                    "self-hosting lifecycle commands require an installed Active Runner"
                )
            usage_lease = resources.enter_context(
                runner_usage_lease(default_runner_lock_path())
            )
            runner_lease_fd = usage_lease.fileno()
            bootstrap_host = SystemdUserExecutorHost(
                runtime_directory=_executor_runtime_directory(),
                environment=dict(os.environ),
                executor_python=Path(sys.executable),
                runner_lease_fd=runner_lease_fd,
            )
            try:
                bootstrap_host.check_readiness()
            except SystemdExecutionReadinessError as error:
                raise ExecutionReadinessError(str(error)) from error
        git = open_workspace(
            parsed.repo, parsed.github_fixture, create=parsed.command == "run"
        )
        state_root = workspace_state_root(git.root)
        if parsed.state_dir and Path(parsed.state_dir).resolve() != state_root.resolve():
            raise ValueError(
                error_message('cli.error.state_dir_mutation')
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
        if parsed.command in {
            "resume",
            "requeue",
            "approve",
            "revise",
            "stop",
            "abandon",
            "configure",
            "config",
            "profile",
        }:
            _resolve_mutation_selection(parsed, git, states)
        if parsed.command == "resume":
            selected_resume = cli_surface._load_local_run(states, parsed.run_id)
            resume_notifications = selected_resume.get("notifications")
            if isinstance(resume_notifications, dict) and resume_notifications.get("enabled") is True:
                resume_feedback = ResumeFeedback(
                    states.root, selected_resume,
                    lambda: states.load_current_run(parsed.run_id),
                )
                resources.callback(resume_feedback.close)
            selected_parent = selected_resume.get("parent")
            selected_parent_number = (
                selected_parent.get("number")
                if isinstance(selected_parent, Mapping)
                else None
            )
            if type(selected_parent_number) is not int:
                raise ValueError("Delivery Run is missing its Parent Issue")
            parsed.parent = selected_parent_number
        if parsed.command in {"configure", "config", "profile"}:
            return _configure_profile(parsed)
        executor_binding = _executor_binding_from_environment()
        if resume_feedback is not None and executor_binding is not None:
            resume_feedback.identity = executor_binding[0]
        executor_backed = parsed.command in {
            "run",
            "resume",
            "requeue",
            "approve",
            "revise",
            "stop",
            "abandon",
        }
        current_run: dict[str, Any] | None = None
        control_record: dict[str, Any] | None = None
        if parsed.command == "run" and executor_binding is None:
            task = _task_for_parent(parsed, github, git)
            control = TaskControlStore(workspace_state_root(git.root))
            try:
                control_record = control.load(task)
            except TaskControlError:
                # The lifecycle admission path can reconstruct an unreadable
                # record from an exact Run receipt and Host observation.
                control_record = None
            current_run = _read_only_run_preflight(
                parsed,
                task,
                states,
                git,
                control_record,
            )
            if current_run is not None and control_record is not None:
                control_record = control.inspect_run(task, current_run)
            control_rejection = _failed_control_action_rejection(
                current_run, control_record
            )
            if control_rejection is not None:
                cli_presentation._print_precondition_failure(
                    control_rejection, as_json=parsed.as_json
                )
                return 2
            if current_run is not None and _ordinary_run_rejects_before_readiness(
                current_run
            ):
                cli_presentation._print_precondition_failure(
                    current_run, as_json=parsed.as_json
                )
                return 2
        read_only_stop_state = (
            _read_only_stop_result(parsed, states, git)
            if parsed.command == "stop" and executor_binding is None
            else None
        )
        if (
            parsed.command == "stop"
            and executor_binding is None
            and read_only_stop_state is None
        ):
            _reject_conflicting_stop_action(parsed, states, git)
        execution_required = executor_backed and read_only_stop_state is None
        if (
            execution_required
            and fixture_path is None
            and executor_binding is None
            and runner_lease_fd is None
        ):
            usage_lease = resources.enter_context(
                runner_usage_lease(default_runner_lock_path())
            )
            runner_lease_fd = usage_lease.fileno()
        if execution_required and fixture_path is None and not _running_active_runner():
            raise ExecutionReadinessError(
                "self-hosting lifecycle commands require an installed Active Runner"
            )
        executor_host: ExecutorHost | None = None
        prepare_executor_session: Callable[[], None] | None = None
        if execution_required and fixture_path is not None:
            executor_host = FakeExecutorHost(separate_process=True)
        elif execution_required and executor_binding is not None:
            executor_host = BoundExecutorHost(
                action_id=executor_binding[0], generation=executor_binding[1]
            )
        elif execution_required:
            systemd_host = bootstrap_host or SystemdUserExecutorHost(
                runtime_directory=_executor_runtime_directory(),
                environment=dict(os.environ),
                executor_python=Path(sys.executable),
                runner_lease_fd=runner_lease_fd,
            )
            try:
                if bootstrap_host is None:
                    systemd_host.check_readiness()
            except SystemdExecutionReadinessError as error:
                raise ExecutionReadinessError(str(error)) from error
            executor_host = systemd_host
            prepare_executor_session = lambda: systemd_host.prepare_environment(
                supplied_arguments
            )
        if not executor_backed and parsed.command not in {"stop", "abandon"}:
            _reject_if_task_action_pending(parsed, states, git)
        if parsed.command in {
            "resume",
            "requeue",
            "approve",
            "revise",
            "stop",
            "abandon",
        } and read_only_stop_state is None:
            _require_profile(profiles, parsed.run_id)
        if (
            read_only_stop_state is None
            and cli_surface._is_lifecycle_action(parsed.command)
        ):
            local_state = cli_surface._load_local_run(states, parsed.run_id)
            if (
                not cli_surface._command_is_ready(local_state, parsed.command)
                and not _lifecycle_action_is_attachable(
                    parsed, github, git, local_state
                )
            ):
                _reject_if_task_action_pending(parsed, states, git)
                cli_presentation._print_precondition_failure(
                    local_state, as_json=parsed.as_json
                )
                return 2
        if parsed.command == "run":
            control_action = (
                control_record.get("action")
                if isinstance(control_record, Mapping)
                else None
            )
            failed_control_action = (
                isinstance(control_action, Mapping)
                and control_action.get("status") == "failed"
                and control_action.get("kind") in {"stop", "abandon"}
                and current_run is not None
                and control_action.get("run_id") == current_run.get("run_id")
            )
            if (
                current_run is not None
                and (
                    current_run.get("status")
                    in {
                        "operator_stopped",
                        "abandonment_pending",
                    }
                    or failed_control_action
                )
            ):
                cli_presentation._print_precondition_failure(
                    current_run, as_json=parsed.as_json
                )
                return 2
        if read_only_stop_state is not None:
            state = read_only_stop_state
            resumed = True
        elif executor_backed and parsed.command in {"stop", "abandon"}:
            command_dispatched = True
            state, lifecycle_receipt = _run_control_action(
                parsed,
                states,
                git,
                github,
                profiles,
                executor_host,
                lifecycle_arguments=tuple(supplied_arguments),
                executor_binding=executor_binding,
                prepare_executor_session=prepare_executor_session,
            )
            if lifecycle_receipt is not None and lifecycle_receipt.status == "failed":
                control_failure = bounded_error(
                    lifecycle_receipt.failure
                    or f"{lifecycle_receipt.kind} Lifecycle Action failed"
                )
            resumed = True
        elif executor_backed:
            if parsed.command == "resume":
                current = cli_surface._load_local_run(states, parsed.run_id)
                # Preserve this exact recovery need before exit reconciliation
                # finishes it. An already fully completed Run cannot resume.
                final_receipt_pending = (
                    current.get("status") == "completed"
                    and has_unfinished_final_receipt(current, git.root)
                )
                if parsed.message is not None:
                    try:
                        parsed.message = _validated_human_response(parsed.message)
                    except ValueError as error:
                        raise TaskControlError(str(error)) from error
                if executor_binding is None:
                    current = _reconcile_resume_exit(
                        parsed, states, git, github, executor_host, current
                    )
                    if (resume_feedback is not None and final_receipt_pending
                            and not has_unfinished_final_receipt(current, git.root)):
                        resume_feedback.progressed(current, "terminal_completion")
                resume_attachable = executor_binding is not None or (
                    _resume_action_is_attachable(parsed, github, git)
                )
                if (
                    not resume_attachable
                    and not final_receipt_pending
                    and not cli_surface._resume_is_ready(current)
                ):
                    _reject_if_task_action_pending(parsed, states, git)
                    if resume_feedback is not None:
                        resume_feedback.failure("")
                    cli_presentation._print_precondition_failure(
                        current, as_json=parsed.as_json
                    )
                    return 2
                if (
                    not resume_attachable
                    and parsed.message is not None
                    and human_blocker_subject_count(current) != 1
                ):
                    if resume_feedback is not None:
                        resume_feedback.failure("")
                    cli_presentation._print_precondition_failure(
                        current, as_json=parsed.as_json
                    )
                    return 2
                if current.get("status") == "supervision_timeout" and (
                    parsed.new_thread or parsed.message is not None
                ):
                    if resume_feedback is not None:
                        resume_feedback.failure("")
                    cli_presentation._print_precondition_failure(
                        current, as_json=parsed.as_json
                    )
                    return 2
                if has_non_invocation_execution_failure(current) and (
                    parsed.new_thread or parsed.message is not None
                ):
                    if resume_feedback is not None:
                        resume_feedback.failure("")
                    cli_presentation._print_precondition_failure(
                        current, as_json=parsed.as_json
                    )
                    return 2
            command_dispatched = True
            state, resumed, lifecycle_receipt = _run_lifecycle(
                parsed,
                states,
                controller,
                git,
                github,
                profiles,
                creation_profile,
                executor_host,
                lifecycle_arguments=tuple(supplied_arguments),
                executor_binding=executor_binding,
                prepare_executor_session=prepare_executor_session,
                resume_feedback=resume_feedback,
            )
        else:  # pragma: no cover - lifecycle commands are Executor-backed
            raise ExecutionReadinessError(error_message('cli.error.missing_executor'))
        if lifecycle_receipt is not None and lifecycle_receipt.status == "failed":
            control_failure = bounded_error(lifecycle_receipt.failure or "操作未完成")
        if resume_feedback is not None and lifecycle_receipt is not None:
            if lifecycle_receipt.attached and resume_feedback.result is None:
                resume_feedback.suppressed = True
            elif lifecycle_receipt.status == "failed":
                resume_feedback.identity = lifecycle_receipt.action_id or resume_feedback.identity
                resume_feedback.failure(lifecycle_receipt.failure or "execution_failed")
        active_ticket_job = state.get("active_ticket_job")
        diagnostics = state.get("diagnostics")
        current_diagnostics = diagnostics if isinstance(diagnostics, list) else []
        if control_failure is not None and not any(
            isinstance(item, Mapping) and item.get("code") == control_failure
            for item in current_diagnostics
        ):
            current_diagnostics = [
                *current_diagnostics,
                {
                    "code": "lifecycle_action_failed",
                    "message": control_failure,
                },
            ]
        no_active_executor = state.get("_control_no_active_executor") is True
        output = {
            "result": (
                "error"
                if control_failure is not None
                else "no_active_executor"
                if no_active_executor
                else "resumed" if resumed else "started"
            ),
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
                else [
                    *current_diagnostics,
                    {
                        "code": "no_active_executor",
                        "message": "当前没有正在运行的 Agent；未创建 Action，Delivery Run 保持不变",
                    },
                ]
                if no_active_executor
                else current_diagnostics
            ),
            "scope_change": state.get("unsupported_scope_change"),
            "delivery_cleanup": cli_presentation._public_delivery_cleanup(state),
            "next_action": cli_presentation._next_action(state),
        }
        if lifecycle_receipt is not None:
            public_receipt = cli_presentation.public_action_receipt(
                lifecycle_receipt,
                repository=state.get("repository"),
                parent=state.get("parent"),
                next_action=human_next_action(
                    output["next_action"], run_id=state.get("run_id")
                ),
            )
            if lifecycle_receipt.resume_intent is not None:
                public_receipt["resume_authorization"] = (
                    lifecycle_receipt.resume_intent["authorization"]
                )
            output["action"] = public_receipt
        if getattr(parsed, "as_json", False):
            if lifecycle_receipt is not None:
                output["action_audit"] = _action_audit(lifecycle_receipt)
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            _print_lifecycle_result(state, output, lifecycle_receipt)
        if precondition_failed or control_failure is not None:
            return 2
        if parsed.command == "stop":
            return 0
        return (
            0
            if _lifecycle_result_succeeded(
                parsed.command,
                state["status"],
                executor_bound=executor_binding is not None,
            )
            else 2
        )
    except (ExecutorAgentInterruptedError, KeyboardInterrupt) as interruption:
        agent_interrupted = isinstance(interruption, ExecutorAgentInterruptedError)
        executor_backed = parsed.command in {
            "run",
            "resume",
            "requeue",
            "approve",
            "revise",
            "stop",
            "abandon",
        }
        interrupt_controller = controller
        interrupt_states = states
        run_id = getattr(parsed, "run_id", None)
        if (
            executor_backed
            and git is not None
            and states is not None
            and github is not None
        ):
            try:
                task = _task_for_parent(parsed, github, git)
                control_record = TaskControlStore(workspace_state_root(git.root)).load(task)
                _require_managed_state(states, git, control_record)
                if isinstance(control_record, Mapping):
                    control_run_id = control_record.get("run_id")
                    if not isinstance(control_run_id, str):
                        action = control_record.get("action")
                        control_run_id = (
                            action.get("run_id")
                            if isinstance(action, Mapping)
                            else None
                        )
                    if isinstance(control_run_id, str):
                        run_id = control_run_id
            except (GitError, GitHubReadError, OSError, TaskControlError, ValueError):
                pass
        if (
            not isinstance(run_id, str)
            and executor_backed
            and interrupt_states is not None
            and github is not None
        ):
            repository = github.repository()
            interrupted = interrupt_states.find_run(
                repository.name_with_owner, parsed.parent
            )
            if isinstance(interrupted, dict):
                run_id = interrupted.get("run_id")
        if (
            not executor_backed
            and interrupt_controller is not None
            and isinstance(run_id, str)
        ):
            interrupt_controller.record_execution_failure(
                run_id, "controller_interrupted"
            )
        durable = (
            interrupt_states.load_run(run_id)
            if interrupt_states is not None and isinstance(run_id, str)
            else None
        )
        interruption_output = {
            "result": "interrupted",
            "run_id": run_id,
            "status": (
                durable.get("status", "unknown")
                if executor_backed and isinstance(durable, dict)
                else "execution_failed"
            ),
            "diagnostics": [
                {
                    "code": (
                        "executor_agent_interrupted"
                        if agent_interrupted
                        else "observation_interrupted"
                        if executor_backed
                        else "controller_interrupted"
                    ),
                    "message": (
                        "Executor 内 Agent Invocation 被中断；已保留可恢复的 Semantic Agent Attempt"
                        if agent_interrupted
                        else "已离开 Lifecycle Action 观察；CLI 未改写 Delivery Run"
                        if executor_backed
                        else "控制器被中断；已保留 Managed Development Checkout 与当前 Semantic Agent Attempt"
                    ),
                }
            ],
            "next_action": (
                cli_presentation._next_action(durable)
                if isinstance(durable, dict)
                else None
            ),
        }
        if getattr(parsed, "as_json", False):
            print(
                json.dumps(
                    interruption_output, ensure_ascii=False, sort_keys=True
                )
            )
        else:
            _print_interruption_result(durable, interruption_output)
        return 130
    except (
        CodexProcessError,
        GitError,
        GitHubReadError,
        OSError,
        ValueError,
        WorkerSandboxError,
    ) as error:
        if resume_feedback is not None:
            resume_feedback.failure(bounded_error(error_detail(error, selected_language(resume_feedback.state))))
        selected_run_id = getattr(parsed, "run_id", None)
        run_id = selected_run_id
        if (
            not isinstance(run_id, str)
            and states is not None
            and git is not None
            and github is not None
        ):
            parent_number = getattr(parsed, "parent", None)
            if type(parent_number) is int and parent_number > 0:
                try:
                    repository = _repository_hint(github, parsed)
                    unfinished = states.find_unfinished_runs(
                        repository, parent_number
                    )
                except (GitHubReadError, OSError, TaskControlError, ValueError):
                    unfinished = []
                if len(unfinished) == 1:
                    durable_run_id = unfinished[0].get("run_id")
                    if isinstance(durable_run_id, str):
                        run_id = durable_run_id
        failure_recorded = False
        incompatible_state = isinstance(error, IncompatibleRunStateError)
        if (
            not incompatible_state
            and not isinstance(error, DeliveryPolicyError)
            and not isinstance(error, DirtyManagedCheckoutError)
            and not isinstance(error, RunLocatorError)
            and not isinstance(error, TaskControlError)
            and not isinstance(
                error, (ExecutionReadinessError, SystemdExecutionReadinessError)
            )
            and controller is not None
            and isinstance(selected_run_id, str)
            and executor_binding is not None
        ):
            if isinstance(error, GitHubReadError) and is_proven_github_state_contradiction(
                error.code
            ):
                failure_recorded = controller.record_deterministic_contradiction(
                    selected_run_id, error.code, error.message
                )
            else:
                failure_recorded = controller.record_execution_failure(
                    selected_run_id, bounded_error(str(error))
                )
        failure_state: dict[str, Any] | None = None
        durable_status = None
        if not incompatible_state and states is not None and isinstance(run_id, str):
            failure_state = states.load_run(run_id)
            if isinstance(failure_state, dict):
                durable_status = failure_state.get("status")
        locator_code = error.code if isinstance(error, RunLocatorError) else None
        locator_error = locator_code is not None
        if locator_code is not None:
            diagnostic_code = locator_code
        elif isinstance(error, GitError) and git is None:
            diagnostic_code = "workspace_required"
        elif isinstance(error, GitHubReadError):
            diagnostic_code = error.code
        elif isinstance(
            error, (ExecutionReadinessError, SystemdExecutionReadinessError)
        ):
            diagnostic_code = "execution_readiness"
        elif isinstance(error, DirtyManagedCheckoutError):
            diagnostic_code = "dirty_managed_checkout"
        elif isinstance(error, ActionBusyError):
            diagnostic_code = "action_busy"
        elif isinstance(error, ExecutorStartUnknownError):
            diagnostic_code = "executor_start_unknown"
        elif isinstance(error, TaskControlError):
            diagnostic_code = "task_control"
        elif incompatible_state:
            diagnostic_code = "incompatible_run_state"
        elif str(error).startswith("multiple unfinished Delivery Runs"):
            diagnostic_code = "multiple_unfinished_runs"
        else:
            diagnostic_code = "command_failed"

        if (
            locator_error
            or isinstance(
                error, (ExecutionReadinessError, SystemdExecutionReadinessError)
            )
            or isinstance(error, DirtyManagedCheckoutError)
            or diagnostic_code
            in {"action_busy", "executor_start_unknown", "task_control"}
        ):
            diagnostic_message = bounded_error(str(error))
        elif diagnostic_code == "workspace_required":
            diagnostic_message = (
                "无法定位当前 Git 仓库；请进入目标仓库目录后重试。"
                f"原因：{bounded_error(str(error))}"
            )
        elif diagnostic_code == "incompatible_run_state":
            diagnostic_message = (
                "本地 Run state 不符合当前唯一 Invocation/Generation 契约；"
                "不会迁移、兼容读取或执行任何 mutation，请重新创建或清理该 Run"
            )
        elif diagnostic_code == "multiple_unfinished_runs":
            diagnostic_message = (
                "同一父 Issue 存在多个未终止交付运行；候选运行："
                f"{str(error).partition(': ')[2]}。请先人工确定要保留的运行"
            )
        else:
            diagnostic_message = (
                f"{bounded_error(str(error)) or type(error).__name__}；"
                "请排除上述原因后重试；已有交付请先查询 status 确认状态。"
            )
        # Show the adapter's summary, not its multiline response/body. Keep the
        # recovery instruction separate so truncation cannot remove it.
        diagnostic_message = bounded_error(diagnostic_message)
        if not locator_error:
            diagnostic_message = (
                diagnostic_message.splitlines() or [type(error).__name__]
            )[0]
        diagnostic_next_action = (
            "请进入目标 Git 仓库目录后重试"
            if diagnostic_code == "workspace_required"
            else "请排除上述原因；已有交付先运行 agent-run status 确认状态，再决定是否重试"
        )
        locator_diagnostic: dict[str, object] = {
            "code": diagnostic_code,
            "message": diagnostic_message,
        }
        if parsed.command in {"run", "resume", "stop", "abandon", "approve", "revise", "requeue"}:
            locator_diagnostic["operation"] = parsed.command
            locator_diagnostic["application_status"] = (
                "unknown" if command_dispatched else "not_applied"
            )
            locator_diagnostic["next_action"] = diagnostic_next_action
        if isinstance(error, RunLocatorError):
            locator_diagnostic["candidates"] = error.candidates
        error_status = (
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
        )
        # This command's error must not be replaced by an older Run failure.
        output_diagnostics = [locator_diagnostic]
        if getattr(parsed, "as_json", False):
            print(
                json.dumps(
                    {
                        "result": "error",
                        "status": error_status,
                        "diagnostics": output_diagnostics,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            display_state = failure_state
            if display_state is None:
                possible_state = locals().get("state")
                if isinstance(possible_state, dict) and "language" in possible_state:
                    display_state = possible_state
            valid_display_state = (
                display_state is not None and display_state.get("language") in {"zh", "en"}
            )
            language = selected_language(display_state) if valid_display_state else personal_language()
            if not valid_display_state and getattr(parsed, "display_language", None) in {"zh", "en"}:
                language = parsed.display_language
            localized_detail = bounded_error(error_detail(error, language))
            if not locator_error:
                localized_detail = (localized_detail.splitlines() or [type(error).__name__])[0]
            human_message = _human_failure_reason(
                error, localized_detail, language=language
            )
            if diagnostic_code in {"workspace_required", "incompatible_run_state"}:
                human_message = cli_message(f"cli.error.{diagnostic_code}", language=language,
                                            reason=localized_detail)
            print(cli_message("cli.command_unknown" if command_dispatched else "cli.command_not_applied",
                              language=language, status=cli_presentation.human_delivery_status(
                                  error_status, language=language)))
            print(cli_message("cli.reason", language=language, reason=bounded_error(human_message)[:600]))
            print(cli_message("cli.command_next_action", language=language,
                              action=cli_message("cli.retry_workspace" if diagnostic_code == "workspace_required"
                                                 else "cli.retry_command", language=language)))
            if failure_state is not None and valid_display_state:
                _print_lifecycle_result(failure_state, {}, None)
            if isinstance(error, RunLocatorError) and error.candidates:
                print(cli_message("cli.delivery_candidates", language=language))
                for candidate in error.candidates:
                    candidate_status = cli_presentation.human_delivery_status(
                        candidate.get("status") or "unknown", language=language
                    )
                    print(
                        f"- repository={candidate.get('repository') or 'unknown'} "
                        f"Parent=#{candidate.get('parent') or 'unknown'} "
                        f"status={candidate_status}"
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


def _executor_binding_from_environment() -> tuple[str, int] | None:
    action_id = os.environ.get("AGENT_RUN_EXECUTOR_ACTION_ID")
    raw_generation = os.environ.get("AGENT_RUN_EXECUTOR_GENERATION")
    if action_id is None and raw_generation is None:
        return None
    if not action_id or raw_generation is None:
        raise ExecutionReadinessError(error_message('cli.error.executor_binding'))
    try:
        generation = int(raw_generation)
    except ValueError as error:
        raise ExecutionReadinessError(error_message('cli.error.executor_generation')) from error
    if generation <= 0:
        raise ExecutionReadinessError(error_message('cli.error.executor_generation'))
    return action_id, generation


def _executor_runtime_directory() -> Path:
    return default_executor_runtime_directory()


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


def _repository_hint(github: Any, parsed: argparse.Namespace) -> str:
    hint_reader = getattr(github, "repository_hint", None)
    hinted = hint_reader() if callable(hint_reader) else None
    if isinstance(hinted, str) and hinted:
        return hinted
    explicit = getattr(parsed, "repo", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    repository = github.repository()
    name = getattr(repository, "name_with_owner", None)
    if not isinstance(name, str) or not name:
        raise TaskControlError("GitHub repository identity is unavailable")
    return name


def _print_lifecycle_result(
    state: Mapping[str, Any],
    output: Mapping[str, Any],
    receipt: ActionReceipt | None,
) -> None:
    """Render the ordinary mutation result without machine-only identities."""

    language = str(state.get("language", "zh"))
    def message(key: str, **values: object) -> str:
        return text(f"cli.{key}", language=language, **values)

    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else "?"
    print(cli_message("cli.repository", language=str(state.get("language", "zh")), repository=state.get("repository")))
    print(cli_message("cli.parent_issue", language=str(state.get("language", "zh")), parent=parent_number))
    if state.get("_control_no_active_executor") is True:
        print(message("no_active_agent"))
    if receipt is not None:
        final_approval = is_final_approval_action({"kind": receipt.kind}, state)
        print(message("operation", operation=receipt.kind))
        if (
            receipt.resume_intent is not None
            and receipt.resume_intent.get("authorization") == "new_budget_window"
        ):
            print(message("resume_authorized"))
        print(message("attached" if receipt.attached else "accepted"))
        if receipt.status == "failed":
            print(message("approval_failed" if final_approval else "application_failed"))
            if receipt.failure:
                print(message("failure_reason", reason=receipt.failure))
        elif receipt.status in {"completed", "executor_active"}:
            print(message("delivery_completed_action" if final_approval else "applied"))
        else:
            print(message("approval_running" if final_approval else "applying"))
        if receipt.executor_status in {"exited", "absent"}:
            print(message("executor_absent"))
        elif receipt.attached:
            print(message("executor_existing"))
        else:
            print(message("executor_started"))
    status = state.get("status")
    if status == "completed":
        print(message("cleanup_pending" if final_approval_cleanup_pending(state)
                      else "delivery_completed"))
    elif status == "abandoned":
        print(message("delivery_abandoned"))
    else:
        print(message("delivery_incomplete", status=cli_presentation.human_delivery_status(
            status, language=language)))
    print(message("next_action", action=cli_presentation.human_next_action_for_state(state)))


def _print_interruption_result(
    state: Mapping[str, Any] | None, output: Mapping[str, Any]
) -> None:
    """Render an interrupted observation without exposing machine identities."""

    language = str(state.get("language", "zh")) if state is not None else personal_language()
    def message(key: str, **values: object) -> str:
        return text(f"cli.{key}", language=language, **values)

    if isinstance(state, Mapping):
        parent = state.get("parent")
        parent_number = parent.get("number") if isinstance(parent, Mapping) else "?"
        print(cli_message("cli.repository", language=str(state.get("language", "zh")), repository=state.get("repository")))
        print(cli_message("cli.parent_issue", language=str(state.get("language", "zh")), parent=parent_number))
    # The structured diagnostic remains the original audit fact. Human copy is
    # selected from the same interruption cause without rewriting that record.
    diagnostics = output.get("diagnostics")
    first = diagnostics[0] if isinstance(diagnostics, list) and diagnostics else None
    code = first.get("code") if isinstance(first, Mapping) else None
    print(message("interrupted"))
    if code in {"executor_agent_interrupted", "observation_interrupted", "controller_interrupted"}:
        print(message("interruption." + str(code)))
    if isinstance(state, Mapping) and has_final_approval(state):
        print(message("approval_observation_stopped"))
    print(message("interrupted_delivery", status=cli_presentation.human_delivery_status(
        output.get("status"), language=language)))
    next_action = (
        cli_presentation.human_next_action_for_state(state)
        if isinstance(state, Mapping)
        else message("query_status")
    )
    print(message("next_action", action=next_action))


def _action_audit(receipt: ActionReceipt) -> dict[str, object]:
    """Expose stable machine identities only through explicit JSON output."""

    return {
        "action_id": receipt.action_id,
        "kind": receipt.kind,
        "run_id": receipt.run_id,
        "status": receipt.status,
        "attached": receipt.attached,
        "executor_status": receipt.executor_status,
        "executor_generation": receipt.executor_generation,
        "handshake": receipt.handshake,
        "payload_digest": receipt.payload_digest,
        "failure": receipt.failure,
        "resume_intent": receipt.resume_intent,
    }


def _task_for_parent(
    parsed: argparse.Namespace,
    github: Any,
    git: GitRepository,
) -> TaskKey:
    parent = getattr(parsed, "parent", None)
    if type(parent) is not int or parent <= 0:
        raise TaskControlError("Lifecycle Parent Issue must be a positive integer")
    return TaskKey(git.root, _repository_hint(github, parsed), parent)


def _read_only_stop_result(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
) -> dict[str, Any] | None:
    """Return a proven read-only Stop result before execution readiness checks."""

    state = cli_surface._load_local_run(states, parsed.run_id)
    if state.get("status") not in {"completed", "abandoned", "operator_stopped"}:
        return None
    repository = state.get("repository")
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    if not isinstance(repository, str) or type(parent_number) is not int:
        raise TaskControlError(error_message('cli.error.task_identity'))
    task = TaskKey(git.root, repository, parent_number)
    control = TaskControlStore(workspace_state_root(git.root))
    try:
        if not control.proves_read_only_stop(task, state):
            return None
    except TaskControlError:
        return None
    result = dict(state)
    if state.get("status") not in {"completed", "abandoned", "operator_stopped"}:
        result["_control_no_active_executor"] = True
    return result


def _reject_conflicting_stop_action(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
) -> None:
    """Reject a different pending Action before acquiring Runner resources."""

    state = cli_surface._load_local_run(states, parsed.run_id)
    repository = state.get("repository")
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    if not isinstance(repository, str) or type(parent_number) is not int:
        raise TaskControlError(error_message('cli.error.task_identity'))
    task = TaskKey(git.root, repository, parent_number)
    try:
        record = TaskControlStore(workspace_state_root(git.root)).load(task)
    except TaskControlError:
        return
    action = record.get("action") if isinstance(record, Mapping) else None
    if not isinstance(action, Mapping) or action.get("status") not in {
        "accepted",
        "applying",
    }:
        return
    executor = record.get("executor") if isinstance(record, Mapping) else None
    if (
        isinstance(executor, Mapping)
        and executor.get("binding_token") == "reconciliation-required"
        and action_receipt_matches(state, action)
    ):
        return
    if action.get("kind") == "stop" and _mutation_request_matches_action(
        parsed, action, parsed.run_id
    ):
        return
    raise ActionBusyError(
        final_approval_busy_message(action, state),
        action=action,
    )


def _preflight_run(
    task: TaskKey, states: StateStore | FaultInjectingStateStore
) -> dict[str, Any] | None:
    unfinished = states.find_unfinished_runs(task.repository, task.parent_number)
    if len(unfinished) > 1:
        run_ids = ", ".join(str(state.get("run_id")) for state in unfinished)
        raise ValueError(
            "multiple unfinished Delivery Runs exist for this Parent Issue: "
            f"{run_ids}"
        )
    current = unfinished[0] if unfinished else None
    if current is not None:
        require_current_run_state(current)
    return current


def _read_only_run_preflight(
    parsed: argparse.Namespace,
    task: TaskKey,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
    control_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Locate one existing Run without readiness probes or reconciliation writes."""

    _require_managed_state(states, git, control_record)
    _require_managed_locator(task, states.root.resolve())
    return _preflight_run(task, states)


def _lifecycle_result_succeeded(
    command: str, status: object, *, executor_bound: bool = False
) -> bool:
    """Classify command completion without weakening public ``run`` gates."""

    if status in _SUCCESSFUL_FOREGROUND_STATUSES:
        return True
    return status == "publication_pending" and (
        command == "publish-run" or (command == "run" and executor_bound)
    )


def _require_managed_locator(task: TaskKey, requested_root: Path) -> None:
    """Reject historical bindings that would escape the managed state directory."""

    for entry in RunLocatorIndex.default().entries():
        if Path(entry["repository_root"]).resolve() != task.workspace:
            continue
        if entry.get("parent_number") not in (None, task.parent_number):
            continue
        if Path(entry["state_dir"]).resolve() != requested_root:
            raise TaskControlError(error_message('cli.error.task_index_directory'))


def _run_action_payload(
    parsed: argparse.Namespace,
    policy: DeliveryPolicy,
    creation_profile: tuple[str | None, ProfileOverrides] | None,
) -> dict[str, Any]:
    preset, overrides = creation_profile or (None, {})
    profile_overrides = {
        key: value
        for key, value in overrides.items()
        if value is not None and value is not False
    }
    return {
        "parent": parsed.parent,
        "policy": policy.snapshot(),
        "profile": {
            "preset": preset,
            "overrides": profile_overrides,
        },
    }


def _resume_action_payload(
    parsed: argparse.Namespace,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    budget_checkpoint_resume = (
        cli_surface._review_budget_checkpoint_count(dict(current)) == 1
    )
    explicit_policy = _policy_overrides(parsed)
    if explicit_policy and not budget_checkpoint_resume:
        raise DeliveryPolicyError(
            "ordinary resume does not accept Delivery Policy overrides; "
            "policy can change only when opening a new Budget Window"
        )
    policy = resolve_delivery_policy(
        user_defaults=parse_policy_snapshot(policy_snapshot_for_state(current)),
        command_overrides=explicit_policy,
    )
    return {
        "parent": parsed.parent,
        "run_id": parsed.run_id,
        "new_thread": bool(parsed.new_thread),
        "message": parsed.message,
        "resume_budget_checkpoint": budget_checkpoint_resume,
        "resume_intent": bind_resume_intent(current),
        "policy": policy.snapshot(),
    }


def _resume_payload_for_existing_action(
    parsed: argparse.Namespace,
    existing_payload: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    semantic_fields = {
        "parent": parsed.parent,
        "run_id": parsed.run_id,
        "new_thread": bool(parsed.new_thread),
        "message": parsed.message,
    }
    semantic_match = all(
        existing_payload.get(key) == value for key, value in semantic_fields.items()
    )
    if semantic_match and _resume_policy_matches_existing_action(
        parsed, existing_payload
    ):
        return dict(existing_payload)
    return _resume_action_payload(parsed, current)


def _resume_policy_matches_existing_action(
    parsed: argparse.Namespace,
    existing_payload: Mapping[str, Any],
) -> bool:
    """Compare explicit Resume policy fields against its frozen first payload."""

    explicit_policy = _policy_overrides(parsed)
    if not explicit_policy:
        return True
    if existing_payload.get("resume_budget_checkpoint") is not True:
        return False
    stored_policy = existing_payload.get("policy")
    if not isinstance(stored_policy, Mapping):
        return False
    frozen_policy = parse_policy_snapshot(stored_policy)
    requested_policy = resolve_delivery_policy(
        user_defaults=frozen_policy,
        command_overrides=explicit_policy,
    )
    return requested_policy.snapshot() == frozen_policy.snapshot()


def _mutation_action_payload(
    parsed: argparse.Namespace, current: Mapping[str, Any]
) -> dict[str, Any]:
    run_id = current.get("run_id")
    if not isinstance(run_id, str) or run_id != parsed.run_id:
        raise TaskControlError(
            error_message('cli.error.selector_mismatch' ,value0=parsed.command)
        )
    payload: dict[str, Any] = {
        "parent": parsed.parent,
        "run_id": run_id,
    }
    if parsed.command == "revise":
        message = parsed.message.strip()
        if not message:
            raise TaskControlError("revision feedback must be non-empty")
        payload["message"] = message
    return payload


def _mutation_payload_for_existing_action(
    parsed: argparse.Namespace,
    existing_payload: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    requested = _mutation_action_payload(parsed, current)
    return dict(existing_payload) if dict(existing_payload) == requested else requested


def _run_control_action(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
    github: FixtureGitHubReader | GhGitHubReader,
    profiles: AgentProfileStore,
    host: ExecutorHost | None,
    *,
    lifecycle_arguments: tuple[str, ...] = (),
    executor_binding: tuple[str, int] | None = None,
    prepare_executor_session: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], ActionReceipt | None]:
    """Submit Stop/Abandon and observe its independently hosted Executor."""

    kind = parsed.command
    if kind not in {"stop", "abandon"}:
        raise TaskControlError("unsupported control action")
    if host is None:
        raise ExecutionReadinessError(
            error_message('cli.error.execution_readiness')
        )
    current = states.load_current_run(parsed.run_id)
    if current is None:
        raise TaskControlError(error_message('cli.error.action_run_missing' ,value0=kind))
    run_id = current.get("run_id")
    if not isinstance(run_id, str) or run_id != parsed.run_id:
        raise TaskControlError(error_message('cli.error.selector_mismatch' ,value0=kind))
    task = _task_for_parent(parsed, github, git)
    control = TaskControlStore(workspace_state_root(git.root))
    try:
        existing = control.load(task)
    except TaskControlError:
        existing = None
    _require_managed_state(states, git, existing)
    payload: dict[str, Any] = {"parent": parsed.parent, "run_id": run_id}
    if kind == "abandon":
        payload["discard_worktree"] = bool(parsed.discard_worktree)
        existing_action = (
            existing.get("action") if isinstance(existing, Mapping) else None
        )
        existing_executor = (
            existing.get("executor") if isinstance(existing, Mapping) else None
        )
        if (
            not parsed.discard_worktree
            and not (
                isinstance(existing_action, Mapping)
                and existing_action.get("status") in {"accepted", "applying"}
            )
            and not (
                isinstance(existing_executor, Mapping)
                and existing_executor.get("status") in {"starting", "running"}
            )
        ):
            # Deterministic local authorization is checked before occupying
            # the Action slot when no old Executor needs fencing.
            require_clean_run_worktrees(git, states, run_id)

    def execute_control(
        bound_run_id: str, action_id: str, generation: int
    ) -> Mapping[str, Any]:
        control.assert_executor_current(
            task,
            action_id=action_id,
            generation=generation,
            run_id=bound_run_id,
        )
        record = control.snapshot(task, action_id)
        action = record.get("action") if isinstance(record, Mapping) else None
        target = (
            action.get("target_executor")
            if isinstance(action, Mapping)
            and isinstance(action.get("target_executor"), Mapping)
            else None
        )
        _require_managed_state(states, git, record)
        bound_states = states

        def assert_current() -> None:
            control.assert_executor_current(
                task,
                action_id=action_id,
                generation=generation,
                run_id=bound_run_id,
            )

        bound_states._set_write_guard(
            assert_current,
            transaction=lambda: control._executor_current_transaction(
                task,
                action_id=action_id,
                generation=generation,
                run_id=bound_run_id,
            ),
        )
        bound_profiles = profiles
        bound_controller = Controller(
            github,
            git,
            bound_states,
            profiles=bound_profiles,
        )
        delivery_executor = DeliveryExecutor(
            states=bound_states,
            driver_factory=lambda run_states, _binding: _run_driver(
                parsed,
                run_states,
                bound_controller,
                git,
                github,
                bound_profiles,
                before_external_step=assert_current,
                executor_host=host,
            ),
            record_execution_failure=(
                lambda _states, selected_run_id, message, _binding: (
                    bound_controller.record_execution_failure(
                        selected_run_id, bounded_error(message)
                    )
                )
            ),
        )
        return delivery_executor.execute(
            bound_run_id,
            action_id=action_id,
            generation=generation,
            control_operation=ControlRunOperation(
                kind=kind,
                target_executor=target,
                discard_worktree=bool(
                    getattr(parsed, "discard_worktree", False)
                ),
            ),
        )

    lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: (current, True),
        initialize_profile=None,
        executor_spec=lambda bound_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=bound_run_id,
            generation=generation,
            command=lifecycle_arguments,
            cwd=git.root,
            state_root=states.root.resolve(),
        ),
        execute=lambda _run_id: current,
        execute_with_binding=execute_control,
        prepare_executor_session=prepare_executor_session,
    )
    receipt: ActionReceipt | None
    try:
        if executor_binding is not None:
            state, _resumed, receipt = lifecycle.execute_claimed(
                action_id=executor_binding[0], generation=executor_binding[1]
            )
        else:
            state, _resumed, receipt = lifecycle.submit_control(
                LifecycleRequest(task=task, kind=kind, payload=payload),
            )
    except ExecutorHostError as error:
        failed = control.load(task)
        failed_action = failed.get("action") if isinstance(failed, Mapping) else None
        raw_failure = (
            failed_action.get("failure")
            if isinstance(failed_action, Mapping)
            else None
        )
        failure = raw_failure if isinstance(raw_failure, str) else str(error)
        if kind == "abandon" and any(
            marker in failure.lower()
            for marker in ("dirty", "tracked modifications", "untracked files")
        ):
            raise DirtyManagedCheckoutError(failure) from error
        raise
    if (
        receipt is None
        and kind == "stop"
        and state.get("status")
        not in {"completed", "abandoned", "operator_stopped"}
    ):
        state = dict(state)
        state["_control_no_active_executor"] = True
    return state, receipt


def _ordinary_run_requires_explicit_action(current: Mapping[str, Any]) -> bool:
    """Whether only a dedicated operator command may leave this boundary."""

    state = dict(current)
    if has_final_approval(state) and state.get("status") not in {"completed", "abandoned"}:
        return True
    if state.get("status") == "supervision_timeout":
        return has_local_operator_gate(state)
    return has_run_operator_gate(state)


def _failed_control_action_rejection(
    current: Mapping[str, Any] | None,
    control_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an accurate read-only rejection for an unresolved control target."""

    if current is None or control_record is None:
        return None
    action = control_record.get("action")
    if not isinstance(action, Mapping) or unresolved_control_target(action) is None:
        return None
    if action.get("run_id") != current.get("run_id"):
        return None
    failure = action.get("failure")
    message = (
        failure
        if isinstance(failure, str) and failure
        else "失败 Control Action 的目标 ownership 尚未收口"
    )
    rejected = dict(current)
    diagnostics = rejected.get("diagnostics")
    current_diagnostics = list(diagnostics) if isinstance(diagnostics, list) else []
    current_diagnostics.append(
        {"code": "lifecycle_action_failed", "message": message}
    )
    rejected["diagnostics"] = current_diagnostics
    return rejected


def _ordinary_run_rejects_before_readiness(current: Mapping[str, Any]) -> bool:
    """Reject locally final gates before acquiring execution resources."""

    if current.get("status") in {
        "run_approval_pending",
        "parent_approval_pending",
    }:
        return False
    return _ordinary_run_requires_explicit_action(current)


def _run_lifecycle(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    controller: Controller,
    git: GitRepository,
    github: FixtureGitHubReader | GhGitHubReader,
    profiles: AgentProfileStore,
    creation_profile: tuple[str | None, ProfileOverrides] | None,
    host: ExecutorHost | None,
    *,
    lifecycle_arguments: tuple[str, ...] = (),
    executor_binding: tuple[str, int] | None = None,
    prepare_executor_session: Callable[[], None] | None = None,
    resume_feedback: ResumeFeedback | None = None,
) -> tuple[dict[str, Any], bool, ActionReceipt | None]:
    if host is None:
        raise ExecutionReadinessError(
            error_message('cli.error.execution_readiness')
        )
    action_kind = parsed.command
    if action_kind not in {"run", "resume", "approve", "revise", "requeue"}:
        raise ValueError("unsupported Executor-backed lifecycle action")
    task = _task_for_parent(parsed, github, git)
    # Every user checkout for this remote shares one managed admission domain.
    control = TaskControlStore(workspace_state_root(git.root))
    current = (
        _preflight_run(task, states)
        if action_kind == "run"
        else states.load_current_run(parsed.run_id)
    )
    if action_kind != "run" and current is None:
        raise TaskControlError(error_message('cli.error.action_run_missing' ,value0=action_kind))
    if current is not None:
        controller._require_current_checkout(current)
    try:
        existing_control = control.load(task)
    except TaskControlError:
        # Reconciliation below may replace a corrupt record from an exact Run
        # receipt.  Do not let this read-only inspection mask that path.
        existing_control = None
    _require_managed_state(states, git, existing_control)
    if action_kind == "run":
        _require_managed_locator(task, states.root.resolve())
    if existing_control is not None:
        existing_control = _reconcile_existing_control(
            control, task, current, state_dir=states.root
        )
    if current is not None and current.get("action_application_receipt") is None:
        existing_control = _reconcile_existing_control(
            control, task, current, state_dir=states.root
        )
    existing_action = (
        existing_control.get("action") if isinstance(existing_control, dict) else None
    )
    existing_executor = (
        existing_control.get("executor")
        if isinstance(existing_control, dict)
        else None
    )
    preflight_override = current
    if (
        action_kind == "run"
        and current is not None
        and current.get("status")
        in {"run_approval_pending", "parent_approval_pending"}
        and not _policy_overrides(parsed)
        and not _profile_options_are_explicit(creation_profile)
        and (
            (
                isinstance(existing_action, Mapping)
                and existing_action.get("kind") == "run"
                and existing_action.get("status") in {"completed", "failed"}
                and action_receipt_matches(current, existing_action)
            )
            or (
                # Direct engine seams predate Lifecycle Action records.  They
                # still need bounded approval refresh, never a new Action.
                existing_action is None
                and current.get("action_application_receipt") is None
            )
        )
    ):
        # An unchanged settled Run Action remains idempotent, but approval
        # replay must first observe bounded GitHub readback so scope drift and
        # transient graph-read lag are not hidden behind the old Receipt.
        refreshed = current
        for _ in range(MAX_READ_ATTEMPTS + 1):
            refreshed = controller._refresh(current, task.parent_number)
            if not is_github_refresh_wait(refreshed):
                break
        if is_github_refresh_wait(refreshed):
            return refreshed, True, None
        if refreshed.get("status") != current.get("status"):
            current = refreshed
            preflight_override = refreshed
    if (
        action_kind == "run"
        and current is not None
        and _ordinary_run_requires_explicit_action(current)
    ):
        receipt = current.get("action_application_receipt")
        original_action_is_active = (
            isinstance(receipt, Mapping)
            and isinstance(existing_action, Mapping)
            and existing_action.get("status") in {"accepted", "applying"}
            and action_receipt_matches(current, existing_action)
        )
        original_action_is_terminal_replay = (
            current.get("status")
            in {
                "ready_for_human",
                "run_approval_pending",
                "parent_approval_pending",
            }
            and isinstance(receipt, Mapping)
            and isinstance(existing_action, Mapping)
            and existing_action.get("kind") == "run"
            and existing_action.get("status") in {"completed", "failed"}
            and action_receipt_matches(current, existing_action)
            and not _policy_overrides(parsed)
            and not _profile_options_are_explicit(creation_profile)
        )
        original_action_needs_reconciliation = (
            current.get("status")
            in {
                "ready_for_human",
                "run_approval_pending",
                "parent_approval_pending",
            }
            and isinstance(receipt, Mapping)
            and receipt.get("kind") == "run"
            and existing_action is None
            and not _policy_overrides(parsed)
            and not _profile_options_are_explicit(creation_profile)
        )
        if not (
            original_action_is_active
            or original_action_is_terminal_replay
            or original_action_needs_reconciliation
        ):
            # These boundaries require their dedicated Resume, approval, or
            # revision command.  Preserve the repository-binding probe without
            # admitting a successor Action or preparing a new Executor session,
            # even when run carries explicit Profile options.
            repository = github.repository()
            if not same_repository(repository.name_with_owner, task.repository):
                raise TaskControlError(
                    "configured GitHub repository does not match the Delivery Run"
                )
            return current, True, None
    if (
        isinstance(existing_action, dict)
        and (
            existing_action.get("status") in {"accepted", "applying"}
            or (
                action_kind == "resume"
                and isinstance(existing_executor, Mapping)
                and existing_executor.get("status") in {"starting", "running"}
                and existing_executor.get("action_id")
                == existing_action.get("action_id")
            )
        )
        and isinstance(existing_action.get("payload"), dict)
    ):
        if action_kind == "resume":
            if current is None:
                raise TaskControlError(error_message('cli.error.resume_run_missing'))
            payload = _resume_payload_for_existing_action(
                parsed, existing_action["payload"], current
            )
        elif action_kind == "run":
            payload = _run_payload_for_existing_action(
                parsed,
                existing_action["payload"],
                current,
                creation_profile,
            )
        else:
            if current is None:  # pragma: no cover - guarded above
                raise TaskControlError(error_message('cli.error.action_run_missing' ,value0=action_kind))
            payload = _mutation_payload_for_existing_action(
                parsed, existing_action["payload"], current
            )
    else:
        if action_kind == "resume":
            if current is None or current.get("run_id") != parsed.run_id:
                raise TaskControlError(error_message('cli.error.resume_selector_mismatch'))
            payload = _resume_action_payload(parsed, current)
        elif action_kind == "run":
            if current is None:
                defaults = UserDefaultsStore()
                document = defaults.load()
                policy = resolve_delivery_policy(
                    user_defaults=document.get("policy"),
                    command_overrides=_policy_overrides(parsed),
                )
                preset, overrides = creation_profile or (None, {})
                resolved_creation: tuple[str | None, ProfileOverrides] | None = defaults.resolve_creation(
                    preset=preset, overrides=overrides, document=document,
                )
                payload = _run_action_payload(parsed, policy, resolved_creation)
                payload["language"] = defaults.language(document)
                payload["notifications"] = notification_snapshot(
                    document.get("notifications"), disabled=parsed.no_notifications, mode=parsed.notification_mode,
                )
            else:
                frozen_creation = current.get("creation_configuration")
                if isinstance(frozen_creation, dict):
                    payload = _run_payload_for_existing_action(
                        parsed, frozen_creation, current, creation_profile,
                    )
                else:
                    policy = parse_policy_snapshot(policy_snapshot_for_state(current))
                    payload = _run_action_payload(parsed, policy, creation_profile)
        else:
            if current is None:  # pragma: no cover - guarded above
                raise TaskControlError(error_message('cli.error.action_run_missing' ,value0=action_kind))
            payload = _mutation_action_payload(parsed, current)


    def current_executor_binding() -> tuple[str, int, str | None]:
        record = control.load(task)
        if not isinstance(record, Mapping):
            raise TaskControlError(error_message('cli.error.executor_record_missing'))
        action_id: str | None
        generation: int | None
        if executor_binding is not None:
            action_id, generation = executor_binding
        else:
            action = record.get("action")
            executor = record.get("executor")
            action_id = (
                action.get("action_id")
                if isinstance(action, Mapping)
                else None
            )
            generation = (
                executor.get("generation")
                if isinstance(executor, Mapping)
                else None
            )
        if not isinstance(action_id, str) or type(generation) is not int:
            raise TaskControlError(error_message('cli.error.executor_ownership'))
        executor = record.get("executor")
        run_id = (
            executor.get("run_id")
            if isinstance(executor, Mapping)
            and isinstance(executor.get("run_id"), str)
            else None
        )
        return action_id, generation, run_id

    def assert_executor_current() -> None:
        action_id, generation, run_id = current_executor_binding()
        control.assert_executor_current(
            task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )

    def executor_write_transaction() -> Any:
        action_id, generation, run_id = current_executor_binding()
        return control._executor_current_transaction(
            task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )

    def executor_guards(
        binding: tuple[str, int, str] | None,
    ) -> tuple[Callable[[], None], Callable[[], Any]]:
        if binding is None:
            return assert_executor_current, executor_write_transaction
        action_id, generation, run_id = binding

        def assert_bound_executor_current() -> None:
            control.assert_executor_current(
                task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )

        def bound_executor_write_transaction() -> Any:
            return control._executor_current_transaction(
                task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )

        return assert_bound_executor_current, bound_executor_write_transaction

    states._set_write_guard(
        assert_executor_current, transaction=executor_write_transaction
    )

    def lifecycle_state_store(
        binding: tuple[str, int, str] | None,
    ) -> StateStore | FaultInjectingStateStore:
        current_control = (
            control.snapshot(task, binding[0])
            if binding is not None
            else control.load(task)
        )
        _require_managed_state(states, git, current_control)
        return states

    def lifecycle_components(
        run_states: StateStore,
        binding: tuple[str, int, str] | None,
    ) -> tuple[AgentProfileStore, Controller]:
        fence, transaction = executor_guards(binding)
        run_states._set_write_guard(
            fence, transaction=transaction
        )
        return profiles, controller

    def lifecycle_driver_factory(
        run_states: StateStore,
        binding: tuple[str, int, str] | None,
    ) -> RunDriver:
        run_profiles, run_controller = lifecycle_components(run_states, binding)
        fence, _ = executor_guards(binding)
        resume_retry: Callable[
            [str], tuple[dict[str, Any], bool]
        ] | None = None
        if action_kind == "resume":
            policy_snapshot = payload.get("policy")
            if not isinstance(policy_snapshot, Mapping):
                raise TaskControlError(error_message('cli.error.resume_policy_missing'))
            resume_policy = parse_policy_snapshot(policy_snapshot)
            retry_intent = payload.get("resume_intent")

            def retry_resume_refresh(run_id: str) -> tuple[dict[str, Any], bool]:
                return run_controller.resume(
                    run_id,
                    resume_human_blocker=(
                        isinstance(retry_intent, Mapping)
                        and retry_intent.get("authorization")
                        == "human_response"
                    ),
                    new_thread=payload.get("new_thread") is True,
                    human_response=(
                        payload.get("message")
                        if isinstance(payload.get("message"), str)
                        else None
                    ),
                    explicit_resume=True,
                    record_explicit_resume_audit=False,
                    resume_budget_checkpoint=(
                        payload.get("resume_budget_checkpoint") is True
                    ),
                    budget_policy=resume_policy,
                    validate_resume_state=lambda state: validate_resume_intent(
                        state, retry_intent
                    ),
                )

            resume_retry = retry_resume_refresh

        worker_started: Callable[[int], None] | None = None
        worker_finished: Callable[[int], None] | None = None
        if binding is not None:
            binding_action_id, binding_generation, _binding_run_id = binding
            worker_tokens: dict[int, str] = {}

            def worker_started(pid: int) -> None:
                token = _process_start_token(pid)
                if token is None:
                    raise TaskControlError(error_message('cli.error.worker_binding'))
                worker_tokens[pid] = token
                control.mark_worker_started(
                    task,
                    action_id=binding_action_id,
                    generation=binding_generation,
                    pid=pid,
                    process_start_token=token,
                )

            def worker_finished(pid: int) -> None:
                token = worker_tokens.pop(pid, None)
                if token is None:
                    return
                try:
                    control.mark_worker_finished(
                        task,
                        action_id=binding_action_id,
                        generation=binding_generation,
                        pid=pid,
                        process_start_token=token,
                    )
                except TaskControlError:
                    # A Stop/Abandon fence may already have revoked this
                    # Executor.  The old Worker must not overwrite that fence.
                    return

        driver = _run_driver(
            parsed,
            run_states,
            run_controller,
            git,
            github,
            run_profiles,
            before_external_step=fence,
            resume_pending_refresh=resume_retry,
            use_current_state_once=action_kind == "resume",
            on_worker_started=worker_started,
            on_worker_finished=worker_finished,
        )
        if resume_feedback is not None:
            driver.on_outcome = resume_feedback.progressed
        return driver

    def record_lifecycle_failure(
        run_states: StateStore,
        run_id: str,
        message: str,
        binding: tuple[str, int, str] | None,
    ) -> bool:
        _, run_controller = lifecycle_components(run_states, binding)
        return run_controller.record_execution_failure(run_id, message)

    delivery_executor = DeliveryExecutor(
        resume_feedback=resume_feedback,
        states=states,
        state_store_factory=lifecycle_state_store,
        driver_factory=lifecycle_driver_factory,
        record_execution_failure=record_lifecycle_failure,
    )

    def select_run(action: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        if action_kind == "resume":
            action_payload = action.get("payload")
            if not isinstance(action_payload, Mapping):
                raise TaskControlError(error_message('cli.error.resume_payload_missing'))
            policy_snapshot = action_payload.get("policy")
            if not isinstance(policy_snapshot, Mapping):
                raise TaskControlError(error_message('cli.error.resume_policy_missing'))
            run_id = action_payload.get("run_id")
            if not isinstance(run_id, str) or run_id != parsed.run_id:
                raise TaskControlError(error_message('cli.error.resume_run_binding'))
            budget_checkpoint_resume = action_payload.get(
                "resume_budget_checkpoint"
            )
            if not isinstance(budget_checkpoint_resume, bool):
                raise TaskControlError(error_message('cli.error.resume_budget_binding'))
            bound_intent = action_payload.get("resume_intent")
            return controller.resume(
                run_id,
                resume_human_blocker=(
                    isinstance(bound_intent, Mapping)
                    and bound_intent.get("authorization")
                    == "human_response"
                ),
                new_thread=action_payload.get("new_thread") is True,
                human_response=(
                    action_payload.get("message")
                    if isinstance(action_payload.get("message"), str)
                    else None
                ),
                explicit_resume=True,
                record_explicit_resume_audit=(
                    current is None
                    or not cli_surface._resume_is_publication_recovery(current)
                ),
                resume_budget_checkpoint=budget_checkpoint_resume,
                budget_policy=parse_policy_snapshot(policy_snapshot),
                validate_resume_state=lambda state: validate_resume_intent(
                    state, bound_intent
                ),
                prepare_state=lambda state: prepare_action_application_receipt(
                    state, action
                ),
            )
        if action_kind in {"approve", "revise", "requeue"}:
            action_payload = action.get("payload")
            if not isinstance(action_payload, Mapping):
                raise TaskControlError(error_message('cli.error.action_payload_missing' ,value0=action_kind))
            run_id = action_payload.get("run_id")
            if not isinstance(run_id, str) or run_id != parsed.run_id:
                raise TaskControlError(
                    error_message('cli.error.action_run_binding' ,value0=action_kind)
                )
            driver = lifecycle_driver_factory(states, None)
            prepare_state = lambda state: prepare_action_application_receipt(
                state, action
            )

            def apply_intent() -> Any:
                if action_kind == "approve":
                    return driver.operations.approve(
                        run_id, prepare_state=prepare_state
                    )
                if action_kind == "revise":
                    message = action_payload.get("message")
                    if not isinstance(message, str) or not message:
                        raise TaskControlError(error_message('cli.error.revise_feedback_missing'))
                    return driver.operations.revise(
                        run_id, message, prepare_state=prepare_state
                    )
                return driver.operations.requeue(
                    run_id, prepare_state=prepare_state
                )

            lost_response_reconciled = False
            while True:
                try:
                    outcome = apply_intent()
                except GitHubReadError as error:
                    if (
                        not is_proven_github_state_contradiction(error.code)
                        or not controller.record_deterministic_contradiction(
                            run_id, error.code, error.message
                        )
                    ):
                        raise
                    contradicted = states.load_current_run(run_id)
                    if contradicted is None:  # pragma: no cover - just persisted
                        raise TaskControlError(
                            error_message('cli.error.contradiction_not_saved' ,value0=action_kind)
                        )
                    prepare_action_application_receipt(contradicted, action)
                    states.save_run(run_id, contradicted)
                    return contradicted, True
                except SimulatedProcessCrash:
                    raise
                except (OSError, TimeoutError):
                    persisted = states.load_current_run(run_id)
                    if (
                        lost_response_reconciled
                        or persisted is None
                        or not action_receipt_matches(
                            persisted, {**action, "run_id": run_id}
                        )
                    ):
                        raise
                    if action_kind == "revise":
                        # Revision has no ambiguous Publisher closeout to
                        # reconcile.  Its exact Action receipt proves the
                        # repair intent was already saved, so replaying would
                        # enqueue the same repair window twice.
                        return persisted, True
                    # The intent and its Action receipt committed atomically,
                    # but the Publisher response was lost.  Reconcile the
                    # idempotent closeout once inside this same Executor; do
                    # not admit or apply a second user intent.
                    lost_response_reconciled = True
                    continue
                state = outcome.state
                # A newly admitted mutation is bound to its existing Run only
                # after ``select_run`` returns.  The business mutation has
                # nevertheless persisted that exact Run ID in its atomic
                # application receipt, so include it while validating the
                # receipt before handing control back to the lifecycle spine.
                if action_receipt_matches(state, {**action, "run_id": run_id}):
                    return state, True
                if outcome.kind is not RunOutcomeKind.EXTERNAL_WAIT:
                    raise TaskControlError(
                        error_message('cli.error.receipt_missing' ,value0=action_kind)
                    )
                driver.supervisor.observe(state)
                if not driver.supervisor.before_retry(
                    state,
                    persist_before_sleep=lambda: states.save_run(run_id, state),
                ):
                    states.save_run(run_id, state)
                    raise TaskControlError(
                        error_message('cli.error.intent_timeout' ,value0=action_kind)
                    )
                states.save_run(run_id, state)
        run_payload = action.get("payload")
        if not isinstance(run_payload, Mapping):
            raise TaskControlError(error_message('cli.error.run_payload_missing'))
        frozen_policy = parse_policy_snapshot(run_payload.get("policy"))
        controller.delivery_policy_provider = lambda: frozen_policy
        controller.creation_language = str(run_payload.get("language", "zh"))
        def prepare_creation(state: dict[str, Any]) -> None:
            # Keep the initial configuration beside the creation receipt so a
            # lost Task Control can be reconciled without reading user defaults.
            state.setdefault("creation_configuration", dict(run_payload))
            state.setdefault("notifications", dict(run_payload.get("notifications", {"enabled": False})))
            prepare_action_application_receipt(state, action)

        return controller.start_or_resume_unfinished(
            task.parent_number, prepare_state=prepare_creation,
        )

    def initialize_lifecycle_profile(value: dict[str, Any], resumed: bool) -> None:
        allow_create = not resumed
        if resumed:
            run_id = value.get("run_id")
            if isinstance(run_id, str) and profiles.load(run_id) is None:
                record = control.load(task)
                action = record.get("action") if isinstance(record, dict) else None
                receipt = value.get("action_application_receipt")
                receipt_matches = receipt is None or (
                    isinstance(receipt, Mapping)
                    and receipt.get("run_id") == run_id
                    and all(
                        receipt.get(key)
                        == (action.get(key) if isinstance(action, Mapping) else None)
                        for key in ("action_id", "kind", "payload_digest")
                    )
                )
                allow_create = (
                    isinstance(action, Mapping)
                    and action.get("status") in {"accepted", "applying"}
                    and action.get("application_observed") is False
                    and action.get("run_id") in {None, run_id}
                    and receipt_matches
                )
        configuration = creation_profile
        if allow_create:
            record = control.load(task)
            action = record.get("action") if isinstance(record, Mapping) else None
            action_payload = action.get("payload") if isinstance(action, Mapping) else None
            frozen = action_payload.get("profile") if isinstance(action_payload, Mapping) else None
            if isinstance(frozen, Mapping):
                configuration = (frozen.get("preset"), frozen.get("overrides", {}))
        elif not _profile_options_are_explicit(creation_profile):
            configuration = None
        _initialize_profile(
            profiles, value, configuration, allow_create=allow_create
        )

    lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=host,
        task=task,
        preflight=lambda: preflight_override,
        select_run=select_run,
        initialize_profile=initialize_lifecycle_profile,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
            command=lifecycle_arguments,
            cwd=git.root,
            state_root=states.root.resolve(),
        ),
        execute=delivery_executor.execute,
        execute_with_binding=lambda run_id, action_id, generation: (
            delivery_executor.execute(
                run_id, action_id=action_id, generation=generation
            )
        ),
        prepare_executor_session=prepare_executor_session,
    )
    if executor_binding is not None:
        return lifecycle.execute_claimed(
            action_id=executor_binding[0], generation=executor_binding[1]
        )
    return lifecycle.submit(
        LifecycleRequest(
            task=task,
            kind=action_kind,
            payload=payload,
            allow_terminal_successor=(
                current is not None
                and (
                    (
                        action_kind == "resume"
                        and cli_surface._resume_is_ready(current)
                    )
                    or (
                        cli_surface._is_lifecycle_action(action_kind)
                        and cli_surface._command_is_ready(current, action_kind)
                    )
                )
            ),
        )
    )


def _reconcile_existing_control(
    control: TaskControlStore,
    task: TaskKey,
    current: dict[str, Any] | None,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    return control.reconcile_from_run(task, current, state_dir=state_dir)


def _require_managed_state(
    states: StateStore,
    git: GitRepository,
    control_record: Mapping[str, Any] | None,
) -> None:
    canonical_root = workspace_state_root(git.root).resolve()
    bound_root = _control_state_root(control_record)
    if states.root.resolve() != canonical_root or (
        bound_root is not None and bound_root != canonical_root
    ):
        raise TaskControlError(error_message('cli.error.task_directory'))


def _control_state_root(control_record: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(control_record, Mapping):
        return None
    raw = control_record.get("run_state_dir")
    if not isinstance(raw, str) or not raw:
        return None
    state_root = Path(raw)
    return state_root.resolve() if state_root.is_absolute() else None


def _run_payload_for_existing_action(
    parsed: argparse.Namespace,
    existing_payload: dict[str, Any],
    current: dict[str, Any] | None,
    creation_profile: tuple[str | None, ProfileOverrides] | None,
) -> dict[str, Any]:
    """Resolve retries against the first payload without hiding new inputs."""

    notifications = existing_payload.get("notifications")
    if current is None:
        if getattr(parsed, "no_notifications", False):
            notifications = notification_snapshot(disabled=True)
        elif getattr(parsed, "notification_mode", None):
            notifications = notification_snapshot(notifications, mode=parsed.notification_mode)
    notification_payload = {"notifications": dict(notifications)} if isinstance(notifications, Mapping) else {}
    if "language" in existing_payload:
        notification_payload["language"] = existing_payload["language"]
    explicit_policy = _policy_overrides(parsed)
    stored_policy = existing_payload.get("policy")
    if isinstance(stored_policy, Mapping):
        policy = parse_policy_snapshot(stored_policy)
        if explicit_policy:
            policy = resolve_delivery_policy(
                user_defaults=policy, command_overrides=explicit_policy
            )
        stored_profile = existing_payload.get("profile")
        if isinstance(stored_profile, Mapping):
            if not _profile_options_are_explicit(creation_profile):
                return {
                    "parent": parsed.parent,
                    "policy": policy.snapshot(),
                    "profile": dict(stored_profile),
                    **notification_payload,
                }
            preset, overrides = creation_profile or (None, {})
            frozen_defaults = {
                "profile": {
                    **({"preset": stored_profile["preset"]} if stored_profile.get("preset") else {}),
                    **stored_profile.get("overrides", {}),
                }
            }
            creation_profile = UserDefaultsStore().resolve_creation(
                preset=preset, overrides=overrides, document=frozen_defaults,
            )
        return {**_run_action_payload(parsed, policy, creation_profile), **notification_payload}
    if not explicit_policy and not _profile_options_are_explicit(creation_profile):
        # A record reconstructed from a Run receipt has no semantic payload.
        # Keeping that empty payload is the fail-closed reconciliation path.
        return dict(existing_payload)
    if current is not None:
        policy = parse_policy_snapshot(policy_snapshot_for_state(current))
    else:
        policy = _resolve_delivery_policy(parsed)
    return {**_run_action_payload(parsed, policy, creation_profile), **notification_payload}


def _profile_options_are_explicit(
    creation_profile: tuple[str | None, ProfileOverrides] | None,
) -> bool:
    if creation_profile is None:
        return False
    preset, overrides = creation_profile
    return preset is not None or any(
        value is not None and value is not False for value in overrides.values()
    )


def _reconcile_resume_exit(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
    github: Any,
    host: ExecutorHost | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Explicit Resume may close an exactly identified externally stopped session."""
    if host is None or current.get("status") == "abandoned":
        return current
    task = _task_for_parent(parsed, github, git)
    control = TaskControlStore(workspace_state_root(git.root))
    try:
        record = control.inspect_run(task, current)
    except TaskControlError:
        # This preflight only repairs an intact, concrete process binding.
        # Leave receipt-only reconstruction to the existing lifecycle spine,
        # which requires its own exact Host evidence before admission.
        return current
    executor = record.get("executor") if isinstance(record, Mapping) else None
    action = record.get("action") if isinstance(record, Mapping) else None
    if not (
        isinstance(executor, Mapping)
        and isinstance(action, Mapping)
        and (
            executor.get("status") in {"running", "starting"}
            or isinstance(executor.get("worker"), Mapping)
            or (
                # A previous reconciliation can commit the Host exit before
                # the Run interruption save fails. Recheck its concrete process
                # identity and finish that boundary on the next explicit Resume.
                executor.get("status") in {"exited", "absent"}
                and (not has_run_operator_gate(current) or is_final_approval_action(action, current))
                and type(executor.get("pid")) is int
                and isinstance(executor.get("process_start_token"), str)
            )
        )
        and (
            action.get("status") in {"completed", "failed"}
            or is_final_approval_action(action, current)
        )
        and (action_receipt_matches(current, action)
             or _unbound_action_matches_run_receipt(current, action))
    ):
        return current
    spec = ExecutorSpec(
        task=task,
        action_id=str(action["action_id"]),
        run_id=executor.get("run_id"),
        generation=int(executor["generation"]),
        cwd=git.root,
        state_root=states.root,
        runner_binding=executor.get("runner_binding"),
    )
    observation = host.observe(spec, control)
    if observation.status == "running":
        return current
    if (
        observation.status not in {"exited", "absent"}
        or observation.generation not in {None, spec.generation}
    ):
        raise ExecutorStartUnknownError(observation.reason or error_message('cli.error.executor_exit_unknown'))
    validate_executor_exit(executor)
    latest = states.load_current_run(str(current["run_id"]))
    if latest is None or not (
        action_receipt_matches(latest, action) or _unbound_action_matches_run_receipt(latest, action)
    ):
        raise ExecutorStartUnknownError(error_message('cli.error.executor_changed'))
    current = latest
    if current.get("status") in {"completed", "abandoned"} and not is_final_approval_action(action, current):
        return current
    if _unbound_action_matches_run_receipt(current, action) and is_final_approval_action(action, current):
        control.mark_executor_absent(task, action_id=spec.action_id, generation=spec.generation)
        control.complete_action_from_application_receipt(
            task, action_id=spec.action_id, generation=spec.generation,
            application_receipt=current["action_application_receipt"],
            result_status=str(current.get("status")), failure=final_approval_failure(current),
        )
        return current
    control.finish_executor(
        task, action_id=spec.action_id, generation=spec.generation,
        run_id=spec.run_id, runner_binding=spec.runner_binding,
        failure=(final_approval_failure(current) if is_final_approval_action(action, current) else None),
        result_status=str(current.get("status")),
    )
    if is_final_approval_action(action, current):
        return current
    if not has_run_operator_gate(current):
        control.fail_session_from_application_receipt(
            task, action_id=spec.action_id, generation=spec.generation,
            application_receipt=current["action_application_receipt"],
            persist_run_failure=lambda: record_session_interruption(
                current, save=lambda value: states.save_run(str(current["run_id"]), value)
            ),
        )
    return current


def _resume_action_is_attachable(
    parsed: argparse.Namespace,
    github: Any,
    git: GitRepository,
) -> bool:
    """Whether this exact Resume can join the Executor already in flight."""

    task = _task_for_parent(parsed, github, git)
    try:
        record = TaskControlStore(workspace_state_root(git.root)).load(task)
    except TaskControlError:
        return False
    action = record.get("action") if isinstance(record, Mapping) else None
    executor = record.get("executor") if isinstance(record, Mapping) else None
    if not isinstance(action, Mapping) or action.get("kind") != "resume":
        return False
    action_active = action.get("status") in {"accepted", "applying"}
    executor_active = (
        isinstance(executor, Mapping)
        and executor.get("status") in {"starting", "running"}
        and executor.get("action_id") == action.get("action_id")
    )
    if not action_active and not executor_active:
        return False
    payload = action.get("payload")
    if not isinstance(payload, Mapping):
        return False
    if not all(
        payload.get(key) == value
        for key, value in {
            "parent": parsed.parent,
            "run_id": parsed.run_id,
            "new_thread": bool(parsed.new_thread),
            "message": parsed.message,
        }.items()
    ):
        return False
    return _resume_policy_matches_existing_action(parsed, payload)


def _lifecycle_action_is_attachable(
    parsed: argparse.Namespace,
    github: Any,
    git: GitRepository,
    current: Mapping[str, Any],
) -> bool:
    """Whether this command can be reconciled with its in-flight Action."""

    task = _task_for_parent(parsed, github, git)
    try:
        record = TaskControlStore(workspace_state_root(git.root)).load(task)
    except TaskControlError:
        record = None
    if record is None:
        receipt = current.get("action_application_receipt")
        if parsed.command in {"approve", "revise", "requeue"} and isinstance(
            receipt, Mapping
        ):
            return (
                receipt.get("run_id") == current.get("run_id")
                and receipt.get("kind") == parsed.command
                and receipt.get("payload_digest")
                == payload_digest(_mutation_action_payload(parsed, current))
            )
    action = record.get("action") if isinstance(record, Mapping) else None
    executor = record.get("executor") if isinstance(record, Mapping) else None
    if not isinstance(action, Mapping) or action.get("kind") != parsed.command:
        return False
    if action.get("status") in {"accepted", "applying"} or (
        isinstance(executor, Mapping)
        and executor.get("status") in {"starting", "running"}
        and executor.get("action_id") == action.get("action_id")
    ):
        return parsed.command != "stop" or _mutation_request_matches_action(
            parsed, action, parsed.run_id
        )
    return action.get("status") in {"completed", "failed"} and (
        _mutation_request_matches_action(parsed, action, parsed.run_id)
    )


def _mutation_request_matches_action(
    parsed: argparse.Namespace,
    action: Mapping[str, Any],
    run_id: object,
) -> bool:
    """Compare a repeated public mutation with its first frozen payload."""

    if not isinstance(run_id, str):
        return False
    requested: dict[str, object] = {
        "parent": getattr(parsed, "parent", None),
        "run_id": run_id,
    }
    if parsed.command == "revise":
        message = getattr(parsed, "message", None)
        if not isinstance(message, str) or not message.strip():
            return False
        requested["message"] = message.strip()
    payload = action.get("payload")
    return isinstance(payload, Mapping) and dict(payload) == requested


def _reject_if_task_action_pending(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    git: GitRepository,
) -> None:
    """Reject a legacy mutation without holding control across its work."""

    if parsed.command not in {"resume", "requeue", "approve", "revise", "abandon"}:
        return None
    run_id = getattr(parsed, "run_id", None)
    if not isinstance(run_id, str):
        return None
    state = states.load_run(run_id)
    if not isinstance(state, dict):
        return None
    require_current_run_state(state)
    run_state = state
    parent = state.get("parent")
    repository = state.get("repository")
    number = parent.get("number") if isinstance(parent, dict) else None
    if not isinstance(repository, str) or type(number) is not int or number <= 0:
        return None
    expected = state.get("checkout_identity")
    if not isinstance(expected, str) or git.checkout_identity() != expected:
        return None
    task = TaskKey(git.root, repository, number)
    control = TaskControlStore(workspace_state_root(git.root))
    receipt = (
        run_state.get("action_application_receipt")
        if run_state is not None
        else None
    )
    if control.load(task) is None and isinstance(receipt, Mapping):
        raise TaskControlError(
            error_message('cli.error.control_record_missing')
        )
    control.require_mutation_available(task)


def _run_driver(
    parsed: argparse.Namespace,
    states: StateStore | FaultInjectingStateStore,
    controller: Controller,
    git: GitRepository,
    github: FixtureGitHubReader | GhGitHubReader,
    profiles: AgentProfileStore,
    before_external_step: Callable[[], None] | None = None,
    resume_pending_refresh: Callable[
        [str], tuple[dict[str, Any], bool]
    ]
    | None = None,
    use_current_state_once: bool = False,
    on_worker_started: Callable[[int], None] | None = None,
    on_worker_finished: Callable[[int], None] | None = None,
    executor_host: ExecutorHost | None = None,
) -> RunDriver:
    agents = _agent_backend(
        parsed,
        profiles,
        on_worker_started=on_worker_started,
        on_worker_finished=on_worker_finished,
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
            profiles=profiles,
            before_external_step=before_external_step,
            resume_pending_refresh=resume_pending_refresh,
            use_current_state_once=use_current_state_once,
            executor_host=executor_host,
        ),
        states=states,
        supervisor=_foreground_supervisor(parsed),
    )


def _agent_backend(
    parsed: argparse.Namespace,
    profiles: AgentProfileStore,
    *,
    on_worker_started: Callable[[int], None] | None = None,
    on_worker_finished: Callable[[int], None] | None = None,
) -> ProfiledAgentBackend:
    fixture = getattr(parsed, "agent_fixture", None)
    backend = (
        FixtureAgentBackend(Path(fixture))
        if fixture
        else CodexCliBackend(
            on_worker_started=on_worker_started,
            on_worker_finished=on_worker_finished,
        )
    )
    run_id = getattr(parsed, "run_id", None)
    return ProfiledAgentBackend(
        backend,
        profiles,
        run_id=run_id if isinstance(run_id, str) else None,
    )


def _load_read_only_run(parsed: argparse.Namespace) -> dict[str, object]:
    run_id = getattr(parsed, "run_id", None)
    parent = getattr(parsed, "parent", None)
    if run_id is not None:
        if parent is not None:
            raise _selector_error(
                "run_selector_invalid",
                error_message('cli.error.selector_exclusive'),
                [],
            )
        return _load_exact_read_only_run(parsed, run_id)

    if parent is None and parsed.repo:
        raise _selector_error(
            "run_selector_requires_parent",
            error_message('cli.error.selector_parent_required'),
            [],
        )
    selector_root = selected_repository_root(parsed.repo, parsed.github_fixture)
    records = _selector_records(parsed, read_only=True)
    public, state = _select_one_record(
        records,
        parent_number=parent,
        active_only=parent is None,
        purpose="status/history",
        current_root=selector_root,
    )
    repository_root = public.get("repository_root")
    selected_state_dir = public.get("state_dir")
    return _with_executor_control(
        state,
        repository_root=(
            Path(repository_root)
            if isinstance(repository_root, str) and repository_root != "unavailable"
            else None
        ),
        state_root=(
            Path(selected_state_dir)
            if isinstance(selected_state_dir, str)
            and selected_state_dir != "unavailable"
            else None
        ),
    )


def _resolve_mutation_selection(
    parsed: argparse.Namespace,
    git: GitRepository,
    states: StateStore | FaultInjectingStateStore,
) -> None:
    """Resolve the shared Parent-or-exact-Run mutation selector."""

    raw_selector = parsed.run_id
    if not isinstance(raw_selector, str):
        raise RunLocatorError("run_selector_invalid", error_message('cli.error.selector_required'))
    if parsed.command == "resume" and raw_selector.isdecimal():
        _resolve_resume_selection(parsed, git)
        return
    if raw_selector.isdecimal():
        parent_number = int(raw_selector)
        if parent_number <= 0:
            raise RunLocatorError(
                "run_selector_invalid", error_message('cli.error.parent_positive')
            )
        parsed.run_id = None
        parsed.parent = parent_number
        _reject_local_repository_mismatch(parsed, git, parent_number)
        records = _selector_records(parsed, current_root=git.root, local_only=True)
        ready_command = (
            parsed.command
            if parsed.command in {"approve", "revise", "requeue"}
            else None
        )
        try:
            selected, _state = _select_one_record(
                records,
                parent_number=parent_number,
                active_only=True,
                purpose=parsed.command,
                ready_command=ready_command,
                current_root=git.root,
            )
        except RunLocatorError as selection_error:
            if (
                parsed.command in {"stop", "abandon"}
                and selection_error.code == "run_selector_not_found"
            ):
                selected, _state = _select_one_record(
                    records,
                    parent_number=parent_number,
                    active_only=False,
                    purpose=parsed.command,
                    current_root=git.root,
                )
            elif ready_command is None:
                raise
            else:
                selected = _select_attached_action_run(
                    parsed,
                    git,
                    records,
                    parent_number=parent_number,
                    selection_error=selection_error,
                )
        parsed.run_id = _selected_local_run_id(
            parsed, git, selected, command=parsed.command
        )
        return

    state = states.load_current_run(raw_selector)
    if state is None:
        raise _selector_error(
            "run_selector_not_found",
            error_message('cli.error.selected_run_missing' ,value0=parsed.command),
            [],
        )
    _validate_repository_selector(parsed, state, raw_selector)
    parent = state.get("parent")
    exact_parent_number = (
        parent.get("number") if isinstance(parent, Mapping) else None
    )
    if type(exact_parent_number) is not int or exact_parent_number <= 0:
        raise ValueError("Delivery Run is missing its Parent Issue")
    parsed.parent = exact_parent_number


def _reject_local_repository_mismatch(
    parsed: argparse.Namespace,
    git: GitRepository,
    parent_number: int,
) -> None:
    """Distinguish an explicit wrong repository from an absent Parent Run."""

    repository = parsed.repo
    if repository is None:
        return
    _validate_repository_name(repository)
    local_records = _read_state_directory(workspace_state_root(git.root), git.root)
    mismatches = [
        public
        for public, state in local_records
        if state is not None
        and public.get("parent") == parent_number
        and not same_repository(public.get("repository"), repository)
    ]
    if mismatches:
        raise _selector_error(
            "run_selector_repository_mismatch",
            error_message('cli.error.parent_repository_mismatch' ,value0=parent_number, value1=repository),
            mismatches,
        )


def _select_attached_action_run(
    parsed: argparse.Namespace,
    git: GitRepository,
    records: list[tuple[dict[str, object], dict[str, Any] | None]],
    *,
    parent_number: int,
    selection_error: RunLocatorError,
) -> dict[str, object]:
    """Locate only the exact Action already accepted for this command."""

    expected_root = git.root.resolve()
    candidates = [
        (public, state)
        for public, state in records
        if state is not None
        and public.get("parent") == parent_number
        and isinstance(public.get("repository_root"), str)
        and Path(str(public["repository_root"])).resolve() == expected_root
    ]
    repositories = {
        str(state["repository"])
        for _public, state in candidates
        if isinstance(state.get("repository"), str)
    }
    if len(repositories) != 1:
        raise selection_error
    repository = next(iter(repositories))
    record = TaskControlStore(workspace_state_root(git.root)).load(
        TaskKey(git.root, repository, parent_number)
    )
    action = record.get("action") if isinstance(record, Mapping) else None
    executor = record.get("executor") if isinstance(record, Mapping) else None
    executor_active = (
        isinstance(executor, Mapping)
        and executor.get("status") in {"starting", "running"}
        and isinstance(action, Mapping)
        and executor.get("action_id") == action.get("action_id")
    )
    bound_run_id = (
        action.get("run_id") if isinstance(action, Mapping) else None
    )
    if not isinstance(bound_run_id, str) and isinstance(record, Mapping):
        bound_run_id = record.get("run_id")
    if not isinstance(bound_run_id, str) and isinstance(action, Mapping):
        frozen_payload = action.get("payload")
        if isinstance(frozen_payload, Mapping):
            bound_run_id = frozen_payload.get("run_id")
    bound = [
        (public, state)
        for public, state in candidates
        if isinstance(bound_run_id, str) and public.get("run_id") == bound_run_id
    ]
    if (
        len(bound) != 1
        or not isinstance(action, Mapping)
        or action.get("kind") != parsed.command
        or not isinstance(bound_run_id, str)
        or not _mutation_request_matches_action(parsed, action, bound_run_id)
        or (
            action.get("status") not in {"accepted", "applying"}
            and not executor_active
            and action.get("status") not in {"completed", "failed"}
        )
    ):
        raise selection_error
    return bound[0][0]


def _resolve_resume_selection(
    parsed: argparse.Namespace,
    git: GitRepository,
) -> None:
    """Resolve the Parent form of ``resume`` before any controller mutation."""

    raw_selector = parsed.run_id
    if not isinstance(raw_selector, str) or not raw_selector.isdecimal():
        return
    parent_number = int(raw_selector)
    if parent_number <= 0:
        raise RunLocatorError(
            "run_selector_invalid", error_message('cli.error.parent_positive')
        )

    # The original argument is a Parent Issue, not a Run ID.  Clearing it
    # while selecting is also important: the outer error path must not treat a
    # failed selector as permission to mutate a Run whose ID happens to be
    # numeric.
    parsed.run_id = None
    parsed.parent = parent_number
    records = _selector_records(parsed, current_root=git.root, local_only=True)
    try:
        selected, _state = _select_one_record(
            records,
            parent_number=parent_number,
            active_only=True,
            recoverable_only=True,
            purpose="resume",
            current_root=git.root,
        )
    except RunLocatorError as selection_error:
        # Once an accepted Resume has moved the Run beyond its original
        # recovery boundary, an identical second terminal must still locate
        # and attach to that Action rather than report that nothing is
        # resumable.
        try:
            selected, active_state = _select_one_record(
                records,
                parent_number=parent_number,
                active_only=True,
                purpose="resume",
                current_root=git.root,
            )
        except RunLocatorError:
            raise selection_error
        repository = active_state.get("repository")
        parent = active_state.get("parent")
        parent_value = parent.get("number") if isinstance(parent, Mapping) else None
        if not isinstance(repository, str) or parent_value != parent_number:
            raise selection_error
        task = TaskKey(git.root, repository, parent_number)
        record = TaskControlStore(workspace_state_root(git.root)).load(task)
        action = record.get("action") if isinstance(record, Mapping) else None
        executor = record.get("executor") if isinstance(record, Mapping) else None
        executor_active = (
            isinstance(executor, Mapping)
            and executor.get("status") in {"starting", "running"}
            and isinstance(action, Mapping)
            and executor.get("action_id") == action.get("action_id")
        )
        if (
            not isinstance(action, Mapping)
            or action.get("kind") != "resume"
            or (
                action.get("status") not in {"accepted", "applying"}
                and not executor_active
            )
        ):
            raise selection_error
    parsed.run_id = _selected_local_run_id(parsed, git, selected, command="resume")


def _selected_local_run_id(
    parsed: argparse.Namespace,
    git: GitRepository,
    selected: dict[str, object],
    *,
    command: str,
) -> str:
    selected_run_id = selected.get("run_id")
    if not isinstance(selected_run_id, str):  # pragma: no cover - candidate contract
        raise ValueError("selected Delivery Run is missing its Run ID")
    selected_root = selected.get("repository_root")
    if (
        not isinstance(selected_root, str)
        or Path(selected_root).resolve() != git.root.resolve()
    ):
        raise _selector_error(
            "run_selector_requires_checkout",
            error_message('cli.error.workspace_mismatch' ,value0=command),
            [selected],
        )
    selected_state_dir = selected.get("state_dir")
    expected_state_dir = (
        Path(parsed.state_dir).resolve()
        if parsed.state_dir
        else (workspace_state_root(git.root)).resolve()
    )
    if (
        not isinstance(selected_state_dir, str)
        or Path(selected_state_dir).resolve() != expected_state_dir
    ):
        raise _selector_error(
            "run_selector_requires_state_dir",
            error_message('cli.error.state_directory_mismatch' ,value0=command),
            [selected],
        )
    return selected_run_id


def _load_exact_read_only_run(
    parsed: argparse.Namespace, run_id: str
) -> dict[str, object]:
    repository_root: Path | None = None
    state_root: Path | None = None
    if parsed.state_dir:
        state_dir = Path(parsed.state_dir).resolve()
        state_root = state_dir
        states = StateStore(state_dir)
        repository_root = _repository_root_for_state_dir(state_dir)
        state = _load_read_only_state(states, run_id)
        if state is not None and state.get("run_id") != run_id:
            state = None
    else:
        try:
            git = open_workspace(parsed.repo, parsed.github_fixture, create=False)
        except (GitError, RunLocatorError):
            git = None
        state = None
        if git is not None:
            state = _load_read_only_state(StateStore(workspace_state_root(git.root)), run_id)
            if state is not None and state.get("run_id") != run_id:
                state = None
            elif state is not None:
                repository_root = git.root
                state_root = workspace_state_root(git.root)
        if state is None:
            locator = RunLocatorIndex.default()
            state_dir = locator.resolve_state_dir(run_id)
            state_root = state_dir
            state = _load_read_only_state(StateStore(state_dir), run_id)
            if state is None or state.get("run_id") != run_id:
                raise RunLocatorError(
                    "run_locator_stale",
                    error_message('cli.error.locator_invalid_state' ,value0=run_id),
                )
            roots = {
                Path(entry["repository_root"]).resolve()
                for entry in locator.entries()
                if entry["run_id"] == run_id
                and Path(entry["state_dir"]).resolve() == state_dir.resolve()
            }
            if len(roots) == 1:
                repository_root = next(iter(roots))
    if state is None:  # pragma: no cover - exact loader raises before this point
        raise ValueError(f"unknown Delivery Run: {run_id}")
    _validate_repository_selector(parsed, state, run_id)
    return _with_executor_control(
        state, repository_root=repository_root, state_root=state_root
    )


def _with_executor_control(
    state: dict[str, Any],
    *,
    repository_root: Path | None,
    state_root: Path | None,
) -> dict[str, Any]:
    """Add an ephemeral, strictly read-only Executor ownership audit."""

    projected = dict(state)
    if state_root is not None:
        from agent_run.notifications import read_notifications

        projected["_notifications"] = read_notifications(state_root, str(state["run_id"]))
        config = state.get("notifications")
        if not projected["_notifications"] and isinstance(config, dict):
            projected["_notifications"] = dict(config)
    if repository_root is None:
        projected["_executor_control"] = {
            "activity": "unknown",
            "reason": "task_control_workspace_unavailable",
        }
        return projected
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, Mapping) else None
    repository = state.get("repository")
    if type(parent_number) is not int or not isinstance(repository, str):
        projected["_executor_control"] = {
            "activity": "unknown",
            "reason": "task_control_identity_unavailable",
        }
        return projected
    task = TaskKey(repository_root, repository, parent_number)
    try:
        record = TaskControlStore(workspace_state_root(repository_root)).inspect_run(
            task, state
        )
    except (GitError, OSError, TaskControlError):
        projected["_executor_control"] = {
            "activity": "unknown",
            "reason": "task_control_invalid",
        }
        return projected
    if record is None:
        projected["_executor_control"] = {
            "activity": "unknown",
            "reason": "task_control_missing",
        }
        return projected
    action = record.get("action")
    executor = record.get("executor")
    executor_status = (
        executor.get("status") if isinstance(executor, Mapping) else None
    )
    activity = "unknown"
    reason: str | None = "executor_control_invalid"
    if (
        isinstance(action, Mapping)
        and action.get("kind") in {"stop", "abandon"}
        and action.get("status") != "completed"
        and isinstance(action.get("target_executor"), Mapping)
    ):
        # The Control Executor's exit says nothing about its unresolved target.
        reason = "control_target_unresolved"
    elif isinstance(executor, Mapping) and executor.get(
        "reconciliation_required"
    ) is True:
        reason = "executor_reconciliation_required"
    elif executor_status in {"exited", "absent"}:
        activity = "not_running"
        reason = None
    elif (
        executor_status in {"starting", "running"}
        and isinstance(action, Mapping)
        and isinstance(executor, Mapping)
        and isinstance(state_root, Path)
    ):
        action_id = action.get("action_id")
        generation = executor.get("generation")
        run_id = state.get("run_id")
        exact_binding = (
            isinstance(action_id, str)
            and action_id
            and type(generation) is int
            and generation > 0
            and isinstance(run_id, str)
            and executor.get("action_id") == action_id
            and executor.get("run_id") == run_id
            and action.get("run_id") == run_id
            and action.get("executor_generation") == generation
            and action_receipt_matches(state, action)
        )
        if exact_binding:
            assert isinstance(action_id, str)
            assert type(generation) is int
            assert isinstance(run_id, str)
            spec = ExecutorSpec(
                task=task,
                action_id=action_id,
                run_id=run_id,
                generation=generation,
                state_root=state_root.resolve(),
            )
            try:
                observation = observe_systemd_executor(
                    spec,
                    TaskControlStore(workspace_state_root(repository_root)),
                    runtime_directory=_executor_runtime_directory(),
                    executor_python=Path(sys.executable),
                )
            except (OSError, ExecutorHostError):
                reason = "executor_host_unavailable"
            else:
                if observation.status == "absent":
                    activity = "not_running"
                    reason = None
                elif observation.generation != generation:
                    reason = "executor_binding_invalid"
                elif observation.status == "running":
                    recorded_pid = executor.get("pid")
                    if (
                        executor_status == "running"
                        and isinstance(executor.get("handshake_at"), str)
                        and type(recorded_pid) is int
                        and recorded_pid > 0
                        and observation.pid == recorded_pid
                    ):
                        activity = "running"
                        reason = None
                    else:
                        reason = "executor_binding_invalid"
                elif observation.status == "exited":
                    activity = "not_running"
                    reason = None
                else:
                    reason = "executor_host_unknown"
        else:
            reason = "executor_binding_invalid"
    projected["_executor_control"] = {
        "activity": activity,
        "action": (
            {
                key: action.get(key)
                for key in (
                    "action_id",
                    "kind",
                    "payload_digest",
                    "status",
                    "run_id",
                    "executor_generation",
                    "application_observed",
                )
            }
            if isinstance(action, Mapping)
            else None
        ),
        "executor": (
            {
                key: executor.get(key)
                for key in (
                    "status",
                    "action_id",
                    "run_id",
                    "generation",
                    "pid",
                    "handshake_at",
                )
            }
            if isinstance(executor, Mapping)
            else None
        ),
    }
    if reason is not None:
        projected["_executor_control"]["reason"] = reason
    return projected


def _load_read_only_state(states: StateStore, run_id: str) -> dict[str, Any] | None:
    state = states.load_run(run_id)
    if state is None:
        return None
    try:
        require_current_run_state(state)
    except IncompatibleRunStateError:
        if _is_legacy_run_state(state):
            return state
        raise
    return state


def _is_legacy_run_state(state: Mapping[str, Any]) -> bool:
    return (
        "schema_version" in state
        or state.get("lifecycle_action_protocol") != TASK_CONTROL_PROTOCOL
    )


def _selector_records(
    parsed: argparse.Namespace,
    *,
    current_root: Path | None = None,
    read_only: bool = False,
    local_only: bool = False,
) -> list[tuple[dict[str, object], dict[str, Any] | None]]:
    """Load selector candidates from the bounded index or an explicit state dir."""

    repository = parsed.repo
    if repository is not None:
        _validate_repository_name(repository)

    if parsed.state_dir:
        state_dir = Path(parsed.state_dir).resolve()
        root = _repository_root_for_state_dir(state_dir)
        records = _read_state_directory(state_dir, root, read_only=read_only)
        if repository is not None:
            records = [
                record
                for record in records
                if same_repository(record[0].get("repository"), repository)
                or record[0].get("repository") is None
            ]
        return records

    if current_root is None:
        current_root = selected_repository_root(repository, parsed.github_fixture)
        if current_root is None and repository is None:
            raise _selector_error(
                "run_selector_context",
                error_message('cli.error.repository_required'),
                [],
            )

    locator = RunLocatorIndex.default()
    entries = locator.entries()
    if local_only and current_root is not None:
        expected_root = current_root.resolve()
        entries = [
            entry
            for entry in entries
            if Path(entry["repository_root"]).resolve() == expected_root
        ]
    records = [_read_locator_entry(entry, read_only=read_only) for entry in entries]

    # A current checkout is an already-known, bounded location.  It remains a
    # useful fallback when an older/newly interrupted Run has not completed its
    # locator registration, but it never replaces an indexed entry.
    if current_root is not None and current_root.is_dir():
        local_root = workspace_state_root(current_root.resolve())
        indexed_runs = {
            (entry["run_id"], Path(entry["state_dir"]).resolve())
            for entry in entries
        }
        local_records = _read_state_directory(
            local_root, current_root.resolve(), read_only=read_only
        )
        records.extend(
            record
            for record in local_records
            if (
                str(record[0]["run_id"]),
                Path(str(record[0]["state_dir"])).resolve(),
            )
            not in indexed_runs
        )
        # Resolve local identity only after the bounded fallback is included.
        # Otherwise an unregistered local Run can silently lose other clones.
        if repository is None:
            repository = workspace_for_root(current_root.resolve()).repository
    if repository is not None:
        records = [
            record
            for record in records
            if (record[0].get("repository") is None or same_repository(record[0].get("repository"), repository))
        ]
    elif current_root is not None:
        # With no Repository identity, only this checkout is a known target.
        # Unreadable identities still need explicit disambiguation.
        records = [
            record for record in records
            if record[0].get("repository_root") == str(current_root.resolve())
            or record[1] is None
        ]
    return records


def _repository_root_for_state_dir(state_dir: Path) -> Path | None:
    """Resolve a state directory to a verified checkout when possible."""

    if state_dir.name == "state":
        root = state_dir.parent / "repository"
        try:
            if workspace_state_root(root) == state_dir:
                return root
        except (GitError, OSError):
            pass

    try:
        entries = RunLocatorIndex.default().entries()
    except RunLocatorError:
        return None
    roots = {
        Path(entry["repository_root"]).resolve()
        for entry in entries
        if Path(entry["state_dir"]).resolve() == state_dir
    }
    if len(roots) != 1:
        return None
    recorded_root = next(iter(roots))
    try:
        root = GitRepository.discover(recorded_root).root
    except (GitError, OSError):
        return None
    return root if root == recorded_root else None


def _verified_locator_checkout(
    entry: LocatorEntry,
) -> tuple[GitRepository, str | None]:
    workspace = workspace_for_root(Path(entry["repository_root"]).resolve())
    return workspace.open(), workspace.repository


def _read_state_directory(
    state_dir: Path, repository_root: Path | None, *, read_only: bool = False
) -> list[tuple[dict[str, object], dict[str, Any] | None]]:
    runs_directory = state_dir / "runs"
    if not runs_directory.is_dir():
        return []
    paths: list[Path] = []
    for path in runs_directory.glob("*.json"):
        paths.append(path)
        if len(paths) > MAX_LOCATOR_ENTRIES:
            raise RunLocatorError(
                "run_locator_invalid",
                error_message('cli.error.too_many_runs' ,value0=MAX_LOCATOR_ENTRIES),
            )
    paths.sort(key=lambda path: path.name)
    repository_root_value = (
        str(repository_root.resolve()) if repository_root is not None else "unavailable"
    )
    records: list[tuple[dict[str, object], dict[str, Any] | None]] = []
    for path in paths:
        entry: LocatorEntry = {
            "run_id": path.stem,
            "repository_root": repository_root_value,
            "state_dir": str(state_dir.resolve()),
            "updated_at": "",
        }
        public, state = _read_locator_entry(
            entry,
            verify_checkout=repository_root is not None,
            read_only=read_only,
        )
        records.append((public, state if repository_root is not None else None))
    return records


def _read_locator_entry(
    entry: LocatorEntry,
    *,
    verify_checkout: bool = True,
    read_only: bool = False,
) -> tuple[dict[str, object], dict[str, Any] | None]:
    run_id = entry["run_id"]
    if Path(run_id).name != run_id:
        return _candidate(entry, error=error_message("cli.error.unsafe_run_id")), None
    state_dir = Path(entry["state_dir"])
    state_path = state_dir / "runs" / f"{run_id}.json"
    state: dict[str, Any] | None = None
    state_error: str | None = None
    if not state_path.is_file():
        state_error = error_message("cli.error.state_missing")
    else:
        try:
            # Identity must be checked before validating the lifecycle payload:
            # a broken payload cannot hide a contradictory routing identity.
            state = StateStore(state_dir).load_run(run_id)
        except (OSError, ValueError) as error:
            state_error = bounded_error(str(error))
        if state is None:
            if state_error is None:
                state_error = error_message("cli.error.run_id_mismatch")
        elif state.get("run_id") != run_id:
            return _candidate(
                entry,
                error=error_message("cli.error.run_id_mismatch"),
                identity_conflict=True,
            ), None
        if state is not None:
            repository = state.get("repository")
            parent = state.get("parent")
            parent_number = parent.get("number") if isinstance(parent, dict) else None
            if (
                (
                    repository is not None
                    and (
                        not isinstance(repository, str)
                        or len(repository.split("/")) != 2
                        or not all(repository.split("/"))
                        or any(character.isspace() for character in repository)
                    )
                )
                or (
                    parent_number is not None
                    and (type(parent_number) is not int or parent_number <= 0)
                )
            ):
                return _candidate(
                    entry,
                    error=error_message("cli.error.identity_format"),
                    identity_conflict=True,
                ), None
            if repository is None or parent_number is None:
                state_error = error_message("cli.error.identity_missing")
        if state is not None and "repository" in entry:
            parent = state.get("parent")
            if (
                not same_repository(state.get("repository"), entry["repository"])
                or not isinstance(parent, dict)
                or parent.get("number") != entry["parent_number"]
            ):
                return _candidate(
                    entry,
                    error=error_message("cli.error.identity_mismatch"),
                    identity_conflict=True,
                ), None
        if state is not None:
            try:
                require_current_run_state(state)
            except IncompatibleRunStateError as error:
                if not (read_only and _is_legacy_run_state(state)):
                    state_error = bounded_error(str(error))
    if state is None and state_error is not None:
        if not verify_checkout:
            return _candidate(entry, error=state_error), None
        try:
            _checkout, checkout_repository = _verified_locator_checkout(entry)
        except (GitError, OSError) as error:
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    error=error_message("cli.error.checkout_unavailable", reason=bounded_error(str(error))),
                ),
                None,
            )
        candidate = _candidate(entry, error=state_error)
        if checkout_repository is not None:
            if "repository" in entry and not same_repository(entry["repository"], checkout_repository):
                return _candidate(
                    entry,
                    error=error_message("cli.error.checkout_repository"),
                    identity_conflict=True,
                ), None
            candidate["repository"] = checkout_repository
        return candidate, None
    if state is None:  # pragma: no cover - state errors return above
        return _candidate(entry, error=error_message("cli.error.state_unreadable")), None
    if verify_checkout:
        try:
            checkout, checkout_repository = _verified_locator_checkout(entry)
        except (GitError, OSError) as error:
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    state=state,
                    error=error_message("cli.error.checkout_unavailable", reason=bounded_error(str(error))),
                ),
                None,
            )
        state_repository = state.get("repository")
        if checkout_repository is not None and not same_repository(state_repository, checkout_repository):
            return _candidate(
                entry,
                state=state,
                error=error_message("cli.error.checkout_run"),
                identity_conflict=True,
            ), None
        checkout_identity = checkout.checkout_identity()
        state_identity = state.get("checkout_identity")
        if (
            not isinstance(state_identity, str)
            or checkout_identity is None
            or checkout_identity != state_identity
        ):
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    state=state,
                    error=error_message("cli.error.checkout_identity"),
                    identity_conflict=state_identity is not None,
                ),
                None,
            )
    return _candidate(entry, state=state, error=state_error), (
        state if state_error is None else None
    )


def _candidate(
    entry: LocatorEntry,
    *,
    state: dict[str, Any] | None = None,
    error: str | None = None,
    identity_conflict: bool = False,
) -> dict[str, object]:
    parent: object = entry.get("parent_number")
    repository: object = entry.get("repository")
    status: object = "unavailable"
    started_at: object = None
    if state is not None:
        raw_parent = state.get("parent")
        if isinstance(raw_parent, dict) and isinstance(raw_parent.get("number"), int):
            parent = raw_parent["number"]
        repository = state.get("repository")
        status = state.get("status")
        started_at = state.get("created_at", state.get("started_at"))
    candidate: dict[str, object] = {
        "parent": parent,
        "repository": repository,
        "repository_root": entry["repository_root"],
        "run_id": entry["run_id"],
        "started_at": started_at,
        "state_dir": entry["state_dir"],
        "status": status,
    }
    if error is not None:
        candidate["error"] = error
    if identity_conflict:
        candidate.update(repository=None, parent=None, repository_root="unavailable")
    return candidate


def _select_one_record(
    records: list[tuple[dict[str, object], dict[str, Any] | None]],
    *,
    parent_number: int | None,
    active_only: bool,
    purpose: str,
    recoverable_only: bool = False,
    ready_command: str | None = None,
    current_root: Path | None = None,
) -> tuple[dict[str, object], dict[str, Any]]:
    if parent_number is not None:
        records = [
            record
            for record in records
            if record[0].get("parent") in (None, parent_number)
        ]
    public_records = [record[0] for record in records]
    invalid = [public for public, state in records if state is None]
    if invalid:
        raise _selector_error(
            "run_locator_stale",
            error_message('cli.error.invalid_candidates' ,value0=purpose),
            public_records,
        )

    matches: list[tuple[dict[str, object], dict[str, Any]]] = []
    for public, state in records:
        if state is None:
            continue
        if parent_number is not None and public.get("parent") != parent_number:
            continue
        if active_only and state.get("status") in {"completed", "abandoned"}:
            if not (
                purpose == "resume" and state.get("status") == "completed"
                and (
                    final_approval_failure(state) is not None and has_final_approval(state)
                    or (current_root is not None and has_unfinished_final_receipt(state, current_root))
                )
            ):
                continue
        if ready_command is not None and not cli_surface._command_is_ready(
            state, ready_command
        ):
            continue
        matches.append((public, state))

    if current_root is not None:
        expected_root = current_root.resolve()
        local_matches = [
            match
            for match in matches
            if isinstance(match[0].get("repository_root"), str)
            and Path(str(match[0]["repository_root"])).resolve() == expected_root
        ]
        other_matches = [match for match in matches if match not in local_matches]
        if local_matches:
            if other_matches:
                raise _selector_error(
                    "run_selector_ambiguous",
                    error_message('cli.error.multiple_checkouts' ,value0=purpose),
                    [public for public, _state in matches],
                )
            matches = local_matches
        elif other_matches:
            raise _selector_error(
                "run_selector_requires_checkout",
                error_message('cli.error.other_checkout' ,value0=purpose),
                [public for public, _state in other_matches],
            )

    if recoverable_only:
        if len(matches) > 1:
            raise _selector_error(
                "run_selector_ambiguous",
                error_message('cli.error.multiple_runs' ,value0=purpose),
                [public for public, _state in matches],
            )
        if len(matches) == 1:
            public, state = matches[0]
            if cli_surface._resume_is_ready(state) or (
                state.get("status") == "completed"
                and current_root is not None
                and has_unfinished_final_receipt(state, current_root)
            ):
                return public, state
            raise _selector_error(
                "run_selector_not_recoverable",
                error_message('cli.error.not_recoverable'),
                [public],
            )
    elif len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        raise _selector_error(
            "run_selector_ambiguous",
            error_message('cli.error.multiple_runs' ,value0=purpose),
            [public for public, _state in matches],
        )
    raise _selector_error(
        "run_selector_not_found",
        error_message('cli.error.no_unique_run' ,value0=purpose),
        public_records,
    )


def _selector_error(
    code: str, message: str, candidates: list[dict[str, object]]
) -> RunLocatorError:
    details = message
    if candidates:
        details = error_message("cli.error.candidates", reason=message, candidates="\n".join(
            _candidate_line(candidate) for candidate in candidates
        ))
    return RunLocatorError(code, details, candidates=candidates)


def _candidate_line(candidate: dict[str, object]) -> str:
    error = candidate.get("error")
    error_detail = f" error={error}" if isinstance(error, str) else ""
    return (
        f"- repository={candidate.get('repository') or 'unknown'} "
        f"Parent=#{candidate.get('parent') or 'unknown'} "
        f"status={candidate.get('status') or 'unknown'} "
        f"started_at={candidate.get('started_at') or 'unknown'} "
        f"worktree={candidate.get('repository_root')} "
        f"state_dir={candidate.get('state_dir')}{error_detail}"
    )


def _validate_repository_selector(
    parsed: argparse.Namespace, state: dict[str, object], run_id: str
) -> None:
    repository = parsed.repo
    if repository is not None:
        _validate_repository_name(repository)
        if not same_repository(state.get("repository"), repository):
            parent = state.get("parent")
            candidate = {
                "parent": (
                    parent.get("number")
                    if isinstance(parent, Mapping)
                    else None
                ),
                "repository": state.get("repository"),
                "repository_root": "当前 checkout",
                "run_id": run_id,
                "started_at": state.get("created_at"),
                "state_dir": parsed.state_dir or "Runner 统一状态目录",
                "status": state.get("status"),
            }
            raise _selector_error(
                "run_selector_repository_mismatch",
                error_message('cli.error.repository_mismatch' ,value0=run_id, value1=repository),
                [candidate],
            )


def _human_failure_reason(error: Exception, fallback: str, *, language: str = "zh") -> str:
    """Render selector failures without machine-only locator identities."""

    def message(key: str) -> str:
        return text(f"cli.error.{key}", language=language, reason=fallback)

    if isinstance(error, ExecutorLostError):
        return message("executor_lost")
    if isinstance(error, ActionBusyError):
        return message("action_busy")
    if isinstance(error, ExecutorStartUnknownError):
        return message("executor_unknown")
    if isinstance(error, TaskControlError):
        return message("control_failed")
    if str(error).startswith("multiple unfinished Delivery Runs"):
        return message("unfinished_runs")
    if not isinstance(error, RunLocatorError):
        return fallback
    return message({
        "run_selector_repository_mismatch": "wrong_repository",
        "run_selector_not_found": "no_delivery",
        "run_locator_stale": "stale_locator",
    }.get(error.code, "ambiguous_delivery"))


def _validate_repository_name(repository: str) -> None:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise RunLocatorError(
            "run_selector_invalid_repository",
            error_message('cli.error.repository_format'),
        )


def _list_runs(parsed: argparse.Namespace) -> int:
    records = _selector_records(parsed)
    parent = getattr(parsed, "parent", None)
    if parent is not None:
        records = [
            record
            for record in records
            if record[0].get("parent") in {parent, None}
        ]
    candidates = [public for public, _state in records]
    repository = parsed.repo
    if repository is None:
        repositories = {
            candidate["repository"]
            for candidate in candidates
            if isinstance(candidate.get("repository"), str)
        }
        repository = next(iter(repositories), None) if len(repositories) == 1 else None
    output: dict[str, object] = {
        "result": "runs",
        "repository": repository,
        "runs": candidates,
    }
    if parsed.as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    print(cli_message("cli.repository", repository=repository or "unknown"))
    if not candidates:
        print(cli_message("cli.no_runs"))
        return 0
    print(cli_message("cli.run_candidates"))
    for candidate, state in records:
        language = (str(state["language"]) if isinstance(state, dict)
                    and state.get("language") in {"zh", "en"} else personal_language())
        print(cli_message(
            "cli.run_candidate", language=language,
            repository=candidate.get("repository") or "unknown",
            parent=candidate.get("parent") or "unknown",
            status=cli_presentation.human_delivery_status(
                candidate.get("status") or "unknown", language=language),
            started_at=candidate.get("started_at") or "unknown",
            worktree=candidate.get("repository_root"), state_dir=candidate.get("state_dir"),
        ))
        if candidate.get("error"):
            print(cli_message("cli.reason", language=language, reason=error_detail(ValueError(candidate["error"]), language)))
    return 0


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        help=cli_message('cli.repo_help'),
    )
    parser.add_argument(
        "--state-dir",
        help=cli_message('cli.state_dir_help'),
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
        help=cli_message('cli.preset_help'),
    )
    parser.add_argument(
        "--development-model",
        "--dev-model",
        dest="development_model",
        help=cli_message('cli.dev_model_help'),
    )
    parser.add_argument(
        "--development-effort",
        "--development-reasoning-effort",
        "--dev-effort",
        dest="development_effort",
        help=cli_message('cli.dev_effort_help'),
    )
    parser.add_argument(
        "--review-model", dest="review_model", help=cli_message('cli.review_model_help')
    )
    parser.add_argument(
        "--review-effort",
        "--review-reasoning-effort",
        dest="review_effort",
        help=cli_message('cli.review_effort_help'),
    )
    parser.add_argument(
        "--publication-model",
        dest="publication_model",
        help=cli_message('cli.publication_model_help'),
    )
    parser.add_argument(
        "--publication-effort",
        "--publication-reasoning-effort",
        dest="publication_effort",
        help=cli_message('cli.publication_effort_help'),
    )
    parser.add_argument(
        "--publication-from-development",
        "--publication-reference-development",
        "--publication-use-development",
        action="store_true",
        default=None,
        dest="publication_from_development",
        help=cli_message('cli.publication_reference_help'),
    )
    parser.add_argument(
        "--publication-reference",
        choices=("development",),
        dest="publication_reference",
        help=argparse.SUPPRESS,
    )


def _add_policy_options(
    parser: argparse.ArgumentParser, *, dest_prefix: str = "",
    allow_thread_policy: bool = True,
) -> None:
    if allow_thread_policy:
        parser.add_argument(
            "--development-thread-policy",
            choices=("reuse", "new-per-attempt"),
            dest=f"{dest_prefix}development_thread_policy",
            help=cli_message('cli.thread_policy_help'),
        )
    parser.add_argument(
        "--parent-only-paired-rounds",
        "--parent-only-paired-round",
        dest=f"{dest_prefix}parent_only_paired_rounds",
        type=_positive_integer,
        help=cli_message('cli.parent_rounds_help'),
    )
    parser.add_argument(
        "--run-repair-rounds",
        "--run-repair-round",
        dest=f"{dest_prefix}run_repair_rounds",
        type=_positive_integer,
        help=cli_message('cli.repair_rounds_help'),
    )
    parser.add_argument(
        "--ticket-review-rounds",
        "--ticket-review-round",
        dest=f"{dest_prefix}ticket_review_rounds",
        type=_positive_integer,
        help=cli_message('cli.ticket_rounds_help'),
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
            help=cli_message("cli.deadline_help", role=label),
        )


def _policy_overrides(parsed: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key in (
        "parent_only_paired_rounds",
        "run_repair_rounds",
        "ticket_review_rounds",
        "development_thread_policy",
    ):
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
    store = UserDefaultsStore()
    command = getattr(parsed, "policy_command", None)
    overrides = _policy_overrides(parsed)
    if command == "show" or (command is None and not overrides):
        result = store.describe()
        result["result"] = "policy"
    else:
        result = store.configure(policy=overrides)
        result["result"] = "configured"
    result["user_defaults"] = result["defaults"].get("policy", {})
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
    output = {
        "result": "configured",
        "run_id": parsed.run_id,
        "profile_revision": document["profile_revision"],
        "preset": document["preset"],
        "profiles": document["profiles"],
    }
    if parsed.as_json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        parent = state.get("parent")
        parent_number = parent.get("number") if isinstance(parent, Mapping) else "?"
        print(cli_message("cli.repository", language=str(state.get("language", "zh")), repository=state.get("repository")))
        print(cli_message("cli.parent_issue", language=str(state.get("language", "zh")), parent=parent_number))
        print(cli_message("cli.profile_saved", language=str(state.get("language", "zh")), revision=document["profile_revision"]))
        print(cli_message("cli.profile_no_execution", language=str(state.get("language", "zh"))))
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
        raise ValueError(error_message('cli.error.unknown_auth'))
    except GitHubAuthProfileError as error:
        print(
            json.dumps(
                {
                    "result": "error",
                    "status": "invalid_auth_profile",
                    "diagnostics": [
                        {
                            "code": "github_auth_profile_invalid",
                            "message": bounded_error(str(error)),
                        }
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
        git = open_workspace(parsed.repo, parsed.github_fixture, create=False)
    except (GitError, RunLocatorError):
        git = None
    if git is not None:
        local_root = workspace_state_root(git.root)
        if StateStore(local_root).load_run(parsed.run_id) is not None:
            return local_root
    return RunLocatorIndex.default().resolve_state_dir(parsed.run_id)


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(cli_message("cli.positive_integer")) from error
    if number <= 0:
        raise argparse.ArgumentTypeError(cli_message("cli.positive_integer"))
    return number


def _positive_duration_argument(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError(cli_message("cli.positive_duration"))
    return value


def _is_currentness_human_blocker(state: dict[str, object]) -> bool:
    """Whether fresh external-state evidence has stopped this command."""
    return (
        state.get("status") == "blocked"
        and state.get("terminal_kind") == "waiting_human"
    )


if __name__ == "__main__":
    sys.exit(main())
