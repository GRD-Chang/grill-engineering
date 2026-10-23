"""Non-secret GitHub identity selection and native host/PAT credential consumption."""
from __future__ import annotations

from agent_run.contracts.identity import _error
from agent_run.contracts.identity import normalize_github_config
from agent_run.contracts.identity import validate_identity_snapshot


import argparse
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.config.github_app import GitHubAppProfileStore
from agent_run.errors import error_message
from agent_run.contracts.copy import text








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
    """Freeze Runner selection; Worker restrictions do not narrow Runner access."""
    config = normalize_github_config({**normalize_github_config(defaults or {}),
                                      **normalize_github_config(overrides or {})})
    source = config.get("runner_source", "host")
    if source == "host":
        return {"source": "host"}
    if source == "app":
        if "app" not in config:
            raise _error("app_missing")
        return {"source": "app", "app_profile": config["app"],
                "app_read_only": False, "app_current_repository_only": False}
    if "pat" not in config:
        raise _error("reference")
    return {"source": "pat", "pat": config["pat"]}




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
        from agent_run.adapters.auth.app import _GitHubAppCredentialProvider
        from agent_run.config.github_app import GitHubAppProfile
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
