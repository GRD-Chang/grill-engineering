"""Public native query and personal-settings entry point."""
from __future__ import annotations

from agent_run.adapters.foreground import DomainUnknown

import argparse
from collections.abc import Iterable, Mapping, Sequence
from functools import partial
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Literal, NoReturn, TYPE_CHECKING

from agent_run.cli.messages import personal_language as _personal_language
from agent_run.errors import error_detail
from agent_run.contracts.copy import text
from agent_run.errors import GitError
from agent_run.errors import GitHubReadError
from agent_run.runtime.package_lease import RunnerLeaseError

from agent_run.errors import KernelError
from agent_run.adapters.systemd import HostUnavailable
from agent_run.runtime.handoff import HandoffError

from agent_run.cli import selection as cli_selection, settings as personal_settings
from agent_run.queries.run import QueryDocument, read, header
from agent_run.queries.read_facts import QueryError
from agent_run.cli.render import render, render_settings, safe_text
from agent_run.storage.preflight import UnsupportedState
from agent_run.storage.sqlite import QUERY_LIMIT

if TYPE_CHECKING:
    from agent_run.storage.routing import RepositoryRoute


class _HelpFormatter(argparse.HelpFormatter):
    def __init__(self, *args: Any, language: str, **kwargs: Any) -> None:
        self.language = language
        super().__init__(*args, **kwargs)

    def _format_usage(self, usage: str | None, actions: Iterable[argparse.Action],
                      groups: Iterable[argparse._MutuallyExclusiveGroup], prefix: str | None) -> str:
        return super()._format_usage(usage, actions, groups,
            text("cli.usage_prefix", language=self.language) if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, language: str, **kwargs: Any) -> None:
        self.language = language
        kwargs.setdefault("formatter_class", partial(_HelpFormatter, language=language))
        super().__init__(*args, **kwargs)
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = text("cli.help_option", language=language)
        if language == "zh":
            self._positionals.title = "位置参数"
            self._optionals.title = "选项"

    def _check_value(self, action: argparse.Action, value: Any) -> None:
        if action.choices is not None and value not in action.choices:
            self.error(text("cli.invalid_choice", language=self.language,
                argument=action.dest, value=repr(value), choices=", ".join(map(repr, action.choices))))

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        label = "错误" if self.language == "zh" else "Error"
        self.exit(2, f"{label}: {message}\n")

    def add_subparsers(self, **kwargs: Any) -> Any:
        kwargs.setdefault("parser_class", partial(type(self), language=self.language))
        return super().add_subparsers(**kwargs)


def _parser(language: str) -> argparse.ArgumentParser:
    parser = _Parser(language=language, prog="agent-run", description="只读交付查询" if language == "zh" else "Read-only delivery queries")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "history", "runs"):
        help_text = {"status": "当前交付状态", "history": "语义轮次与原始审计", "runs": "发现本地交付运行"}[command] if language == "zh" else {
            "status": "Current delivery status", "history": "Semantic rounds and original audit", "runs": "Discover local delivery Runs"}[command]
        child = commands.add_parser(command, language=language, help=help_text, description=help_text)
        if command != "runs":
            child.add_argument("run_id", nargs="?")
        child.add_argument("--repo")
        child.add_argument("--parent", type=int)
        child.add_argument("--state-dir", type=Path)
        child.add_argument("--data-root", type=Path)
        child.add_argument("--json", action="store_true", dest="as_json")
        if command != "runs":
            child.add_argument("--plain", action="store_true")
        if command == "history":
            child.add_argument("--details", action="store_true")
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int)
    doctor = commands.add_parser("doctor", language=language,
        help=text("cli.doctor_help", language=language), description=text("cli.doctor_help", language=language))
    doctor.add_argument("--json", action="store_true", dest="as_json", help=text("cli.doctor_json_help", language=language))
    verification = doctor.add_mutually_exclusive_group()
    verification.add_argument("--local", action="store_true", help=text("cli.doctor_local_help", language=language))
    verification.add_argument("--online", action="store_true", help=text("cli.doctor_online_help", language=language))
    doctor.add_argument("--mode", choices=("background", "foreground"), default="background",
                        help=text("cli.doctor_mode_help", language=language))
    doctor.add_argument("--repo", help=text("cli.repo_help", language=language))
    doctor.add_argument("--run", dest="run_id", help=text("cli.doctor_run_help", language=language))
    doctor.add_argument("--state-dir", type=Path, help=text("cli.state_dir_help", language=language))
    personal_settings.add_parsers(commands, language)
    from agent_run.cli import profiles as run_profile_cli
    run_profile_cli.add_parser(commands, language)
    from agent_run.cli import prompts as prompt_cli
    prompt_cli.add_parser(commands, language)
    from agent_run.cli.lifecycle import add_parsers
    add_parsers(commands, language)
    return parser


def _print_receipt(parsed: argparse.Namespace, output: Mapping[str, Any], language: str) -> None:
    """Describe the submitted request, never infer Agent or delivery success."""
    def copy(key: str, **values: object) -> str:
        return text("cli.receipt." + key, language=language, **values)

    completed = (parsed.command == "stop" and output.get("action") == "stop"
                 and output["result"] == output["status"] == "completed"
                 and output.get("action_id") is None and output["host"] == "exited")
    idle = (parsed.command == "stop" and output.get("action") == "stop"
            and output["result"] == "no_op" and output["status"] == "not_running"
            and output.get("action_id") is None and output["host"] == "exited")
    print(safe_text(text("cli.no_active_agent", language=language) if idle else
                   copy("completed" if completed else "attached" if output["result"] == "attached" else "accepted", command=parsed.command)))
    if not completed and not idle:
        status = {"action_pending": "pending", "action_applied": "applied", "action_complete": "complete",
                  "action_updating": "updating", "action_superseded": "superseded",
                  "action_failed": "failed"}.get(output["status"], "unknown")
        print(copy("status", status=copy(status)))
    host = output["host"] if output["host"] in {"running", "exited"} else "unknown"
    if not idle:
        print(copy("host_" + host))
    parent = getattr(parsed, "parent", None)
    selector = getattr(parsed, "run_id", None)
    if parent is None and isinstance(selector, str) and selector.isdecimal():
        parent = int(selector)
    target = [parsed.repo] if parsed.repo else []
    if parent is not None:
        target.append(f"Parent #{parent}")
    if getattr(parsed, "ticket", None) is not None:
        target.append(text("presentation.status.ticket_v1", language=language, v1=parsed.ticket))
    if target:
        print(safe_text(copy("target", target=" / ".join(target))))
    command = ["agent-run", "status" if parent is not None else "runs"]
    if parsed.repo:
        command.extend(("--repo", parsed.repo))
    elif parent is None:
        command.append("--help")
    if parent is not None:
        command.extend(("--parent", str(parent)))
    if not completed and not idle:
        print(copy("boundary"))
    print(safe_text(copy("next", command=shlex.join(command))))


def _repository_query(parsed: argparse.Namespace, routes: Sequence[RepositoryRoute], language: str) -> QueryDocument:
    """Read from typed selection; never initialize or turn display into control."""
    if parsed.command == "runs":
        locations = cli_selection.select_repository(parsed, cli_selection.repository_candidates(parsed, routes))
        headers = []
        size = 0
        for location in locations:
            item = header(location.database,
                storage=cli_selection.repository_route_for(location, routes).at_run(location.run_id))
            size += len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode())
            if size > QUERY_LIMIT:
                raise QueryError("query_limit", "Run listing exceeds its bounded response limit")
            headers.append(item)
        return QueryDocument("runs", language, {"result": "runs", "runs": headers})
    if parsed.command not in {"status", "history", "settings"}:
        raise QueryError("run_locator_invalid", "Unknown repository query")
    route, location = cli_selection.repository_target(parsed, routes)
    kind: Literal["status", "history", "settings"] = (
        "history" if parsed.command == "history" else "settings" if parsed.command == "settings" else "status")
    return read(location.database, kind, after=getattr(parsed, "after", 0), limit=getattr(parsed, "limit", None),
        expected_run=location.run_id, expected_repository=location.task.repository, storage=route.at_run(location.run_id))


def main(arguments: Sequence[str] | None = None, *, repository_routes: Sequence[RepositoryRoute] | None = None) -> int:
    language = _personal_language()
    parser = _parser(language)
    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if parsed.command in {"configure", "config", "profile"}:
            from agent_run.cli import profiles as run_profile_cli
            return run_profile_cli.execute(parsed, repository_routes=repository_routes)
        if parsed.command == "prompts":
            from agent_run.cli import prompts as prompt_cli
            return prompt_cli.execute(parsed)
        if parsed.command in {"run", "resume", "approve", "requeue", "revise", "stop", "abandon"}:
            from agent_run.cli.lifecycle import execute
            route = None
            if repository_routes is not None:
                if parsed.command == "run":
                    repository = cli_selection._repository(parsed.repo) if parsed.repo else cli_selection._cwd_repository()
                    matches = [item for item in repository_routes if item.repository == repository]
                    if len(matches) != 1:
                        raise QueryError("run_locator_conflict", "Select one exact managed repository")
                    route = matches[0]
                else:
                    selection = cli_selection.control_selection(parsed, language=language)
                    route, _ = cli_selection.repository_target(selection, repository_routes)
            output = execute(parsed, language=language, repository_route=route)
            language = output.pop("language", language)
            if parsed.as_json:
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            else:
                _print_receipt(parsed, output, language)
            return 0
        if parsed.command == "doctor":
            from agent_run.install.doctor import run as diagnose
            if parsed.state_dir is not None and parsed.run_id is None:
                raise ValueError("doctor --state-dir requires --run RUN_ID")
            frozen = None
            if parsed.run_id is not None:
                parsed.parent = None
                routes = repository_routes if repository_routes is not None else cli_selection.managed_repository_routes(parsed)
                route, location = cli_selection.repository_target(parsed, routes)
                frozen = route.read(location.run_id, "settings").data
                parsed.repo = location.task.repository
            return diagnose(as_json=parsed.as_json, local=parsed.local, online=parsed.online,
                            mode=parsed.mode, repository=parsed.repo, frozen=frozen)
        if parsed.command == "settings":
            if parsed.settings_command == "show" and (parsed.run_id is not None or parsed.parent is not None):
                routes = repository_routes if repository_routes is not None else cli_selection.managed_repository_routes(parsed)
                document = _repository_query(parsed, routes, language)
                settings, language = dict(document.data), document.language
            else:
                settings = personal_settings.execute(parsed)
            render_settings(settings, as_json=parsed.as_json, stream=sys.stdout)
            return 0
        routes = repository_routes if repository_routes is not None else cli_selection.managed_repository_routes(parsed)
        document = _repository_query(parsed, routes, language)
        style: Literal["json", "rich", "plain"] = "json" if parsed.as_json else "rich" if (not getattr(parsed, "plain", False)
                and "NO_COLOR" not in os.environ and os.environ.get("TERM", "").lower() != "dumb"
                and sys.stdout.isatty()) else "plain"
        render(document, style=style, details=getattr(parsed, "details", False), stream=sys.stdout)
        return 0
    except (DomainUnknown, ValueError, OSError, GitError, GitHubReadError, RunnerLeaseError, KernelError, HostUnavailable, HandoffError) as error:
        language = getattr(parsed, "_run_language", language)
        diagnostic_code = "execution_readiness" if isinstance(error, HostUnavailable) else str(getattr(error, "code", "command_failed"))
        error_candidates = error.candidates if isinstance(error, QueryError) else []
        state_diagnostic = (error.diagnostic() if isinstance(error, UnsupportedState)
                            else error.state_diagnostic if isinstance(error, QueryError) else None)
        missing_run_hint = (text("cli.error.run_locator_missing", language=language)
                            if isinstance(error, QueryError) and diagnostic_code == "run_locator_missing" else None)
        diagnostic = state_diagnostic or {"code": diagnostic_code, "message": missing_run_hint or str(error)}
        error_output: dict[str, Any] = {"result": "error", "status": "command_failed",
                  "diagnostics": [diagnostic]}
        if state_diagnostic is not None:
            error_output["selection"] = {key: str(value) if isinstance(value, Path) else value
                for key in ("run_id", "repo", "parent", "state_dir", "data_root")
                if (value := getattr(parsed, key, None)) is not None}
        if error_candidates:
            error_output["candidates"] = error_candidates
        if parsed.as_json:
            print(json.dumps(error_output, ensure_ascii=False, sort_keys=True))
        else:
            prefix = "命令未应用" if language == "zh" else "Command not applied"
            print(safe_text(f"{prefix}: {diagnostic_code}\n{missing_run_hint or error_detail(error, language)}"))
            if state_diagnostic is not None:
                print(safe_text(f"{diagnostic['path']}\n{diagnostic['recovery_hint']}"))
            for error_candidate in error_candidates:
                root = error_candidate["repository_root"] or ("未记录" if language == "zh" else "Not recorded")
                scope = (f"{error_candidate['repository']} Parent #{error_candidate['parent']} "
                         if error_candidate["repository"] is not None and error_candidate["parent"] is not None else "")
                print(safe_text(f"{scope}{root} [{error_candidate['database']}]"))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
