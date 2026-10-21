"""Read-only status/history/discovery entry point for native Kernel databases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Literal, NoReturn, Sequence

from agent_run.cli_messages import personal_language as _personal_language
from agent_run.messages import error_detail

from . import personal_settings
from .query import QueryDocument, QueryError, discover, read
from .query_render import render, render_settings, safe_text


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


def _parser(language: str) -> argparse.ArgumentParser:
    parser = _Parser(language=language, prog="agent-run", description="只读交付查询" if language == "zh" else "Read-only delivery queries")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
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
    personal_settings.add_parsers(commands, language)
    return parser


def _repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise QueryError("run_locator_invalid", "Repository must be OWNER/NAME")
    return value.lower()


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


def _candidates(parsed: argparse.Namespace) -> list[dict[str, Any]]:
    default_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "agent-run"
    roots = [parsed.state_dir] if parsed.state_dir is not None else [getattr(parsed, "data_root", None) or default_root]
    result: dict[str, dict[str, Any]] = {}
    for root in roots:
        for candidate in discover(root):
            result[str(Path(candidate["database"]).resolve())] = candidate
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
    except (ValueError, OSError) as error:
        diagnostic_code = error.code if isinstance(error, QueryError) else "command_failed"
        error_candidates = error.candidates if isinstance(error, QueryError) else []
        error_output: dict[str, Any] = {"result": "error", "status": "command_failed",
                  "diagnostics": [{"code": diagnostic_code, "message": str(error)}]}
        if error_candidates:
            error_output["candidates"] = error_candidates
        if parsed.as_json:
            print(json.dumps(error_output, ensure_ascii=False, sort_keys=True))
        else:
            prefix = "命令未应用" if language == "zh" else "Command not applied"
            print(safe_text(f"{prefix}: {diagnostic_code}\n{error_detail(error, language)}"))
            for error_candidate in error_candidates:
                root = error_candidate["repository_root"] or ("未记录" if language == "zh" else "Not recorded")
                print(safe_text(f"{error_candidate['repository']} Parent #{error_candidate['parent']} {error_candidate['run_id']} {root} [{error_candidate['state_dir']}]"))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
