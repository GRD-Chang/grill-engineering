"""Renew short-lived credentials independently of their tool consumers."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

WORKER_CREDENTIAL_LIFETIME_SECONDS = 3600
WORKER_RENEWAL_MARGIN_SECONDS = 300
WORKER_RENEWAL_WINDOW_SECONDS = 600
WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS = 15.0
WORKER_CREDENTIAL_PROVIDER_TIMEOUT_SECONDS = 30.0


def safe_http_status(value: object) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


class WorkerCredentialError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = safe_http_status(http_status)


class InitialCredentialUnavailable(WorkerCredentialError):
    """Initial authentication failed before a consumer started."""


@dataclass(frozen=True)
class ReadCredential:
    token: str = field(repr=False)
    expires_at: float


# Providers own bounded I/O and implement cancel() for interruptible operations.
CredentialProvider = Callable[[], ReadCredential | str]


class RenewableCredential:
    def __init__(
        self, provider: CredentialProvider, *,
        on_update: Callable[[ReadCredential], None],
        on_exhausted: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        renewal_margin: float = WORKER_RENEWAL_MARGIN_SECONDS,
        renewal_window: float = WORKER_RENEWAL_WINDOW_SECONDS,
    ) -> None:
        self._provider = provider
        self._on_update = on_update
        self._on_exhausted = on_exhausted
        self._clock = clock
        self._margin = renewal_margin
        self._window = renewal_window
        self._condition = threading.Condition()
        self._closed = False
        self._credential: ReadCredential | None = None
        self._renewal_thread: threading.Thread | None = None
        self._expiry_thread: threading.Thread | None = None
        self._last_status: int | None = None
        self._attempts = 0

    def start(self) -> None:
        try:
            self._update()
        except Exception as error:
            self.close()
            raise WorkerCredentialError(
                "Could not create initial Worker credential: " + (
                    "invalid credential" if getattr(error, "retryable", None) is False
                    or isinstance(error, WorkerCredentialError) else "provider unavailable"
                ),
                http_status=getattr(error, "http_status", None),
            ) from None
        with self._condition:
            if self._closed:
                raise InitialCredentialUnavailable("Credential lifecycle is closed")
            self._renewal_thread = threading.Thread(
                target=self._renew, name="agent-run-credential-renewal", daemon=True,
            )
            self._expiry_thread = threading.Thread(
                target=self._watch_expiry, name="agent-run-credential-expiry", daemon=True,
            )
            self._renewal_thread.start()
            self._expiry_thread.start()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._credential = None
            self._condition.notify_all()
        try:
            self._cancel_provider()
        finally:
            for thread in (self._renewal_thread, self._expiry_thread):
                if (thread is not None and thread.ident is not None
                        and thread is not threading.current_thread()):
                    thread.join(timeout=WORKER_CREDENTIAL_PROVIDER_TIMEOUT_SECONDS + 1)

    def _cancel_provider(self) -> None:
        cancel = getattr(self._provider, "cancel", None)
        if callable(cancel):
            cancel()

    def _watch_expiry(self) -> None:
        # Signing/network I/O must never delay removing an expired credential.
        with self._condition:
            while not self._closed:
                assert self._credential is not None
                remaining = self._credential.expires_at - self._clock()
                if remaining <= 0:
                    self._closed = True
                    self._credential = None
                    self._condition.notify_all()
                    break
                self._condition.wait(remaining)
            else:
                return
        try:
            if self._on_exhausted is not None:
                status = f"; HTTP {self._last_status}" if self._last_status else ""
                self._on_exhausted(
                    "worker_credential_renewal_failed: Worker credential renewal failed "
                    f"after {self._attempts} attempts{status}; repair the selected credential "
                    "and run agent-run resume"
                )
        finally:
            self._cancel_provider()

    def _update(self) -> None:
        value = self._provider()
        if isinstance(value, str):
            value = ReadCredential(value, self._clock() + WORKER_CREDENTIAL_LIFETIME_SECONDS)
        if (not isinstance(value, ReadCredential) or not value.token.strip()
                or not math.isfinite(value.expires_at) or value.expires_at <= self._clock()):
            raise WorkerCredentialError("Credential provider returned an invalid credential")
        with self._condition:
            if self._closed:
                return
            # Serialize delivery and close so no token can be published after cleanup.
            self._on_update(value)
            self._credential = value
            self._condition.notify_all()

    def _renew(self) -> None:
        failure_deadline: float | None = None
        retry_at = 0.0
        retry_delay = 1.0
        while True:
            with self._condition:
                if self._closed:
                    return
                now = self._clock()
                credential = self._credential
                assert credential is not None
                if failure_deadline is not None and now >= credential.expires_at:
                    return
                if failure_deadline is not None and now >= failure_deadline:
                    # Keep a valid old token usable but stop retrying after the window.
                    if now < credential.expires_at:
                        self._condition.wait(credential.expires_at - now)
                        continue
                    return
                due = max(credential.expires_at - self._margin, retry_at)
                if failure_deadline is not None:
                    due = min(due, credential.expires_at)
                if now < due:
                    self._condition.wait(due - now)
                    continue
            try:
                self._update()
            except Exception as error:
                self._last_status = safe_http_status(getattr(error, "http_status", None))
                self._attempts += 1
                now = self._clock()
                if failure_deadline is None:
                    failure_deadline = now + self._window
                retry_at = min(now + retry_delay, failure_deadline)
                retry_delay = min(30.0, retry_delay * 2)
            else:
                failure_deadline = None
                self._attempts = 0
                retry_delay = 1.0
                # Avoid tight loops for credentials shorter than the refresh margin.
                retry_at = self._clock() + 1.0
