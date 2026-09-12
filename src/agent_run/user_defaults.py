from __future__ import annotations

"""One sparse, user-editable authority for defaults of future Delivery Runs."""

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.agent_profiles import (
    DEFAULT_PRESET,
    AgentProfileError,
    ProfileOverrides,
    resolve_profiles,
)
from agent_run.delivery_policy import (
    DeliveryPolicyError,
    normalize_policy_overrides,
    resolve_delivery_policy,
)

SCOPE_NOTICE = "仅影响之后创建的新 Run，已有 Run 保持原设置"
_PROFILE_KEYS = frozenset(
    {"preset", "publication_from_development"}
    | {f"{role}_{field}" for role in ("development", "review", "publication")
       for field in ("model", "effort")}
)


class UserDefaultsError(ValueError):
    """User defaults cannot be parsed or safely updated."""


def _profile(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UserDefaultsError("profile 必须是对象")
    for key, value in raw.items():
        if key not in _PROFILE_KEYS:
            raise UserDefaultsError(f"未知 profile 配置项: {key}")
        if value is None:
            raise UserDefaultsError(f"profile.{key} 不允许 null")
    result = dict(raw)
    preset = result.get("preset")
    if preset is not None and not isinstance(preset, str):
        raise UserDefaultsError("profile.preset 必须是字符串")
    try:
        resolve_profiles(preset=preset, overrides={k: v for k, v in result.items() if k != "preset"})
    except AgentProfileError as error:
        raise UserDefaultsError(f"profile: {error}") from error
    for key, value in result.items():
        if key.endswith("_model"):
            result[key] = value.strip()
    return result


def _document(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UserDefaultsError("用户默认配置必须是对象")
    unknown = set(raw) - {"policy", "profile"}
    if unknown:
        raise UserDefaultsError(f"未知配置项: {sorted(unknown)}")
    result: dict[str, Any] = {}
    if "policy" in raw:
        try:
            result["policy"] = normalize_policy_overrides(raw["policy"])
            resolve_delivery_policy(user_defaults=result["policy"])
        except DeliveryPolicyError as error:
            raise UserDefaultsError(f"policy: {error}") from error
    if "profile" in raw:
        result["profile"] = _profile(raw["profile"])
    return result


def _merge_profile(current: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    merged = {**current, **supplied}
    if supplied.get("publication_from_development") is True:
        for key in ("publication_model", "publication_effort"):
            merged.pop(key, None)
    elif any(key in supplied for key in ("publication_model", "publication_effort")):
        merged.pop("publication_from_development", None)
    return merged


class UserDefaultsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else self.default_path()
        self.legacy_path = self.path.with_name("delivery-policy.json")

    @staticmethod
    def default_path() -> Path:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
        if not root.is_absolute():
            raise UserDefaultsError("XDG_CONFIG_HOME 必须是绝对路径")
        return root / "agent-run" / "user-defaults.json"

    def _read(self) -> tuple[dict[str, Any], str]:
        path = self.path if self.path.exists() else self.legacy_path
        if not path.exists():
            return {}, "builtin"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            document = _document(raw if path == self.path else {"policy": raw})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, UserDefaultsError) as error:
            raise UserDefaultsError(f"{path}: {error}") from error
        return document, "user-defaults" if path == self.path else "legacy-delivery-policy"

    def load(self) -> dict[str, Any]:
        return self._read()[0]

    def _describe(self, document: dict[str, Any], source: str) -> dict[str, Any]:
        profile = document.get("profile", {})
        resolved = resolve_profiles(
            preset=profile.get("preset"),
            overrides={k: v for k, v in profile.items() if k != "preset"},
        )
        sources: dict[str, Any] = {"preset": source if "preset" in profile else "builtin"}
        for role, values in resolved["profiles"].items():
            sources[role] = {
                field: ("development" if role == "publication" and values["reference"] else
                        source if f"{role}_{option}" in profile else f"preset:{resolved['preset']}")
                for field, option in (("model", "model"), ("reasoning_effort", "effort"))
            }
            sources[role]["reference"] = values["reference"]
        policy = resolve_delivery_policy(user_defaults=document.get("policy")).snapshot()
        policy_defaults = document.get("policy", {})
        policy_sources: dict[str, Any] = {
            key: source if key in policy_defaults else "builtin"
            for key in policy if key != "invocation_deadlines"
        }
        policy_sources["invocation_deadlines"] = {
            role: source if role in policy_defaults.get("invocation_deadlines", {}) else "builtin"
            for role in policy["invocation_deadlines"]
        }
        return {
            "result": "settings",
            "path": str(self.path), "source": source,
            "source_path": str(self.legacy_path if source == "legacy-delivery-policy" else self.path) if source != "builtin" else None,
            "legacy_ignored": source == "user-defaults" and self.legacy_path.exists(),
            "defaults": document,
            "policy": policy, "policy_sources": policy_sources,
            "profile": resolved, "profile_sources": sources,
            "scope": "future-runs", "notice": SCOPE_NOTICE,
        }

    def describe(self, document: dict[str, Any] | None = None) -> dict[str, Any]:
        if document is not None:
            return self._describe(_document(document), "provided")
        return self._describe(*self._read())

    def configure(self, *, policy: Mapping[str, Any] | None = None,
                  profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
        supplied = _document({**({"policy": policy} if policy is not None else {}),
                              **({"profile": profile} if profile is not None else {})})
        if not any(supplied.values()):
            raise UserDefaultsError("configure requires an explicit option")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self.load()
                merged = dict(current)
                if "policy" in supplied:
                    prior = current.get("policy", {})
                    update = supplied["policy"]
                    merged["policy"] = {**prior, **update}
                    if "invocation_deadlines" in update:
                        merged["policy"]["invocation_deadlines"] = {
                            **prior.get("invocation_deadlines", {}), **update["invocation_deadlines"]}
                if "profile" in supplied:
                    merged["profile"] = _merge_profile(current.get("profile", {}), supplied["profile"])
                merged = _document(merged)
                self._write(merged)
                result = self._describe(merged, "user-defaults")
                result["result"] = "configured"
                result["changed"] = supplied
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def resolve_creation(self, *, preset: str | None = None,
                         overrides: ProfileOverrides | None = None,
                         document: dict[str, Any] | None = None) -> tuple[str, ProfileOverrides]:
        loaded = self.load() if document is None else _document(document)
        profile = loaded.get("profile", {}) if preset is None else {}
        selected = preset or profile.get("preset", DEFAULT_PRESET)
        supplied = {k: v for k, v in (overrides or {}).items() if v is not None and v is not False}
        _profile({"preset": selected, **supplied})
        merged = _merge_profile({k: v for k, v in profile.items() if k != "preset"}, supplied)
        resolve_profiles(preset=selected, overrides=merged)
        return selected, merged

    def _write(self, document: Mapping[str, Any]) -> None:
        rollback: Path | None = None
        temporary: Path | None = None
        replaced = False
        try:
            if self.path.exists():
                descriptor, name = tempfile.mkstemp(
                    dir=self.path.parent, prefix=".user-defaults.rollback.", suffix=".tmp"
                )
                rollback = Path(name)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(self.path.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
            descriptor, name = tempfile.mkstemp(
                dir=self.path.parent, prefix=".user-defaults.", suffix=".tmp"
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            replaced = True
            self._sync_directory()
        except OSError as error:
            if replaced:
                try:
                    if rollback is None:
                        self.path.unlink()
                    else:
                        os.replace(rollback, self.path)
                except OSError as rollback_error:
                    recovery_path = rollback
                    rollback = None  # Preserve recoverable original bytes after rollback failure.
                    raise OSError(
                        f"{self.path}: 写入失败且无法恢复原配置: {rollback_error}; "
                        f"原配置备份: {recovery_path}; 原错误: {error}"
                    ) from error
                try:
                    self._sync_directory()
                except OSError as sync_error:
                    raise OSError(
                        f"{self.path}: 写入失败，已恢复原配置，但无法确认恢复的磁盘持久性: {sync_error}"
                    ) from error
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if rollback is not None:
                rollback.unlink(missing_ok=True)

    def _sync_directory(self) -> None:
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
