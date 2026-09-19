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
from agent_run.paths import app_config_root
from agent_run.messages import DEFAULT_LANGUAGE, ErrorMessage, error_message, text, validate_language

_PROFILE_KEYS = frozenset(
    {"preset", "publication_from_development"}
    | {f"{role}_{field}" for role in ("development", "review", "publication")
       for field in ("model", "effort")}
)


class UserDefaultsError(ValueError):
    """User defaults cannot be parsed or safely updated."""


def _profile(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UserDefaultsError(error_message("defaults.profile_object"))
    for key, value in raw.items():
        if key not in _PROFILE_KEYS:
            raise UserDefaultsError(error_message("defaults.profile_unknown", field=key))
        if value is None:
            raise UserDefaultsError(error_message("defaults.profile_null", field=key))
    result = dict(raw)
    preset = result.get("preset")
    if preset is not None and not isinstance(preset, str):
        raise UserDefaultsError(error_message("defaults.preset_string"))
    try:
        resolve_profiles(preset=preset, overrides={k: v for k, v in result.items() if k != "preset"})
    except AgentProfileError as error:
        reason = error.args[0] if error.args and isinstance(error.args[0], ErrorMessage) else str(error)
        raise UserDefaultsError(error_message("defaults.profile_error", reason=reason)) from error
    for key, value in result.items():
        if key.endswith("_model"):
            result[key] = value.strip()
    return result


def notification_snapshot(raw: object = None, *, disabled: bool = False, mode: str | None = None) -> dict[str, Any]:
    """Resolve optional notification settings without preventing Run creation."""
    result: dict[str, Any] = {"enabled": False, "open_id": None, "profile": None, "app_id": None, "mode": "concise"}
    if disabled:
        return result
    try:
        result.update(_notifications(raw) if raw is not None else {})
        if mode is not None:
            result.update(_notifications({"mode": mode}))
            result["enabled"] = True
    except UserDefaultsError as error:
        result["unavailable_reason"] = str(error)
    return result


def _notifications(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UserDefaultsError(error_message("defaults.notifications_object"))
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "enabled":
            if type(value) is not bool:
                raise UserDefaultsError(error_message("defaults.notifications_bool"))
        elif key == "mode":
            if value not in ("concise", "detailed"):
                raise UserDefaultsError(error_message("defaults.notifications_mode"))
        elif key in {"open_id", "profile", "app_id"}:
            if not isinstance(value, str) or not value.strip():
                raise UserDefaultsError(error_message("defaults.notifications_string", field=key))
            value = value.strip()
        else:
            raise UserDefaultsError(error_message("defaults.notifications_unknown", field=key))
        result[key] = value
    return result


def _document(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UserDefaultsError(error_message("defaults.document_object"))
    unknown = set(raw) - {"policy", "profile", "notifications", "language"}
    if unknown:
        raise UserDefaultsError(error_message("defaults.document_unknown", keys=sorted(unknown)))
    result: dict[str, Any] = {}
    if "language" in raw:
        try:
            result["language"] = validate_language(raw["language"])
        except ValueError as error:
            raise UserDefaultsError(str(error)) from error
    if "policy" in raw:
        try:
            result["policy"] = normalize_policy_overrides(raw["policy"])
            resolve_delivery_policy(user_defaults=result["policy"])
        except DeliveryPolicyError as error:
            reason = error.args[0] if error.args and isinstance(error.args[0], ErrorMessage) else str(error)
            raise UserDefaultsError(error_message("defaults.policy_error", reason=reason)) from error
    if "profile" in raw:
        result["profile"] = _profile(raw["profile"])
    if "notifications" in raw:
        result["notifications"] = raw["notifications"]
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
        try:
            return app_config_root() / "user-defaults.json"
        except ValueError as error:
            reason = error.args[0] if error.args and isinstance(error.args[0], ErrorMessage) else str(error)
            raise UserDefaultsError(reason) from error

    def _read(self) -> tuple[dict[str, Any], str]:
        path = self.path if self.path.exists() else self.legacy_path
        if not path.exists():
            return {}, "builtin"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            document = _document(raw if path == self.path else {"policy": raw})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, UserDefaultsError) as error:
            reason = error.args[0] if error.args and isinstance(error.args[0], ErrorMessage) else str(error)
            raise UserDefaultsError(error_message("defaults.read_error", path=path, reason=reason)) from error
        return document, "user-defaults" if path == self.path else "legacy-delivery-policy"

    def load(self) -> dict[str, Any]:
        return self._read()[0]

    def language(self, document: dict[str, Any] | None = None) -> str:
        """Resolve the single personal language setting, never the host locale."""
        loaded = self.load() if document is None else _document(document)
        return validate_language(loaded.get("language", DEFAULT_LANGUAGE))

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
            "notifications": notification_snapshot(document.get("notifications")),
            "language": self.language(document),
            "language_source": source if "language" in document else "builtin",
            "scope": "future-runs",
            "notice": text("settings.scope_notice", language=self.language(document)),
        }

    def describe(self, document: dict[str, Any] | None = None) -> dict[str, Any]:
        if document is not None:
            return self._describe(_document(document), "provided")
        return self._describe(*self._read())

    def configure(self, *, policy: Mapping[str, Any] | None = None,
                  profile: Mapping[str, Any] | None = None,
                  notifications: Mapping[str, Any] | None = None,
                  language: str | None = None) -> dict[str, Any]:
        supplied = _document({**({"policy": policy} if policy is not None else {}),
                              **({"profile": profile} if profile is not None else {})})
        if language is not None:
            try:
                supplied["language"] = validate_language(language)
            except ValueError as error:
                raise UserDefaultsError(text("settings.invalid_language", language=self.language())) from error
        if notifications is not None:
            supplied["notifications"] = _notifications(notifications)
        if not any(supplied.values()):
            raise UserDefaultsError(error_message(
                "defaults.configure_option", audit="configure requires an explicit option"
            ))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self.load()
                merged = dict(current)
                if "language" in supplied:
                    merged["language"] = supplied["language"]
                if "policy" in supplied:
                    prior = current.get("policy", {})
                    update = supplied["policy"]
                    merged["policy"] = {**prior, **update}
                    if "invocation_deadlines" in update:
                        merged["policy"]["invocation_deadlines"] = {
                            **prior.get("invocation_deadlines", {}), **update["invocation_deadlines"]}
                if "profile" in supplied:
                    merged["profile"] = _merge_profile(current.get("profile", {}), supplied["profile"])
                if "notifications" in supplied:
                    prior_notifications = current.get("notifications", {})
                    if not isinstance(prior_notifications, Mapping):
                        prior_notifications = {}
                    merged["notifications"] = _notifications({**prior_notifications, **supplied["notifications"]})
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
