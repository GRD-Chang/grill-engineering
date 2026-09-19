"""Public defaults and frozen Run configuration views; no execution side effects."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_run.agent_profiles import AgentProfileStore, ProfileOverrides
from agent_run.delivery_policy import policy_snapshot_for_state
from agent_run.user_defaults import UserDefaultsStore, UserDefaultsError
from agent_run.messages import DEFAULT_LANGUAGE, text


def add_parser(
    commands: Any,
    common_options: Callable[..., None],
    policy_options: Callable[..., None],
    profile_options: Callable[..., None],
) -> None:
    # Building help must not make existing Run operations depend on current defaults.
    try:
        language = UserDefaultsStore().language()
    except (UserDefaultsError, OSError):
        language = DEFAULT_LANGUAGE
    def message(key: str) -> str:
        return text(f"settings.{key}", language=language)
    parser = commands.add_parser("settings", help=message("help"))
    actions = parser.add_subparsers(dest="settings_command", required=True)
    show = actions.add_parser("show", help=message("show_help"))
    common_options(show)
    show.add_argument("--run", dest="run_id", help=message("run_help"))
    show.add_argument("--parent", type=int, help=message("parent_help"))
    show.add_argument("--json", action="store_true", dest="as_json")
    configure = actions.add_parser("configure", help=message("configure_help"))
    configure.add_argument("--language", metavar="{zh,en}", help=message("language_help"))
    policy_options(configure)
    profile_options(configure)
    notification_toggle = configure.add_mutually_exclusive_group()
    notification_toggle.add_argument(
        "--notifications", dest="notifications_enabled", action="store_true",
        default=None, help=message("notifications_help"),
    )
    notification_toggle.add_argument(
        "--no-notifications", dest="notifications_enabled", action="store_false",
        help=message("no_notifications_help"),
    )
    notification_toggle.add_argument(
        "--notification-mode", choices=("concise", "detailed"),
        help=message("notification_mode_help"),
    )
    for name in ("open-id", "profile", "app-id"):
        configure.add_argument(f"--notification-{name}", help=message("notification_binding_help"))
    configure.add_argument("--json", action="store_true", dest="as_json")


def execute(
    parsed: argparse.Namespace,
    *,
    policy_overrides: Callable[[argparse.Namespace], dict[str, Any]],
    profile_configuration: Callable[[argparse.Namespace], tuple[str | None, ProfileOverrides]],
    load_run: Callable[[argparse.Namespace], dict[str, Any]],
    profile_root: Callable[[argparse.Namespace], Path],
) -> int:
    if parsed.settings_command == "configure":
        preset, overrides = profile_configuration(parsed)
        profile = {key: value for key, value in overrides.items() if value is not None}
        if preset is not None:
            profile["preset"] = preset
        result = UserDefaultsStore().configure(
            policy=policy_overrides(parsed), profile=profile, language=parsed.language,
            notifications={key: value for key, value in {
                "enabled": True if parsed.notification_mode else parsed.notifications_enabled,
                "mode": parsed.notification_mode,
                "open_id": parsed.notification_open_id,
                "profile": parsed.notification_profile,
                "app_id": parsed.notification_app_id,
            }.items() if value is not None} or None,
        )
    elif parsed.run_id is not None or parsed.parent is not None:
        state = load_run(parsed)
        language = state.get("language", DEFAULT_LANGUAGE)
        parsed.display_language = language
        parsed.run_id = state["run_id"]
        document = AgentProfileStore(profile_root(parsed)).load(parsed.run_id)
        initializing = document is None and _profile_initialization_pending(state)
        if document is None and not initializing:
            raise ValueError(text("settings.missing_profile", language=language))
        result = {
            "result": "settings", "scope": "run", "run_id": parsed.run_id,
            "language": language,
            "policy": policy_snapshot_for_state(state),
            "notifications": state.get("notifications", {"enabled": False}),
            "profile": (
                {key: document[key] for key in ("preset", "profiles", "profile_revision")}
                if document is not None else None
            ),
            "source": "initializing" if initializing else "run_snapshot",
            "bindings": document.get("bindings", []) if document is not None else [],
            "message": (
                text("settings.initializing", language=language)
                if initializing else
                text("settings.run_notice", language=language)
            ),
        }
    else:
        if parsed.repo or parsed.state_dir:
            raise ValueError(text("settings.run_required", language=UserDefaultsStore().language()))
        result = UserDefaultsStore().describe()
    if parsed.as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(text("settings.run_title" if result.get("scope") == "run" else "settings.personal_title",
                   language=result["language"]))
        print_fields(result, language=result["language"])
    return 0


def _profile_initialization_pending(state: dict[str, Any]) -> bool:
    """Recognize the persisted creation boundary before profile initialization."""
    return (
        state.get("status") == "starting"
        and state.get("base_resolution_pending") is True
        and state.get("currentness_resolution_pending") is True
        and state.get("action_application_receipt") is None
        and not state.get("active_agent_invocation")
        and not state.get("agent_invocation_history")
        and not any(state.get(key) for key in (
            "active_ticket_job", "ticket_jobs", "parent_job", "run_acceptance", "run_publication",
        ))
    )


def print_fields(values: dict[str, Any], *, language: str, indent: int = 0) -> None:
    """Present known configuration labels; preserve values and unknown machine keys."""
    for key, value in values.items():
        try:
            label = text(f"cli.settings.field.{key}", language=language)
        except KeyError:
            label = key
        prefix = " " * indent + label + ":"
        if isinstance(value, dict) and value:
            print(prefix)
            print_fields(value, language=language, indent=indent + 2)
        elif isinstance(value, list) and value:
            print(prefix)
            for item in value:
                if isinstance(item, dict):
                    print_fields(item, language=language, indent=indent + 2)
                else:
                    print(" " * (indent + 2) + str(item))
        else:
            if value is None or value == [] or value == {}:
                rendered = text("cli.settings.unset", language=language)
            elif isinstance(value, bool):
                rendered = text("cli.settings.yes" if value else "cli.settings.no", language=language)
            else:
                rendered = str(value)
            print(f"{prefix} {rendered}")
