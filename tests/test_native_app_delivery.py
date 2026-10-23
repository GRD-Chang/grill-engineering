"""Native tool substitutes consume a renewable config in one Agent process."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_run.adapters.auth.credential_lifecycle import ReadCredential, RenewableCredential
from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery


def test_delivery_instances_do_not_share_credentials(tmp_path: Path) -> None:
    with NativeCredentialDelivery(lambda: "scope-one", tmp_path / "one") as one, \
         NativeCredentialDelivery(lambda: "scope-two", tmp_path / "two") as two:
        one.start()
        two.start()
        assert json.loads((one.directory / "hosts.yml").read_text())["github.com"]["oauth_token"] == "scope-one"
        assert json.loads((two.directory / "hosts.yml").read_text())["github.com"]["oauth_token"] == "scope-two"
    assert not (one.directory / "hosts.yml").exists()
    assert not (two.directory / "hosts.yml").exists()


def test_expiry_with_blocked_renewal_removes_token_and_cancels_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    renewing = threading.Event()
    cancelled = threading.Event()
    exhausted = threading.Event()
    class Provider:
        calls = 0
        def __call__(self) -> ReadCredential:
            self.calls += 1
            if self.calls == 1:
                return ReadCredential("token-A", 110)
            renewing.set()
            assert cancelled.wait(3), "Expiry did not cancel the blocked issuer"
            # A late successful issuer result cannot restore a closed identity.
            return ReadCredential("late-token-B", 200)
        def cancel(self) -> None:
            cancelled.set()
    def lifecycle(provider: Any, **options: Any) -> RenewableCredential:
        return RenewableCredential(provider, clock=lambda: now[0], renewal_margin=5, **options)
    monkeypatch.setattr('agent_run.adapters.auth.credential_lifecycle.RenewableCredential', lifecycle)
    messages: list[str] = []
    def failed(message: str) -> None:
        messages.append(message)
        exhausted.set()
    with NativeCredentialDelivery(Provider(), tmp_path / "gh", on_exhausted=failed) as delivery:
        delivery.start()
        now[0] = 105
        delivery.lifecycle.wake()
        assert renewing.wait(3)
        assert (delivery.directory / "hosts.yml").exists()
        now[0] = 111
        delivery.lifecycle.wake()
        assert exhausted.wait(3)
        assert cancelled.wait(3)
        assert not (delivery.directory / "hosts.yml").exists()
    assert "worker_credential_renewal_failed" in messages[0]
    assert "resume" in messages[0]
    assert not (delivery.directory / "hosts.yml").exists()
    for thread in (delivery.lifecycle._renewal_thread, delivery.lifecycle._expiry_thread):
        assert thread is not None and not thread.is_alive()
