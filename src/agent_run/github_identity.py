"""Non-secret GitHub identity selection and native host/PAT credential consumption."""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.github_auth_profile import GitHubAppProfileStore
from agent_run.messages import error_message, text


class GitHubIdentityError(ValueError):
    """Selected credentials are invalid; never retry with another identity."""


def _error(key: str, **values: object) -> GitHubIdentityError:
    return GitHubIdentityError(error_message(f"identity.{key}", audit=None, **values))


def normalize_github_config(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("object")
    allowed = {"worker_source", "runner_source", "pat", "app", "app_read_only", "app_current_repository_only"}
    if set(raw) - allowed:
        raise _error("unknown", fields=", ".join(sorted(set(raw) - allowed)))
    result = dict(raw)
    if "worker_source" in result and result["worker_source"] not in ("host", "pat", "app"):
        raise _error("source")
    if "runner_source" in result and result["runner_source"] not in ("host", "pat"):
        raise _error("runner_source")
    if "pat" in result:
        reference = result["pat"]
        if not isinstance(reference, Mapping) or set(reference) not in ({"env"}, {"file"}):
            raise _error("reference")
        key, value = next(iter(reference.items()))
        if not isinstance(value, str) or not value.strip():
            raise _error("reference")
        value = value.strip()
        if key == "env" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise _error("env_name")
        if key == "file":
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise _error("absolute")
            value = str(path)
        result["pat"] = {key: value}
    if "app" in result:
        profile = result["app"]
        if not isinstance(profile, Mapping) or set(profile) != {"app_id", "installation_id", "private_key_path"}:
            raise _error("app_missing")
        if any(not isinstance(value, str) or not value.strip() for value in profile.values()):
            raise _error("app_missing")
        profile = {key: value.strip() for key, value in profile.items()}
        for key in ("app_id", "installation_id"):
            value = profile[key]
            if not value.isascii() or not value.isdigit() or len(value) > 64 or not value.strip("0"):
                raise _error("app_missing")
        path = Path(profile["private_key_path"]).expanduser()
        if not path.is_absolute():
            raise _error("absolute")
        result["app"] = {**profile, "private_key_path": str(path)}
    for key in ("app_read_only", "app_current_repository_only"):
        if key in result and type(result[key]) is not bool:
            raise _error("scope_bool")
    return result


def resolve_worker_identity(
    defaults: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    *, app_profile_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze selection/references, without reading PATs or issuing credentials."""
    config = normalize_github_config({**normalize_github_config(defaults or {}),
                                      **normalize_github_config(overrides or {})})
    source = config.get("worker_source", "host")
    if source != "app" and any(config.get(key) for key in ("app_read_only", "app_current_repository_only")):
        raise _error("scope_source")
    if "worker_source" not in config and GitHubAppProfileStore(app_profile_path).path.exists():
        raise _error("migration_required")
    if source == "host":
        return {"source": "host"}
    if source == "pat":
        if "pat" not in config:
            raise _error("reference")
        return {"source": "pat", "pat": config["pat"]}
    if "app" not in config:
        raise _error("app_missing")
    return {"source": "app", "app_profile": config["app"],
            "app_read_only": config.get("app_read_only", False),
            "app_current_repository_only": config.get("app_current_repository_only", False)}


def resolve_runner_identity(
    defaults: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the independent Runner selection using the shared PAT reference."""
    config = normalize_github_config({**normalize_github_config(defaults or {}),
                                      **normalize_github_config(overrides or {})})
    source = config.get("runner_source", "host")
    if source == "host":
        return {"source": "host"}
    if "pat" not in config:
        raise _error("reference")
    return {"source": "pat", "pat": config["pat"]}


def validate_identity_snapshot(raw: object) -> dict[str, Any]:
    """Validate frozen non-secret data without consulting current defaults."""
    if not isinstance(raw, Mapping):
        raise _error("object")
    source = raw.get("source")
    keys = {"host": {"source"}, "pat": {"source", "pat"},
            "app": {"source", "app_profile", "app_read_only", "app_current_repository_only"}}
    if not isinstance(source, str) or source not in keys or set(raw) != keys[source]:
        raise _error("source")
    if source == "pat":
        return {"source": source, "pat": normalize_github_config({"pat": raw["pat"]})["pat"]}
    if source == "app":
        return resolve_worker_identity({"worker_source": "app", "app": raw["app_profile"],
                                        "app_read_only": raw["app_read_only"],
                                        "app_current_repository_only": raw["app_current_repository_only"]})
    return {"source": source}


def native_github_environment(
    identity: Mapping[str, Any], environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the selected PAT only at consumption; callers own process lifetime."""
    identity = validate_identity_snapshot(identity)
    result = dict(os.environ if environment is None else environment)
    source = identity.get("source")
    if source == "host":
        return result
    if source != "pat":
        raise _error("source")
    reference = normalize_github_config({"pat": identity.get("pat")})["pat"]
    if "env" in reference:
        token = result.get(reference["env"], "")
    else:
        try:
            path = Path(reference["file"])
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise _error("unreadable")
            token = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise _error("unreadable") from None
    token = token.strip()
    if not token:
        raise _error("empty")
    if len(token) > 65536 or any(character.isspace() or ord(character) < 32 for character in token):
        raise _error("invalid_token")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        result.pop(name, None)
    result["GH_TOKEN"] = token
    result["GH_PROMPT_DISABLED"] = "1"
    return result


def validate_worker_auth(
    identity: Mapping[str, Any], environment: Mapping[str, str] | None = None,
    *, repository: str | None = None,
) -> None:
    """Bounded native gh authentication probe; tool output may contain secrets."""
    identity = validate_identity_snapshot(identity)
    if identity.get("source") == "app":
        from agent_run.github_auth import _GitHubAppCredentialProvider
        from agent_run.github_auth_profile import GitHubAppProfile
        if identity["app_current_repository_only"] and repository is None:
            raise _error("app_repository")
        profile = identity["app_profile"]
        provider = _GitHubAppCredentialProvider(
            GitHubAppProfile(profile["app_id"], profile["installation_id"], Path(profile["private_key_path"])),
            read_only=identity["app_read_only"],
            repository=repository if identity["app_current_repository_only"] else None,
        )
        try:
            provider()
        except Exception:
            raise _error("auth_failed", source="app") from None
        finally:
            provider.cancel()
        return
    selected = native_github_environment(identity, environment)
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "user"],
                                env=selected, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise _error("auth_failed", source=identity.get("source")) from None
    if result.returncode:
        raise _error("auth_failed", source=identity.get("source"))


def add_identity_options(parser: argparse.ArgumentParser, language: str) -> None:
    parser.add_argument("--worker-github-source", choices=("host", "pat", "app"),
                        help=text("identity.source_help", language=language))
    parser.add_argument("--runner-github-source", choices=("host", "pat"),
                        help=text("identity.runner_source_help", language=language))
    for option in ("app-id", "installation-id", "private-key-path"):
        parser.add_argument(f"--github-{option}", help=text("identity.app_help", language=language))
    for option in ("read-only", "current-repository-only"):
        parser.add_argument(f"--github-app-{option}", action=argparse.BooleanOptionalAction,
                            default=None, help=text("identity.scope_help", language=language))
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--github-pat-env", help=text("identity.env_help", language=language))
    references.add_argument("--github-pat-file", help=text("identity.file_help", language=language))


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


def describe_identity(
    identity: Mapping[str, Any], language: str,
    runner_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {"worker": dict(identity), "runner": dict(runner_identity) if runner_identity is not None else None,
            "capabilities": text("identity.app_scope" if identity["source"] == "app"
                                 else "identity.native_scope", language=language)}
