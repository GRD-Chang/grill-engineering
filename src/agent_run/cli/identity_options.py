from agent_run.contracts.identity import normalize_github_config


import argparse
from agent_run.contracts.copy import text

def add_identity_options(parser: argparse.ArgumentParser, language: str) -> None:
    parser.add_argument("--worker-github-source", choices=("host", "pat", "app"),
                        help=text("identity.source_help", language=language))
    parser.add_argument("--runner-github-source", choices=("host", "pat", "app"),
                        help=text("identity.runner_source_help", language=language))
    for option in ("app-id", "installation-id", "private-key-path"):
        parser.add_argument(f"--github-{option}", help=text("identity.app_help", language=language))
    for option in ("read-only", "current-repository-only"):
        parser.add_argument(f"--github-app-{option}", action=argparse.BooleanOptionalAction,
                            default=None, help=text("identity.scope_help", language=language))
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--github-pat-env", help=text("identity.env_help", language=language))
    references.add_argument("--github-pat-file", help=text("identity.file_help", language=language))


from typing import Any

def identity_overrides(parsed: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if getattr(parsed, "worker_github_source", None) is not None:
        result["worker_source"] = parsed.worker_github_source
    if getattr(parsed, "runner_github_source", None) is not None:
        result["runner_source"] = parsed.runner_github_source
    for key in ("env", "file"):
        value = getattr(parsed, f"github_pat_{key}", None)
        if value is not None:
            result["pat"] = {key: value}
    app = {key: getattr(parsed, f"github_{key}", None)
           for key in ("app_id", "installation_id", "private_key_path")}
    if any(value is not None for value in app.values()):
        result["app"] = app
    for key in ("app_read_only", "app_current_repository_only"):
        value = getattr(parsed, f"github_{key}", None)
        if value is not None:
            result[key] = value
    return normalize_github_config(result)
