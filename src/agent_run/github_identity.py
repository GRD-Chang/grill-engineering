"""Non-secret GitHub identity selection and native host/PAT credential consumption."""
from __future__ import annotations

import argparse
import os
import re
import stat
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
    if set(raw) - {"worker_source", "pat"}:
        raise _error("unknown", fields=", ".join(sorted(set(raw) - {"worker_source", "pat"})))
    result = dict(raw)
    if "worker_source" in result and result["worker_source"] not in ("host", "pat", "app"):
        raise _error("source")
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
    return result


def resolve_worker_identity(
    defaults: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    *, app_profile_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze selection/references, without reading PATs or issuing credentials."""
    config = normalize_github_config({**normalize_github_config(defaults or {}),
                                      **normalize_github_config(overrides or {})})
    source = config.get("worker_source")
    if source == "host":
        return {"source": "host"}
    if source == "pat":
        if "pat" not in config:
            raise _error("reference")
        return {"source": "pat", "pat": config["pat"]}
    profile = GitHubAppProfileStore(app_profile_path).load()
    if profile is None:
        if source == "app":
            raise _error("app_missing")
        return {"source": "host"}
    return {"source": "app", "app_profile": {
        "app_id": profile.app_id, "installation_id": profile.installation_id,
        "private_key_path": str(profile.private_key_path),
    }}


def validate_identity_snapshot(raw: object) -> dict[str, Any]:
    """Validate frozen non-secret data without consulting current defaults."""
    if not isinstance(raw, Mapping):
        raise _error("object")
    source = raw.get("source")
    keys = {"host": {"source"}, "pat": {"source", "pat"},
            "app": {"source", "app_profile"}}
    if not isinstance(source, str) or source not in keys or set(raw) != keys[source]:
        raise _error("source")
    if source == "pat":
        return {"source": source, "pat": normalize_github_config({"pat": raw["pat"]})["pat"]}
    if source == "app":
        profile = raw["app_profile"]
        if not isinstance(profile, Mapping) or set(profile) != {
            "app_id", "installation_id", "private_key_path",
        } or any(not isinstance(value, str) or not value.strip() for value in profile.values()):
            raise _error("app_missing")
        if not Path(profile["private_key_path"]).is_absolute():
            raise _error("app_missing")
        return {"source": source, "app_profile": dict(profile)}
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
) -> None:
    """Bounded native gh authentication probe; tool output may contain secrets."""
    identity = validate_identity_snapshot(identity)
    if identity.get("source") == "app":
        return  # Existing App provider owns validation and renewal during transition.
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
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--github-pat-env", help=text("identity.env_help", language=language))
    references.add_argument("--github-pat-file", help=text("identity.file_help", language=language))


def identity_overrides(parsed: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if getattr(parsed, "worker_github_source", None) is not None:
        result["worker_source"] = parsed.worker_github_source
    for key in ("env", "file"):
        value = getattr(parsed, f"github_pat_{key}", None)
        if value is not None:
            result["pat"] = {key: value}
    return normalize_github_config(result)


def describe_identity(identity: Mapping[str, Any], language: str) -> dict[str, Any]:
    return {"worker": dict(identity), "runner": {"source": "host"},
            "capabilities": text("identity.app_scope" if identity["source"] == "app"
                                 else "identity.native_scope", language=language)}
