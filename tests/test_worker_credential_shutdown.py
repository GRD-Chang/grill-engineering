from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

import agent_run.worker_credentials as worker_credentials
from agent_run.worker_credentials import (
    HostGitHubReadChannel,
    ReadCredential,
    WorkerCredentialChannel,
)


def test_close_during_retry_joins_renewal_without_another_backoff(
    tmp_path: Path,
) -> None:
    retry_started = threading.Event()
    release_retry = threading.Event()
    clock_offset = 0.0

    def clock() -> float:
        return time.time() + clock_offset

    class CancellableProvider:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> ReadCredential:
            nonlocal clock_offset
            self.calls += 1
            if self.calls == 1:
                return ReadCredential("reader", clock() + 1)
            if self.calls == 2:
                # Expire the credential, then enter the real renewal retry path.
                clock_offset += 2
                raise OSError("temporary issuer outage")
            retry_started.set()
            if not release_retry.wait(timeout=5):
                raise OSError("test did not release renewal")
            raise OSError("issuer cancelled")

        def cancel(self) -> None:
            release_retry.set()

    credentials = WorkerCredentialChannel(
        CancellableProvider(), clock=clock, renewal_margin=10
    )
    socket_path = tmp_path / "credential.sock"
    renewal_thread: threading.Thread | None = None
    try:
        credentials.start(socket_path)
        renewal_thread = credentials._renewal_thread  # noqa: SLF001 - lifecycle seam
        assert retry_started.wait(timeout=5)
        credentials.close()

        assert renewal_thread is not None
        assert not renewal_thread.is_alive()
        assert not socket_path.exists()
    finally:
        release_retry.set()
        credentials.close()
        if renewal_thread is not None:
            renewal_thread.join(timeout=5)


@pytest.mark.parametrize("provider", ["host", "app"])
def test_close_reclaims_connection_waiting_for_an_incomplete_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    receiving = threading.Event()
    original_receive = worker_credentials._receive_message

    def receive(connection: socket.socket) -> object:
        receiving.set()
        return original_receive(connection)

    monkeypatch.setattr(worker_credentials, "_receive_message", receive)
    channel = (
        HostGitHubReadChannel()
        if provider == "host"
        else WorkerCredentialChannel(lambda: "reader")
    )
    socket_path = tmp_path / "read.sock"
    try:
        channel.start(socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            client.sendall(b"\x00")
            assert receiving.wait(timeout=5)
            server = channel._server_thread
            channel.close()
            assert server is not None and not server.is_alive()
            assert client.recv(1) == b""
        assert not socket_path.exists()
    finally:
        channel.close()
