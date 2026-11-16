"""Invocation-local native gh configuration, updated without restarting a Worker."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

from agent_run.adapters.auth.credential_lifecycle import (
    CredentialProvider, InitialCredentialUnavailable, ReadCredential, RenewableCredential,
)


class NativeCredentialDelivery:
    """Own gh files and credentials in a directory exclusive until close returns."""

    def __init__(
        self, provider: CredentialProvider, directory: Path, *,
        on_exhausted: Callable[[str], None] | None = None,
    ) -> None:
        self.directory = directory
        self._on_exhausted = on_exhausted
        self._lock = threading.RLock()
        self._closed = False
        self._owned_files: set[str] = set()
        self.lifecycle = RenewableCredential(
            provider, on_update=self._write, on_exhausted=self._exhausted,
        )

    def start(self, *, guard: Callable[[], None] | None = None, deadline: float | None = None) -> None:
        with self._lock:
            if self._closed:
                raise InitialCredentialUnavailable("Credential delivery is closed")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            # Declare the native schema before gh can try an online user migration.
            self._replace_config("config.yml", {"version": "1"})
        self.lifecycle.start(guard=guard, deadline=deadline)

    def _write(self, credential: ReadCredential) -> None:
        self._replace_config("hosts.yml", {"github.com": {
            "oauth_token": credential.token, "git_protocol": "https",
        }})

    def _replace_config(self, name: str, value: dict[str, object]) -> None:
        # JSON is a YAML subset. Every native gh child reads files afresh;
        # replacing directory entries avoids exposing partial configuration.
        with self._lock:
            if self._closed:
                raise InitialCredentialUnavailable("Credential delivery is closed")
            descriptor, staging = tempfile.mkstemp(prefix=f".{name}-", dir=self.directory)
            temporary = Path(staging)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.directory / name)
                self._owned_files.add(name)
            finally:
                temporary.unlink(missing_ok=True)

    def _remove_owned(self, name: str) -> None:
        with self._lock:
            if name in self._owned_files:
                (self.directory / name).unlink(missing_ok=True)
                self._owned_files.remove(name)

    def _exhausted(self, message: str) -> None:
        try:
            self._remove_owned("hosts.yml")
        finally:
            if self._on_exhausted is not None:
                self._on_exhausted(message)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # Never hold the file lock while joining a provider's delivery thread.
        try:
            self.lifecycle.close()
        finally:
            try:
                self._remove_owned("hosts.yml")
            finally:
                self._remove_owned("config.yml")

    def __enter__(self) -> NativeCredentialDelivery:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
