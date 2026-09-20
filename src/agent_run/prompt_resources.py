"""Read complete role bodies and fixed prompt copy, shared across repositories."""

from __future__ import annotations

import json
from pathlib import Path
import stat
from typing import Any

from agent_run.messages import ErrorMessage, error_message, validate_language

from agent_run.paths import app_config_root

METHOD_NAMES = ("development", "repair", "acceptance", "publishing")
RESOURCE_ROOT = Path(__file__).parent / "resources" / "zh"
MAX_RESOURCE_BYTES = 512 * 1024
# The manifest fixes resource identities independently of mutable files on disk.
COPY_KEYS = (
    'internal/checkout',
    'internal/full-requirement-url',
    'internal/identity-candidate',
    'internal/identity-default-base',
    'internal/identity-expected-tree',
    'internal/identity-repair-candidate',
    'internal/identity-reviewed-base',
    'internal/identity-reviewed-candidate',
    'internal/identity-reviewed-tree',
    'internal/identity-run-base',
    'internal/identity-run-head',
    'internal/parent-url',
    'internal/previous-review-artifact-label',
    'internal/previous-review-object-label',
    'internal/publication-acceptance-evidence',
    'internal/repair-evidence',
    'internal/review-object-label',
    'internal/task-url',
)
MARKDOWN_KEYS = (
    'internal/development-output-repair',
    'internal/development-resume',
    'internal/development-review-budget',
    'internal/git',
    'internal/human-response',
    'internal/initial-review-baseline',
    'internal/integration-evidence',
    'internal/output',
    'internal/previous-review-boundary',
    'internal/probe',
    'internal/publication-boundary',
    'internal/publication-fallback',
    'internal/publication-handshake',
    'internal/publication-output',
    'internal/publication-output-repair',
    'internal/publication-resume',
    'internal/read-only-validation',
    'internal/repair-acceptance',
    'internal/repair-git-integrity',
    'internal/repair-human-revision',
    'internal/repair-merge-conflict',
    'internal/repair-required-checks',
    'internal/repair-requirement-refresh',
    'internal/repair-resume',
    'internal/requirement-baseline',
    'internal/requirements-read',
    'internal/requirements-read-dependencies',
    'internal/review-attempt-budget',
    'internal/review-candidate-object',
    'internal/review-commit-object',
    'internal/review-output',
    'internal/review-output-repair',
    'internal/review-resume',
    'internal/review-run-object',
    'internal/review-run-repair-object',
    'internal/scope-child-task',
    'internal/scope-current-regressions',
    'internal/scope-existing-work',
    'internal/scope-full-requirement',
    'internal/scope-integrated-run',
    'methods/acceptance',
    'methods/development',
    'methods/publishing',
    'methods/repair',
)
RESOURCE_KEYS = MARKDOWN_KEYS + COPY_KEYS


def selected_language(language: str | None = None) -> str:
    if language is None:
        from agent_run.user_defaults import UserDefaultsStore
        language = UserDefaultsStore().language()
    return validate_language(language)


def personal_method_directory(language: str | None = None) -> Path:
    return app_config_root() / "prompts" / selected_language(language)


def read_builtin_resource(key: str, package_root: Path | None = None, *, language: str | None = None) -> str:
    if key not in RESOURCE_KEYS:
        raise ValueError(error_message(
            "prompts.error.unknown", audit=f"Unknown prompt resource: {key}", resource=key
        ))
    language = selected_language(language)
    if package_root is None:
        root = RESOURCE_ROOT if language == "zh" else RESOURCE_ROOT.parent / language
    else:
        root = package_root / "resources" / language
    if key in COPY_KEYS:
        return _read_copy(root)[key]
    return _read(root / f"{key}.md")


def _read_copy(root: Path) -> dict[str, str]:
    path = root / "prompt-copy.json"
    try:
        values = json.loads(_read(path))
        if not isinstance(values, dict) or set(values) != set(COPY_KEYS):
            raise ValueError("prompt copy identities differ")
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("prompt copy must contain non-empty text")
        return values
    except (ValueError, TypeError) as error:
        raise ValueError(error_message(
            "prompts.error.read", audit=f"Cannot read prompt resource {path}: {error}",
            path=path, reason=str(error),
        )) from error


def _read(path: Path) -> str:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(error_message(
                "prompts.error.regular", audit="resource must be a regular Markdown file"
            ))
        # Bound reads before decoding; explicit unreadability must never fall back.
        with path.open("rb") as stream:
            data = stream.read(MAX_RESOURCE_BYTES + 1)
        if len(data) > MAX_RESOURCE_BYTES:
            raise ValueError(error_message("prompts.error.size", audit="resource exceeds 512 KiB"))
        text = data.decode("utf-8")
        if not text.strip():
            raise ValueError("resource must contain non-empty text")
        return text
    except (OSError, UnicodeError, ValueError) as error:
        reason = error.args[0] if error.args and isinstance(error.args[0], ErrorMessage) else str(error)
        raise ValueError(error_message(
            "prompts.error.read", audit=f"Cannot read prompt resource {path}: {error}",
            path=path, reason=reason,
        )) from error


def builtin_resources(language: str | None = None) -> dict[str, str]:
    language = selected_language(language)
    resources = {key: read_builtin_resource(key, language=language) for key in MARKDOWN_KEYS}
    root = RESOURCE_ROOT if language == "zh" else RESOURCE_ROOT.parent / language
    resources.update(_read_copy(root))
    validate_resources(resources)
    return resources


def resolve_resources(language: str | None = None) -> dict[str, str]:
    language = selected_language(language)
    resources = builtin_resources(language)
    for name in METHOD_NAMES:
        path = personal_method_directory(language) / f"{name}.md"
        # lexists semantics: broken symlinks and unreadable entries must fail.
        if path.exists() or path.is_symlink():
            resources[f"methods/{name}"] = _read(path)
        else:
            try:
                path.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ValueError(error_message(
                    "prompts.error.inspect", audit=f"Cannot inspect prompt resource {path}: {error}",
                    path=path, reason=str(error),
                )) from error
    validate_resources(resources)
    return resources


def validate_resources(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != set(RESOURCE_KEYS):
        raise ValueError(error_message(
            "prompts.error.identities",
            audit="Incompatible prompt resource snapshot: required resource identities differ",
        ))
    if any(not isinstance(value, str) or not value.strip() for value in snapshot.values()):
        raise ValueError(error_message(
            "prompts.error.content", audit="Invalid prompt resource snapshot: content must be non-empty text"
        ))
    if sum(len(value.encode("utf-8")) for value in snapshot.values()) > MAX_RESOURCE_BYTES:
        raise ValueError(error_message(
            "prompts.error.snapshot_size", audit="Prompt resource snapshot exceeds 512 KiB"
        ))


def resource(request: dict[str, Any] | None, key: str) -> str:
    if request is not None and "_prompt_resources" in request:
        snapshot = request["_prompt_resources"]
        if not isinstance(snapshot, dict) or key not in snapshot or not isinstance(snapshot[key], str):
            raise ValueError(error_message(
                "prompts.error.missing", audit=f"Incompatible prompt resource snapshot: missing {key}",
                resource=key,
            ))
        value = str(snapshot[key])
        return value.removesuffix("\n") if key.startswith("internal/") else value
    value = resolve_resources(request.get("language") if request is not None else None)[key]
    return value.removesuffix("\n") if key.startswith("internal/") else value


def bind_resources(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve current resources once, without changing the caller's request."""
    if "_prompt_resources" in request:
        return request
    return {**request, "_prompt_resources": resolve_resources(request.get("language"))}
