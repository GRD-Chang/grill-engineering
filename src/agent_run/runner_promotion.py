"""Immutable Runner Structured Outputs promotion handshake."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import agent_run

from agent_run.agent_schemas import publication_or_human_blocker_schema
from agent_run.codex import CodexCliBackend

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_INCONCLUSIVE = re.compile(
    r"auth(?:entication)?|credential|unauthorized|forbidden|invalid[ _-]?(?:api[ _-]?)?key|"
    r"invalid token|\b40[13]\b|\b429\b|"
    r"network|connection|econn(?:refused|reset)|enotfound|socket hang up|dns|tls|ssl|"
    r"certificate|rate.?limit|too many requests|throttl(?:ed|ing)?|timeout|timed out",
    re.IGNORECASE,
)
_SCHEMA_REJECTION = re.compile(
    r"invalid_json_schema|schema.{0,80}(?:invalid|reject)|"
    r"(?:invalid|reject).{0,80}schema",
    re.IGNORECASE,
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization|proxy-authorization)"
    r"([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?"
    r"([^\s,;\"'}]+)"
)
_SECRET = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password|private[ _-]?key)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
)
_QUOTED_SECRET = re.compile(
    r"(?is)\b(token|api[_-]?key|secret|password|private[ _-]?key)\b[\"']?"
    r"\s*[:=]\s*([\"']).*?\2"
)
_MULTILINE_PRIVATE_KEY = re.compile(
    r"(?is)\bprivate[ _-]?key\b[\"']?\s*[:=]\s*"
    r"(?:[\"']?-----BEGIN .*?-----END [^-]*-----|[^\r\n]*(?:\r?\n[^\r\n]*)+)"
)
_MAX_ERROR_LENGTH = 8 * 1024


class PublicationHandshakeBackend(Protocol):
    def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]: ...


@dataclass(frozen=True)
class PromotionVerification:
    runner_commit_sha: str
    runner_python: str
    runner_module: str
    runner_package_sha256: str


def verify_immutable_runner(checkout: Path, runner_sha: str) -> PromotionVerification:
    """Verify this non-editable Runner and checkout are pinned to one SHA."""

    if not _COMMIT_SHA.fullmatch(runner_sha):
        raise ValueError("runner SHA must be a lowercase 40-character commit SHA")
    module = Path(agent_run.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix not in module.parents:
        raise ValueError("Runner must import agent_run from its own non-editable environment")
    source_package = checkout / "src" / "agent_run"
    if _package_sha256(module.parent) != _package_sha256(source_package):
        raise ValueError("Runner package does not match the immutable checkout source")
    if _git(checkout, "rev-parse", "HEAD") != runner_sha:
        raise ValueError("checkout HEAD does not match the requested Runner SHA")
    if _git(checkout, "status", "--porcelain"):
        raise ValueError("Runner checkout must be clean")
    if _git(checkout, "rev-parse", "origin/main") != runner_sha:
        raise ValueError("Runner SHA must match the current origin/main")
    attached = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "--quiet", "HEAD"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if attached.returncode == 0:
        raise ValueError("Runner checkout must be detached at the immutable SHA")
    return PromotionVerification(
        runner_commit_sha=runner_sha,
        runner_python=str(Path(sys.executable).resolve()),
        runner_module=str(module),
        runner_package_sha256=_package_sha256(module.parent),
    )


def run_promotion_handshake(
    *,
    checkout: Path,
    audit_file: Path,
    backend: PublicationHandshakeBackend | None = None,
    verification: PromotionVerification,
    codex_version: str | None = None,
) -> dict[str, object]:
    """Run one production-boundary schema handshake and persist its audit record."""

    if audit_file.resolve().is_relative_to(checkout.resolve()):
        raise ValueError("promotion audit must be outside the immutable Runner checkout")
    if audit_file.exists():
        raise ValueError(f"promotion audit already exists: {audit_file}")
    started_at = _now()
    schema_sha256 = _schema_sha256()
    active_backend = backend or CodexCliBackend()
    try:
        _output, thread_id = active_backend.publication_schema_handshake(checkout)
    except Exception as error:
        verdict, bounded_error = _classify_error(error)
        record: dict[str, object] = {
            **_record_identity(verification, schema_sha256, codex_version),
            "started_at_utc": started_at,
            "finished_at_utc": _now(),
            "credential_redaction": "failed",
            "sandbox": "not_completed",
            "handshake_verdict": verdict,
            "thread_id_present": False,
            "bounded_error": bounded_error,
        }
    else:
        record = {
            **_record_identity(verification, schema_sha256, codex_version),
            "started_at_utc": started_at,
            "finished_at_utc": _now(),
            "credential_redaction": "passed",
            "sandbox": "passed",
            "handshake_verdict": "passed",
            "thread_id_present": bool(thread_id),
            "bounded_error": None,
        }
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def codex_cli_version() -> str:
    result = subprocess.run(
        ["codex", "--version"], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise ValueError("could not determine Codex CLI version")
    return result.stdout.strip()


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("could not verify immutable Runner checkout")
    return result.stdout.strip()


def _record_identity(
    verification: PromotionVerification, schema_sha256: str, codex_version: str | None
) -> dict[str, object]:
    return {
        "runner_commit_sha": verification.runner_commit_sha,
        "runner_python": verification.runner_python,
        "runner_module": verification.runner_module,
        "runner_package_sha256": verification.runner_package_sha256,
        "codex_cli_version": codex_version or codex_cli_version(),
        "publication_schema_sha256": schema_sha256,
    }


def _schema_sha256() -> str:
    encoded = json.dumps(
        publication_or_human_blocker_schema(), sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _package_sha256(package: Path) -> str:
    if not package.is_dir():
        raise ValueError("could not read Runner package provenance")
    digest = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        digest.update(str(source.relative_to(package)).encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _classify_error(error: Exception) -> tuple[str, str]:
    message = _bounded_error(str(error))
    if _SCHEMA_REJECTION.search(message):
        return "failed", message
    if _INCONCLUSIVE.search(message):
        return "inconclusive", message
    return "failed", message


def _bounded_error(message: str) -> str:
    private_key_redacted = _MULTILINE_PRIVATE_KEY.sub("private_key=[REDACTED]", message)
    clean = _CONTROL_CHARACTERS.sub(" ", private_key_redacted)
    redacted = _AUTHORIZATION.sub(r"\1\2[REDACTED]", clean)
    redacted = _QUOTED_SECRET.sub(r"\1=[REDACTED]", redacted)
    redacted = _SECRET.sub(r"\1\2[REDACTED]", redacted)
    return redacted[:_MAX_ERROR_LENGTH]


def _now() -> str:
    return datetime.now(UTC).isoformat()
