"""Public native query and personal-settings entry point."""
from __future__ import annotations

import argparse
from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Literal, NoReturn, Sequence

from agent_run.cli_messages import personal_language as _personal_language
from agent_run.messages import error_detail
from agent_run.git_errors import GitError
from agent_run.github import GitHubReadError
from agent_run.runner_lease import RunnerLeaseError
from agent_run.managed_workspace import ManagedWorkspace, normalize_repository
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey

from .model import KernelError
from .host_adapter import HostUnavailable
from .native_host import HandoffError

from . import personal_settings
from .query import QueryDocument, QueryError, discover, read, task_binding, task_published
from .query_render import render, render_settings, safe_text
from .task_location import read_task_bindings
from .state_preflight import UnsupportedState


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, language: str, **kwargs: Any) -> None:
        self.language = language
        super().__init__(*args, **kwargs)
        if language == "zh":
            self._positionals.title = "位置参数"
            self._optionals.title = "选项"

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
        help="只读运行环境诊断" if language == "zh" else "Read-only runtime diagnostics")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    personal_settings.add_parsers(commands, language)
    from . import run_profile_cli
    run_profile_cli.add_parser(commands, language)
    from agent_run import prompt_cli
    prompt_cli.add_parser(commands)
    from .lifecycle_cli import add_parsers
    add_parsers(commands, language)
    return parser


def _repository(value: str) -> str:
    try:
        return normalize_repository(value)
    except GitError as error:
        raise QueryError("run_locator_invalid", "Repository must be OWNER/NAME") from error


def _cwd_repository() -> str | None:
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                                timeout=3, check=False, env=env)
        if result.returncode or len(result.stdout) > 4096:
            return None
        url = result.stdout.strip().removesuffix(".git")
        match = re.fullmatch(r"(?:https?://[^/]+/|ssh://[^/]+/|[^/@]+@[^:]+:)([^/]+/[^/]+)", url)
        return _repository(match[1]) if match else None
    except (OSError, subprocess.SubprocessError, QueryError):
        return None


def _managed_state_roots(repository: str | None) -> list[Path]:
    """Locate durable state without walking clones, worktrees or installations."""
    base = app_data_root() / "repositories"

    def directory(path: Path) -> bool:
        if path.is_symlink():
            raise QueryError("run_locator_invalid", f"Symbolic managed state path: {path}")
        if not path.exists():
            return False
        if not path.is_dir():
            raise QueryError("run_locator_invalid", f"Managed state path is not a directory: {path}")
        return True

    if repository is not None:
        owner_name, repository_name = repository.split("/")
        paths = (base, base / owner_name, base / owner_name / repository_name,
                 base / owner_name / repository_name / "state")
        for path in paths:
            directory(path)
        return [paths[-1]]
    if not directory(base):
        return []
    roots = []
    visited = 0
    for owner in base.iterdir():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", owner.name):
            continue
        if not directory(owner):
            continue
        for checkout in owner.iterdir():
            visited += 1
            if visited > 4096:
                raise QueryError("query_limit", "Too many managed repositories; provide --repo or --state-dir")
            try:
                _repository(owner.name + "/" + checkout.name)
            except QueryError:
                continue
            if directory(checkout):
                directory(checkout / "state")
                roots.append(checkout / "state")
    return roots


def _candidates(parsed: argparse.Namespace) -> list[dict[str, Any]]:
    published: dict[Path, str] | None = None
    repository: str | None = None
    selected_task: TaskKey | None = None
    if parsed.state_dir is not None:
        roots = [parsed.state_dir]
    elif getattr(parsed, "data_root", None) is not None:
        roots = [parsed.data_root]
    else:
        repository = _repository(parsed.repo) if parsed.repo else None
        if repository is None and not getattr(parsed, "run_id", None):
            repository = _cwd_repository()
        roots = _managed_state_roots(repository)
        published = {}
        base = app_data_root() / "repositories"
        if repository is not None and parsed.parent is not None:
            workspace = ManagedWorkspace.for_repository(repository)
            selected_task = TaskKey(workspace.repository_root, repository, parsed.parent)
            binding = task_binding(selected_task)
            bindings = iter(()) if binding is None else iter((binding,))
        else:
            bindings = read_task_bindings()
        for run_id, database in bindings:
            exact = getattr(parsed, "run_id", None)
            if exact and run_id != exact:
                continue
            try:
                relative = database.relative_to(base)
            except ValueError:
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication is outside managed state")
                continue
            if len(relative.parts) < 4 or relative.parts[2] != "state":
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication is outside managed state")
                continue
            try:
                bound_repository = _repository(relative.parts[0] + "/" + relative.parts[1])
            except QueryError:
                continue
            if repository is not None and bound_repository != repository:
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication names another managed repository")
                continue
            scoped_root = _managed_state_roots(bound_repository)[0]
            if scoped_root not in roots:
                roots.append(scoped_root)
            path = database.resolve()
            if not path.is_relative_to(scoped_root.resolve()):
                raise QueryError("run_locator_conflict", f"Task database escaped its managed state directory: {database}")
            if path in published and published[path] != run_id:
                raise QueryError("run_locator_conflict", f"Conflicting Task publications: {path}")
            published[path] = run_id
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for root in roots:
        for candidate in discover(root, published=published, run_id=getattr(parsed, "run_id", None),
                                  repository=repository or (_repository(parsed.repo) if parsed.repo else None),
                                  parent=parsed.parent):
            if published is not None:
                exact = getattr(parsed, "run_id", None)
                if (selected_task is not None and Path(candidate["database"]).resolve() in published
                        and (candidate["repository"].lower() != selected_task.repository
                             or candidate["parent"] != selected_task.parent_number
                             or candidate["repository_root"] != str(selected_task.workspace))):
                    raise QueryError("run_locator_conflict", "Task publication and database identities differ")
                if ((repository is not None and candidate["repository"].lower() != repository)
                        or (parsed.parent is not None and candidate["parent"] != parsed.parent)
                        or (exact and candidate["run_id"] != exact)):
                    continue
                if not task_published(candidate) and not candidate["is_terminal"]:
                    continue
            result[(str(Path(candidate["database"]).resolve()), candidate["run_id"])] = candidate
            if len(result) > 256:
                raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    return list(result.values())


def _select(parsed: argparse.Namespace, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact = getattr(parsed, "run_id", None)
    if (parsed.parent is not None and parsed.parent < 1) or (exact and parsed.parent is not None):
        raise QueryError("run_locator_invalid", "Choose a positive --parent or an exact Run ID")
    repository = _repository(parsed.repo) if parsed.repo else None
    if repository and parsed.command != "runs" and not (exact or parsed.parent):
        raise QueryError("run_locator_invalid", "--repo requires --parent or an exact Run ID")
    if not exact and repository is None:
        repository = _cwd_repository()
        if repository is None:
            raise QueryError("workspace_required", "Use --repo OWNER/NAME with --parent, or an exact Run ID with --state-dir")
    matches = [item for item in candidates if (not exact or item["run_id"] == exact)
               and (repository is None or item["repository"].lower() == repository)
               and (parsed.parent is None or item["parent"] == parsed.parent)]
    if parsed.command == "runs":
        return sorted(matches, key=lambda item: (item["repository"], item["parent"], item["run_id"]))
    if (not exact and parsed.parent is not None and parsed.state_dir is None
            and getattr(parsed, "data_root", None) is None):
        current = [item for item in matches if task_published(item)]
        if current:
            matches = current
    if not exact and parsed.parent is None:
        matches = [item for item in matches if not item["is_terminal"]]
    if not matches:
        raise QueryError("run_locator_missing", "No matching Run; use runs to inspect candidates")
    if len(matches) != 1:
        raise QueryError("run_locator_conflict", "Multiple Runs or clones match; provide an exact Run ID and state directory", candidates=matches)
    return matches


def main(arguments: Sequence[str] | None = None) -> int:
    language = _personal_language()
    parser = _parser(language)
    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if parsed.command in {"configure", "config", "profile"}:
            from . import run_profile_cli
            return run_profile_cli.execute(parsed)
        if parsed.command == "prompts":
            from agent_run import prompt_cli
            return prompt_cli.execute(parsed)
        if parsed.command in {"run", "resume", "approve", "requeue", "revise", "stop", "abandon"}:
            from .lifecycle_cli import execute
            output = execute(parsed)
            language = output.pop("language", language)
            if parsed.as_json:
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            else:
                if output["result"] == "attached":
                    label = "已附着原请求；本次操作未应用" if language == "zh" else "Attached to original request; new action not applied"
                else:
                    label = "请求已受理" if language == "zh" else "Request accepted"
                host_label = "宿主" if language == "zh" else "Host"
                print(safe_text(f"{label}: {output['run_id']} {output['status']} [{output['action_id']}] {host_label}: {output['host']}"))
            return 0
        if parsed.command == "doctor":
            from agent_run.doctor import run as diagnose
            return diagnose(as_json=parsed.as_json)
        if parsed.command in {"settings", "policy"}:
            if parsed.command == "settings" and parsed.settings_command == "show" and (parsed.run_id is not None or parsed.parent is not None):
                candidate = _select(parsed, _candidates(parsed))[0]
                if candidate.get("language") in {"zh", "en"}:
                    language = candidate["language"]
                document = read(Path(candidate["database"]), "settings", expected_run=candidate["run_id"],
                                expected_repository=candidate["repository"].lower())
                settings = dict(document.data)
                language = document.language
            else:
                settings = personal_settings.execute(parsed)
            render_settings(settings, as_json=parsed.command == "policy" or parsed.as_json, stream=sys.stdout)
            return 0
        selected = _select(parsed, _candidates(parsed))
        if parsed.command == "runs":
            document = QueryDocument("runs", language, {"result": "runs", "runs": selected})
        else:
            candidate = selected[0]
            if candidate.get("language") in {"zh", "en"}:
                language = candidate["language"]
            kind: Literal["history", "status"] = "history" if parsed.command == "history" else "status"
            document = read(Path(candidate["database"]), kind,
                            after=getattr(parsed, "after", 0), limit=getattr(parsed, "limit", None),
                            expected_run=candidate["run_id"], expected_repository=candidate["repository"].lower())
        style: Literal["json", "rich", "plain"] = "json" if parsed.as_json else "rich" if (not getattr(parsed, "plain", False)
                and "NO_COLOR" not in os.environ and os.environ.get("TERM", "").lower() != "dumb"
                and sys.stdout.isatty()) else "plain"
        render(document, style=style, details=getattr(parsed, "details", False), stream=sys.stdout)
        return 0
    except (ValueError, OSError, GitError, GitHubReadError, RunnerLeaseError, KernelError, HostUnavailable, HandoffError) as error:
        language = getattr(parsed, "_run_language", language)
        diagnostic_code = "execution_readiness" if isinstance(error, HostUnavailable) else str(getattr(error, "code", "command_failed"))
        error_candidates = error.candidates if isinstance(error, QueryError) else []
        state_diagnostic = (error.diagnostic() if isinstance(error, UnsupportedState)
                            else error.state_diagnostic if isinstance(error, QueryError) else None)
        diagnostic = state_diagnostic or {"code": diagnostic_code, "message": str(error)}
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
            print(safe_text(f"{prefix}: {diagnostic_code}\n{error_detail(error, language)}"))
            if state_diagnostic is not None:
                print(safe_text(f"{diagnostic['path']}\n{diagnostic['recovery_hint']}"))
            for error_candidate in error_candidates:
                root = error_candidate["repository_root"] or ("未记录" if language == "zh" else "Not recorded")
                scope = (f"{error_candidate['repository']} Parent #{error_candidate['parent']} "
                         if error_candidate["repository"] is not None and error_candidate["parent"] is not None else "")
                print(safe_text(f"{scope}{error_candidate['run_id']} {root} [{error_candidate['state_dir']}]"))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
