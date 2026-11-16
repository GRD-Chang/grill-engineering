"""Invocation-local native gh files, without an account or credential service."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import threading
import time

import pytest

from agent_run.adapters.auth.credential_lifecycle import InitialCredentialUnavailable, ReadCredential
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
    assert list(directory.iterdir()) == []


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
    assert list(directory.iterdir()) == []


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


def test_native_delivery_close_only_removes_files_created_by_its_scope(tmp_path: Path) -> None:
    directory = tmp_path / "gh"
    directory.mkdir()
    foreign = {name: "unrelated configuration" for name in ("config.yml", "hosts.yml", "other.yml")}
    for name, value in foreign.items():
        (directory / name).write_text(value)
    minted: list[bool] = []

    def provider() -> ReadCredential:
        minted.append(True)
        return ReadCredential("synthetic-private", time.time() + 3600)

    unopened = NativeCredentialDelivery(provider, directory)
    unopened.close()
    assert minted == []
    assert {path.name: path.read_text() for path in directory.iterdir()} == foreign

    delivery = NativeCredentialDelivery(provider, directory)
    delivery.start()
    delivery.close()
    assert {path.name for path in directory.iterdir()} == {"other.yml"}
    for name in ("config.yml", "hosts.yml"):
        (directory / name).write_text(f"later owner's {name}")
    delivery.close()
    assert (directory / "other.yml").read_text() == foreign["other.yml"]
    for name in ("config.yml", "hosts.yml"):
        assert (directory / name).read_text() == f"later owner's {name}"
    assert minted == [True]


def test_native_delivery_removes_private_files_when_provider_close_fails(tmp_path: Path) -> None:
    class Provider:
        def __call__(self) -> ReadCredential:
            return ReadCredential("synthetic-private", time.time() + 3600)

        def cancel(self) -> None:
            raise OSError("synthetic provider cleanup failure")

    directory = tmp_path / "gh"
    delivery = NativeCredentialDelivery(Provider(), directory)
    delivery.start()
    with pytest.raises(OSError, match="provider cleanup failure"):
        delivery.close()
    assert list(directory.iterdir()) == []
    directory.rmdir()


@pytest.mark.parametrize("during_renewal", [False, True])
def test_native_delivery_close_prevents_late_provider_files_and_restart(
    tmp_path: Path, during_renewal: bool,
) -> None:
    entered, release = threading.Event(), threading.Event()
    errors: list[Exception] = []

    class Provider:
        calls = 0

        def __call__(self) -> ReadCredential:
            self.calls += 1
            if during_renewal and self.calls == 1:
                return ReadCredential("synthetic-initial", time.time() + 60)
            entered.set()
            assert release.wait(5), "Provider was not released"
            return ReadCredential("synthetic-late", time.time() + 3600)

        def cancel(self) -> None:
            release.set()

    directory = tmp_path / "gh"
    provider = Provider()
    delivery = NativeCredentialDelivery(provider, directory)

    def start() -> None:
        try:
            delivery.start()
        except Exception as error:
            errors.append(error)

    starter = threading.Thread(target=start)
    try:
        starter.start()
        assert entered.wait(5), "Provider did not reach the cancellation boundary"
        assert (directory / "config.yml").is_file()
        delivery.close()
        starter.join(5)
        assert not starter.is_alive()
        if during_renewal:
            assert errors == []
        else:
            assert len(errors) == 1
            assert isinstance(errors[0], InitialCredentialUnavailable)
        assert list(directory.iterdir()) == []
        directory.rmdir()
        delivery.close()
        calls = provider.calls
        with pytest.raises(InitialCredentialUnavailable, match="closed"):
            delivery.start()
        assert provider.calls == calls
        assert not directory.exists()
    finally:
        release.set()
        delivery.close()
        starter.join(5)
        assert not starter.is_alive()
