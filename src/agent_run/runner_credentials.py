"""Command-lifetime ownership of renewable program-side App credentials."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from agent_run.credential_lifecycle import ReadCredential, RenewableCredential, safe_http_status
from agent_run.github_auth import _GitHubAppCredentialProvider
from agent_run.github_auth_profile import GitHubAppProfile
from agent_run.github_identity import GitHubIdentityError, native_github_environment, validate_worker_auth


class RunnerCredentialError(GitHubIdentityError):
    """The shared Runner identity is known to be unusable."""


def _credential_failure(message: str, authorization_failed: bool) -> Exception:
    if authorization_failed:
        return RunnerCredentialError(message)
    from agent_run.github import GitHubReadError

    return GitHubReadError("runner_credential_unavailable", message)


class RunnerAppCredential:
    """Reuse shared renewal machinery; keep secrets outside frozen Run facts."""

    def __init__(self, identity: Mapping[str, Any]) -> None:
        profile = identity["app_profile"]
        self._lock = threading.Lock()
        self._closed = False
        self._credential: ReadCredential | None = None
        self._failure = "Runner App credential unavailable; repair credentials and run agent-run resume"
        provider = _GitHubAppCredentialProvider(GitHubAppProfile(
            profile["app_id"], profile["installation_id"], Path(profile["private_key_path"]),
        ))
        self.lifecycle = RenewableCredential(provider, on_update=self._update, on_exhausted=self._exhausted)
        try:
            self.lifecycle.start()
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            status = safe_http_status(getattr(error, "http_status", None))
            detail = f" (HTTP {status})" if status is not None else ""
            raise _credential_failure(
                self._failure + detail, getattr(error, "authorization_failed", False) is True,
            ) from None

    def _update(self, credential: ReadCredential) -> None:
        with self._lock:
            self._credential = credential

    def _exhausted(self, reason: str) -> None:
        with self._lock:
            self._credential = None
            self._failure = reason.replace("worker_credential", "runner_credential").replace("Worker", "Runner")

    def token(self) -> str:
        with self._lock:
            if self._closed:
                raise GitHubIdentityError("Runner credential is closed")
            if self._credential is None or self._credential.expires_at <= time.time():
                raise _credential_failure(self._failure, self.lifecycle.failure_authorization_failed)
            return self._credential.token

    def close(self) -> None:
        self.lifecycle.close()
        with self._lock:
            self._closed = True
            self._credential = None


_scope: ContextVar[dict[str, RunnerAppCredential] | None] = ContextVar("runner_credentials", default=None)


@contextmanager
def runner_credential_scope() -> Iterator[None]:
    """One CLI command owns its credentials, including cancellation and failures."""
    credentials: dict[str, RunnerAppCredential] = {}
    binding = _scope.set(credentials)
    try:
        yield
    finally:
        _scope.reset(binding)
        for credential in credentials.values():
            credential.close()


def runner_app_token(identity: Mapping[str, Any]) -> str:
    credentials = _scope.get()
    if credentials is None:
        with runner_credential_scope():
            return runner_app_token(identity)
    if identity.get("app_read_only") or identity.get("app_current_repository_only"):
        raise RunnerCredentialError("Runner App identity cannot inherit Worker restrictions")
    key = json.dumps(dict(identity), sort_keys=True)
    if key not in credentials:
        credentials[key] = RunnerAppCredential(identity)
    return credentials[key].token()


def validate_runner_auth(identity: Mapping[str, Any]) -> None:
    if identity["source"] == "app":
        runner_app_token(identity)
    else:
        validate_worker_auth(identity)


def runner_credential_environment(identity: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, str]:
    if identity["source"] != "app":
        try:
            return native_github_environment(identity, environment)
        except GitHubIdentityError as error:
            raise RunnerCredentialError(str(error)) from error
    result = dict(environment)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        result.pop(key, None)
    result.update(GH_TOKEN=runner_app_token(identity), GH_PROMPT_DISABLED="1")
    return result
