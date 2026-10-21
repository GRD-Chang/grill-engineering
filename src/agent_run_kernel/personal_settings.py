"""Existing personal-default semantics exposed by the native public parser.

This module never maps policy into Kernel state or stores Run/Profile data.
UserDefaultsStore remains the sole personal-file validator and atomic writer.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agent_run.cli_messages import personal_language
from agent_run.github_identity import add_identity_options, identity_overrides
from agent_run.messages import text
from agent_run.user_defaults import UserDefaultsStore

from .query import QueryError


_POSITIVE_POLICY = (
    ("parent_only_paired_rounds", "--parent-only-paired-round", "parent_rounds_help"),
    ("run_repair_rounds", "--run-repair-round", "repair_rounds_help"),
    ("ticket_review_rounds", "--ticket-review-round", "ticket_rounds_help"),
    ("ticket_development_workers", None, "ticket_development_workers_help"),
    ("ticket_integration_development_limit", None, "ticket_integration_development_limit_help"),
    ("ticket_ci_development_limit", None, "ticket_ci_development_limit_help"),
    ("parent_ci_development_limit", None, "parent_ci_development_limit_help"),
)


def _positive(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(text("cli.positive_integer", language=personal_language()))


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
        if number >= 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(text("cli.nonnegative_integer", language=personal_language()))


def _duration(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError(text("cli.positive_duration", language=personal_language()))
    return value  # The existing policy leaf validates units, finiteness and positivity.


def _policy_options(parser: argparse.ArgumentParser, language: str) -> None:
    for key, alias, message in _POSITIVE_POLICY:
        options = ("--" + key.replace("_", "-"),) + ((alias,) if alias else ())
        parser.add_argument(*options, dest=key, type=_positive, help=text("cli." + message, language=language))
    parser.add_argument("--development-thread-policy", choices=("reuse", "new-per-attempt"),
                        help=text("cli.thread_policy_help", language=language))
    parser.add_argument("--integration-resync-limit", type=_nonnegative,
                        help=text("cli.integration_resync_limit_help", language=language))
    parser.add_argument("--ticket-ci-review", action=argparse.BooleanOptionalAction, default=None,
                        help=text("cli.ticket_ci_review_help", language=language))
    for role, label in (("development", "Development"), ("review", "Review"), ("publication", "Publication")):
        parser.add_argument(f"--{role}-deadline", f"--{role}-duration", f"--{role}-timeout", dest=f"{role}_deadline",
                            type=_duration, help=text("cli.deadline_help", language=language, role=label))


def add_parsers(commands: Any, language: str) -> None:
    settings = commands.add_parser("settings", language=language, help=text("settings.help", language=language))
    actions = settings.add_subparsers(dest="settings_command", required=True)
    show = actions.add_parser("show", language=language, help=text("settings.show_help", language=language))
    show.add_argument("--run", dest="run_id", help=text("settings.run_help", language=language))
    show.add_argument("--parent", type=_positive, help=text("settings.parent_help", language=language))
    show.add_argument("--repo", help=text("cli.repo_help", language=language))
    show.add_argument("--state-dir", type=Path, help=text("cli.state_dir_help", language=language))
    show.add_argument("--json", action="store_true", dest="as_json")
    configure = actions.add_parser("configure", language=language, help=text("settings.configure_help", language=language))
    configure.add_argument("--language", metavar="{zh,en}", help=text("settings.language_help", language=language))
    add_identity_options(configure, language)
    configure.add_argument("--migrate-github-app", action="store_true", help=text("identity.migrate_help", language=language))
    _policy_options(configure, language)
    configure.add_argument("--preset", "--profile-preset", dest="profile_preset", help=text("cli.preset_help", language=language))
    configure.add_argument("--development-model", "--dev-model", dest="development_model", help=text("cli.dev_model_help", language=language))
    configure.add_argument("--development-effort", "--development-reasoning-effort", "--dev-effort", dest="development_effort", help=text("cli.dev_effort_help", language=language))
    configure.add_argument("--review-model", help=text("cli.review_model_help", language=language))
    configure.add_argument("--review-effort", "--review-reasoning-effort", dest="review_effort", help=text("cli.review_effort_help", language=language))
    configure.add_argument("--publication-model", help=text("cli.publication_model_help", language=language))
    configure.add_argument("--publication-effort", "--publication-reasoning-effort", dest="publication_effort", help=text("cli.publication_effort_help", language=language))
    configure.add_argument("--publication-from-development", "--publication-reference-development", "--publication-use-development",
                           action="store_true", default=None, dest="publication_from_development", help=text("cli.publication_reference_help", language=language))
    configure.add_argument("--publication-reference", choices=("development",), help=argparse.SUPPRESS)
    toggle = configure.add_mutually_exclusive_group()
    toggle.add_argument("--notifications", dest="notifications_enabled", action="store_true", default=None, help=text("settings.notifications_help", language=language))
    toggle.add_argument("--no-notifications", dest="notifications_enabled", action="store_false", help=text("settings.no_notifications_help", language=language))
    toggle.add_argument("--notification-mode", choices=("concise", "detailed"), help=text("settings.notification_mode_help", language=language))
    for name in ("open-id", "profile", "app-id"):
        configure.add_argument(f"--notification-{name}", help=text("settings.notification_binding_help", language=language))
    configure.add_argument("--json", action="store_true", dest="as_json")
    policy = commands.add_parser("policy", language=language, help=text("cli.policy_help", language=language))
    policy.add_argument("--json", action="store_true", dest="as_json")
    policy_commands = policy.add_subparsers(dest="policy_command")
    policy_show = policy_commands.add_parser("show", language=language, help=text("cli.policy_show_help", language=language))
    policy_show.add_argument("--json", action="store_true", dest="as_json")
    policy_configure = policy_commands.add_parser("configure", language=language, help=text("cli.policy_configure_help", language=language))
    _policy_options(policy_configure, language)
    policy_configure.add_argument("--json", action="store_true", dest="as_json")


def _policy_values(parsed: argparse.Namespace) -> dict[str, Any]:
    keys = tuple(item[0] for item in _POSITIVE_POLICY) + ("development_thread_policy", "integration_resync_limit", "ticket_ci_review")
    result = {key: getattr(parsed, key) for key in keys if getattr(parsed, key, None) is not None}
    deadlines = {role: getattr(parsed, role + "_deadline") for role in ("development", "review", "publication")
                 if getattr(parsed, role + "_deadline", None) is not None}
    if deadlines:
        result["invocation_deadlines"] = deadlines
    return result


def execute(parsed: argparse.Namespace) -> dict[str, Any]:
    store = UserDefaultsStore()
    if parsed.command == "policy":
        result = store.configure(policy=_policy_values(parsed)) if parsed.policy_command == "configure" else store.describe()
        result["result"] = "configured" if parsed.policy_command == "configure" else "policy"
        result["user_defaults"] = result["defaults"].get("policy", {})
        return result
    if parsed.settings_command == "show":
        if parsed.run_id is not None or parsed.parent is not None:
            raise QueryError("run_locator_invalid", "Select a canonical Run before reading its frozen configuration")
        if parsed.repo or parsed.state_dir:
            raise ValueError(text("settings.run_required", language=store.language()))
        return store.describe()
    profile = {f"{role}_{field}": getattr(parsed, f"{role}_{field}") for role in ("development", "review", "publication")
               for field in ("model", "effort") if getattr(parsed, f"{role}_{field}") is not None}
    if parsed.profile_preset is not None:
        profile["preset"] = parsed.profile_preset
    if parsed.publication_from_development or parsed.publication_reference == "development":
        profile["publication_from_development"] = True
    notifications = {key: value for key, value in {
        "enabled": True if parsed.notification_mode else parsed.notifications_enabled, "mode": parsed.notification_mode,
        "open_id": parsed.notification_open_id, "profile": parsed.notification_profile, "app_id": parsed.notification_app_id,
    }.items() if value is not None}
    return store.configure(policy=_policy_values(parsed), profile=profile, language=parsed.language,
                           github=identity_overrides(parsed), migrate_github_app=parsed.migrate_github_app,
                           notifications=notifications or None)
