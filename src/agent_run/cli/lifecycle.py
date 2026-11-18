"""Parse lifecycle input; runtime owns Task and Host coordination."""
from __future__ import annotations
import argparse
from pathlib import Path
from functools import partial
from typing import Any, TYPE_CHECKING
from agent_run.cli import settings as personal_settings
from agent_run.contracts.copy import text
if TYPE_CHECKING:
    from agent_run.storage.routing import RepositoryRoute

def add_parsers(commands: Any, language: str) -> None:
    """Retain the established public arguments; unsupported actions stay explicit."""
    for command in ("run", "resume", "approve", "requeue", "revise", "stop", "abandon"):
        child = commands.add_parser(command, language=language, help=text(f"cli.{command}_help", language=language))
        child.add_argument("parent" if command == "run" else "run_id",
                           type=partial(personal_settings._positive, language=language) if command == "run" else str)
        child.add_argument("--repo", help=text("cli.repo_help", language=language))
        child.add_argument("--state-dir", type=Path, help=text("cli.state_dir_help", language=language))
        child.add_argument("--json", action="store_true", dest="as_json")
        if command != "stop":
            child.add_argument("--foreground", action="store_true", default=None,
                               help="Run the admitted Host in this terminal; never fall back from systemd")
        if command in {"resume", "stop"}:
            child.add_argument("--ticket", type=partial(personal_settings._positive, language=language),
                               help=text(f"cli.{command}_ticket_help", language=language))
        if command in {"resume", "revise"}:
            child.add_argument("--message", required=command == "revise",
                               help=text("cli.response_help" if command == "resume" else "cli.feedback_help", language=language))
        if command == "resume":
            child.add_argument("--new-thread", action="store_true", help=text("cli.new_thread_help", language=language))
            personal_settings._policy_options(child, language, allow_thread_policy=False)
        if command == "abandon":
            child.add_argument("--discard-worktree", action="store_true", help=text("cli.discard_help", language=language))
        if command == "run":
            personal_settings._policy_options(child, language)
            personal_settings._profile_options(child, language)
            from agent_run.cli.identity_options import add_identity_options
            add_identity_options(child, language)
            notify = child.add_mutually_exclusive_group()
            notify.add_argument("--notification-mode", choices=("concise", "detailed"), help=text("cli.notifications_help", language=language))
            notify.add_argument("--no-notifications", action="store_true", help=text("cli.no_notifications_help", language=language))


def execute(parsed: argparse.Namespace, *, language: str,
            repository_route: RepositoryRoute | None = None) -> dict[str, Any]:
    from agent_run.cli.identity_options import identity_overrides
    from agent_run.runtime.run_control import execute as control
    parsed._policy_overrides = personal_settings._policy_values(parsed)
    if parsed.command == "run":
        parsed._identity_overrides = identity_overrides(parsed)
    return control(parsed, language=language, repository_route=repository_route)
