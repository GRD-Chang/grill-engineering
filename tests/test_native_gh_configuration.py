"""Invocation-local native gh files, without an account or credential service."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import threading
import time

import pytest

from agent_run.adapters.auth.credential_lifecycle import ReadCredential
from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery


@pytest.mark.parametrize("existing_version", [None, "not-a-native-version"])
def test_native_delivery_initializes_private_versioned_configuration(
    tmp_path: Path, existing_version: str | None,
) -> None:
    directory = tmp_path / "gh"
    directory.mkdir(mode=0o700)
    if existing_version is not None:
        (directory / "config.yml").write_text(json.dumps({"version": existing_version}))
    credential = ReadCredential("synthetic-installation-credential", time.time() + 3600)
    with NativeCredentialDelivery(lambda: credential, directory) as delivery:
        delivery.start()
        assert json.loads((directory / "config.yml").read_text()) == {"version": "1"}
        assert json.loads((directory / "hosts.yml").read_text()) == {
            "github.com": {"oauth_token": credential.token, "git_protocol": "https"},
        }
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for name in ("config.yml", "hosts.yml"):
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
        assert {path.name for path in directory.iterdir()} == {"config.yml", "hosts.yml"}
    assert not (directory / "hosts.yml").exists()
    assert json.loads((directory / "config.yml").read_text()) == {"version": "1"}


def test_native_delivery_renews_hosts_without_replacing_configuration(tmp_path: Path) -> None:
    renewal_entered, release_renewal = threading.Event(), threading.Event()
    calls = 0

    def provider() -> ReadCredential:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ReadCredential("synthetic-first", time.time() + 60)
        renewal_entered.set()
        assert release_renewal.wait(5), "Renewal was not released"
        return ReadCredential("synthetic-renewed", time.time() + 3600)

    directory = tmp_path / "gh"
    delivery = NativeCredentialDelivery(provider, directory)
    try:
        delivery.start()
        assert renewal_entered.wait(5)
        metadata = directory / "config.yml"
        original = metadata.stat()
        assert json.loads(metadata.read_text()) == {"version": "1"}
        release_renewal.set()
        deadline = time.monotonic() + 5
        while json.loads((directory / "hosts.yml").read_text())["github.com"]["oauth_token"] != "synthetic-renewed":
            assert time.monotonic() < deadline, "Renewed credential was not published"
            threading.Event().wait(0.01)
        assert calls == 2
        assert metadata.stat().st_ino == original.st_ino
        assert metadata.stat().st_mtime_ns == original.st_mtime_ns
        assert json.loads(metadata.read_text()) == {"version": "1"}
        assert stat.S_IMODE((directory / "hosts.yml").stat().st_mode) == 0o600
        assert {path.name for path in directory.iterdir()} == {"config.yml", "hosts.yml"}
    finally:
        release_renewal.set()
        delivery.close()
    assert not (directory / "hosts.yml").exists()


def test_native_delivery_does_not_mint_after_configuration_initialization_failure(tmp_path: Path) -> None:
    directory = tmp_path / "gh"
    (directory / "config.yml").mkdir(parents=True)
    minted: list[bool] = []

    def provider() -> ReadCredential:
        minted.append(True)
        return ReadCredential("synthetic-not-to-be-issued", time.time() + 3600)

    with NativeCredentialDelivery(provider, directory) as delivery:
        with pytest.raises(OSError):
            delivery.start()
    assert minted == []
    assert (directory / "config.yml").is_dir()
    assert {path.name for path in directory.iterdir()} == {"config.yml"}
