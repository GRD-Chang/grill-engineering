"""Invocation-local native gh configuration, updated without restarting a Worker."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from agent_run.adapters.auth.credential_lifecycle import CredentialProvider, ReadCredential, RenewableCredential


class NativeCredentialDelivery:
    """Own a private gh configuration and its renewable installation credential."""

    def __init__(
        self, provider: CredentialProvider, directory: Path, *,
        on_exhausted: Callable[[str], None] | None = None,
    ) -> None:
        self.directory = directory
        self._on_exhausted = on_exhausted
        self.lifecycle = RenewableCredential(
            provider, on_update=self._write, on_exhausted=self._exhausted,
        )

    def start(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        # Declare the native schema before gh can try an online user migration.
        self._replace_config("config.yml", {"version": "1"})
        self.lifecycle.start()

    def _write(self, credential: ReadCredential) -> None:
        self._replace_config("hosts.yml", {"github.com": {
            "oauth_token": credential.token, "git_protocol": "https",
        }})

    def _replace_config(self, name: str, value: dict[str, object]) -> None:
        # JSON is a YAML subset. Every native gh child reads files afresh;
        # replacing directory entries avoids exposing partial configuration.
        descriptor, staging = tempfile.mkstemp(prefix=f".{name}-", dir=self.directory)
        temporary = Path(staging)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / name)
        finally:
            temporary.unlink(missing_ok=True)

    def _exhausted(self, message: str) -> None:
        try:
            (self.directory / "hosts.yml").unlink(missing_ok=True)
        finally:
            if self._on_exhausted is not None:
                self._on_exhausted(message)

    def close(self) -> None:
        self.lifecycle.close()
        (self.directory / "hosts.yml").unlink(missing_ok=True)

    def __enter__(self) -> NativeCredentialDelivery:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
