"""Read whole Markdown resources; personal methods are shared across repositories."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Any

try:
    from agent_run.paths import app_config_root
except ModuleNotFoundError:  # source-tree installer probe
    from paths import app_config_root  # type: ignore[import-not-found, no-redef]

METHOD_NAMES = (
    "development-common", "development-initial", "development-repair", "review", "publication",
)
RESOURCE_ROOT = Path(__file__).parent / "resources" / "zh"
MAX_RESOURCE_BYTES = 512 * 1024
# The manifest fixes resource identities independently of mutable files on disk.
RESOURCE_KEYS = (
    'internal/checkout',
    'internal/development-completion',
    'internal/development-delivery-boundary',
    'internal/development-output-repair',
    'internal/development-resume',
    'internal/development-resume-output',
    'internal/development-review-budget',
    'internal/development-role',
    'internal/full-requirement-url',
    'internal/git',
    'internal/human-response-boundary',
    'internal/human-response-label',
    'internal/identity-candidate',
    'internal/identity-default-base',
    'internal/identity-expected-tree',
    'internal/identity-repair-candidate',
    'internal/identity-reviewed-base',
    'internal/identity-reviewed-candidate',
    'internal/identity-reviewed-tree',
    'internal/identity-run-base',
    'internal/identity-run-head',
    'internal/initial-review-baseline',
    'internal/integration-evidence-boundary',
    'internal/integration-evidence-label',
    'internal/output',
    'internal/parent-url',
    'internal/previous-review-artifact-label',
    'internal/previous-review-boundary',
    'internal/previous-review-object-label',
    'internal/probe',
    'internal/publication-acceptance-evidence',
    'internal/publication-boundary',
    'internal/publication-fallback-boundary',
    'internal/publication-fallback-evidence',
    'internal/publication-handshake',
    'internal/publication-output',
    'internal/publication-output-repair',
    'internal/publication-resume',
    'internal/publication-resume-output',
    'internal/publication-role',
    'internal/read-only-validation',
    'internal/repair-acceptance',
    'internal/repair-completion',
    'internal/repair-evidence',
    'internal/repair-git-integrity',
    'internal/repair-human-revision',
    'internal/repair-merge-conflict',
    'internal/repair-required-checks',
    'internal/repair-requirement-refresh',
    'internal/repair-role',
    'internal/repair-resume',
    'internal/requirement-baseline',
    'internal/requirements-read-command',
    'internal/requirements-read-dependencies',
    'internal/requirements-read-order',
    'internal/requirements-source-authority',
    'internal/review-attempt-budget',
    'internal/review-budget-boundary',
    'internal/review-candidate-object',
    'internal/review-commit-object',
    'internal/review-object-label',
    'internal/review-output',
    'internal/review-finding-contract',
    'internal/review-output-repair',
    'internal/review-resume',
    'internal/review-resume-output',
    'internal/review-role',
    'internal/review-run-object',
    'internal/review-run-repair-object',
    'internal/scope-child-task',
    'internal/scope-current-regressions',
    'internal/scope-existing-work',
    'internal/scope-full-requirement',
    'internal/scope-integrated-run',
    'internal/task-url',
    'methods/development-common',
    'methods/development-initial',
    'methods/development-repair',
    'methods/publication',
    'methods/review',
)


def personal_method_directory() -> Path:
    return app_config_root() / "prompts" / "zh"


def read_builtin_resource(key: str, package_root: Path | None = None) -> str:
    if key not in RESOURCE_KEYS:
        raise ValueError(f"Unknown prompt resource: {key}")
    root = RESOURCE_ROOT if package_root is None else package_root / "resources" / "zh"
    return _read(root / f"{key}.md")


def _read(path: Path) -> str:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("resource must be a regular Markdown file")
        # Bound reads before decoding; explicit unreadability must never fall back.
        with path.open("rb") as stream:
            data = stream.read(MAX_RESOURCE_BYTES + 1)
        if len(data) > MAX_RESOURCE_BYTES:
            raise ValueError("resource exceeds 512 KiB")
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"Cannot read prompt resource {path}: {error}") from error


def builtin_resources() -> dict[str, str]:
    resources = {key: read_builtin_resource(key) for key in RESOURCE_KEYS}
    validate_resources(resources)
    return resources


def resolve_resources() -> dict[str, str]:
    resources = builtin_resources()
    for name in METHOD_NAMES:
        path = personal_method_directory() / f"{name}.md"
        # lexists semantics: broken symlinks and unreadable entries must fail.
        if path.exists() or path.is_symlink():
            resources[f"methods/{name}"] = _read(path)
        else:
            try:
                path.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ValueError(f"Cannot inspect prompt resource {path}: {error}") from error
    validate_resources(resources)
    return resources


def validate_resources(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != set(RESOURCE_KEYS):
        raise ValueError("Incompatible prompt resource snapshot: required resource identities differ")
    if any(not isinstance(value, str) for value in snapshot.values()):
        raise ValueError("Invalid prompt resource snapshot: content must be text")
    if sum(len(value.encode("utf-8")) for value in snapshot.values()) > MAX_RESOURCE_BYTES:
        raise ValueError("Prompt resource snapshot exceeds 512 KiB")


def resource(request: dict[str, Any] | None, key: str) -> str:
    if request is not None and "_prompt_resources" in request:
        snapshot = request["_prompt_resources"]
        if not isinstance(snapshot, dict) or key not in snapshot or not isinstance(snapshot[key], str):
            raise ValueError(f"Incompatible prompt resource snapshot: missing {key}")
        value = str(snapshot[key])
        return value.removesuffix("\n") if key.startswith("internal/") else value
    value = resolve_resources()[key]
    return value.removesuffix("\n") if key.startswith("internal/") else value


def bind_resources(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve current resources once, without changing the caller's request."""
    if "_prompt_resources" in request:
        return request
    return {**request, "_prompt_resources": resolve_resources()}
