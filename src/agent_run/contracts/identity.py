from agent_run.errors import GitHubIdentityError


from agent_run.errors import error_message

def _error(key: str, **values: object) -> GitHubIdentityError:
    return GitHubIdentityError(error_message(f"identity.{key}", audit=None, **values))


import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

def normalize_github_config(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("object")
    allowed = {"worker_source", "runner_source", "pat", "app", "app_read_only", "app_current_repository_only"}
    if set(raw) - allowed:
        raise _error("unknown", fields=", ".join(sorted(set(raw) - allowed)))
    result = dict(raw)
    if "worker_source" in result and result["worker_source"] not in ("host", "pat", "app"):
        raise _error("source")
    if "runner_source" in result and result["runner_source"] not in ("host", "pat", "app"):
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


from collections.abc import Mapping
from typing import Any

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
        config = normalize_github_config({"worker_source": "app", "app": raw["app_profile"],
                                          "app_read_only": raw["app_read_only"],
                                          "app_current_repository_only": raw["app_current_repository_only"]})
        return {"source": "app", "app_profile": config["app"],
                "app_read_only": config["app_read_only"],
                "app_current_repository_only": config["app_current_repository_only"]}
    return {"source": source}


from collections.abc import Mapping
from typing import Any
from agent_run.contracts.copy import text

def describe_identity(
    identity: Mapping[str, Any], language: str,
    runner_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {"worker": dict(identity), "runner": dict(runner_identity) if runner_identity is not None else None,
            "capabilities": text("identity.app_scope" if identity["source"] == "app"
                                 else "identity.native_scope", language=language)}
