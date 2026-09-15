from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from collections.abc import Iterator

import pytest

from agent_run import notification_transport as transport
from agent_run.process_cleanup import child_subreaper


CONFIG = {"enabled": True, "profile": "notify", "open_id": "ou_recipient", "app_id": "cli_app"}
CARD = {"schema": "2.0", "body": {"elements": []}}


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    cli = tmp_path / "lark-cli"
    cli.write_text(
        f"#!{sys.executable}\n" + '''import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["FAKE_LARK_ROOT"])
(root / "called").touch()
if "whoami" in args:
    print(json.dumps({"profile": "notify", "appId": os.environ.get("FAKE_APP", "cli_app"), "identity": "bot", "available": True}))
else:
    (root / "request.json").write_text(json.dumps(args))
    mode = os.environ.get("FAKE_MODE", "success")
    if mode == "hang":
        (root / "cli.pid").write_text(str(os.getpid()))
        (root / "supervisor.pid").write_text(str(os.getppid()))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        (root / "child.pid").write_text(str(child.pid))
        time.sleep(60)
    elif mode == "overflow":
        print("x" * 100000)
    elif mode == "reject":
        print(json.dumps({"code": 230001}))
        sys.exit(1)
    elif mode == "error":
        print("secret-do-not-persist", file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps({"data": {"message_id": "om_sent"}}))
''', encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("FAKE_LARK_ROOT", str(tmp_path))
    with child_subreaper():
        try:
            yield tmp_path
        finally:
            _cleanup_recorded_processes(tmp_path)


def test_explicit_recipient_profile_bot_and_card(fake_cli: Path) -> None:
    assert transport.send(CONFIG, CARD, "stable-key", threading.Event())["outcome"] == "success"
    argv = json.loads((fake_cli / "request.json").read_text())
    assert argv[:4] == ["--profile", "notify", "im", "+messages-send"]
    for flag, value in [("--as", "bot"), ("--user-id", "ou_recipient"), ("--msg-type", "interactive"), ("--idempotency-key", "stable-key")]:
        assert argv[argv.index(flag) + 1] == value
    assert json.loads(argv[argv.index("--content") + 1]) == CARD


@pytest.mark.parametrize("config", [{"enabled": False}, {"enabled": True}, {**CONFIG, "app_id": "changed"}])
def test_disabled_incomplete_or_changed_binding_never_sends(fake_cli: Path, config: dict[str, Any]) -> None:
    assert transport.send(config, CARD, "key", threading.Event())["outcome"] == "failed"
    assert not (fake_cli / "request.json").exists()
    if not config.get("enabled") or not config.get("profile"):
        assert not (fake_cli / "called").exists()


@pytest.mark.parametrize("mode,outcome", [("error", "unknown"), ("reject", "failed"), ("overflow", "unknown")])
def test_failure_and_unknown_delivery_are_safe_and_bounded(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str,
) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    result = transport.send(CONFIG, CARD, "key", threading.Event())
    assert result["outcome"] == outcome
    assert "secret" not in (result["reason"] or "")
    assert len(result["reason"] or "") < 200


@pytest.mark.parametrize("cancel", [True, False])
def test_real_process_group_cancel_and_timeout(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    offset = [0.0]
    if not cancel:
        monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))
    event = threading.Event()
    results: list[transport.SendResult] = []
    with child_subreaper():
        worker = threading.Thread(target=lambda: results.append(transport.send(CONFIG, CARD, "key", event)))
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not (fake_cli / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (fake_cli / "child.pid").exists()
            child = int((fake_cli / "child.pid").read_text())
            start = time.monotonic()
            if cancel:
                event.set()
            else:
                offset[0] = 20.0
            worker.join(2)
            assert not worker.is_alive()
            assert time.monotonic() - start < 2
            assert results[0]["outcome"] == "unknown"
            # waitpid proves the descendant exited, including a transient zombie.
            try:
                assert os.waitpid(child, os.WNOHANG)[0] == child
            except ChildProcessError:
                assert not Path(f"/proc/{child}").exists()
        finally:
            event.set()
            worker.join(2)


@pytest.mark.parametrize("death_signal", [signal.SIGTERM, signal.SIGKILL])
def test_executor_death_reaps_cli_and_detached_descendant(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, death_signal: int,
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    environment = {**os.environ, "PYTHONPATH": str(Path(transport.__file__).resolve().parent.parent)}
    with child_subreaper():
        owner = subprocess.Popen([
            sys.executable, "-c",
            "from agent_run.notification_transport import send; from threading import Event; "
            f"send({CONFIG!r}, {CARD!r}, 'key', Event())",
        ], env=environment)
        pids: list[int] = []
        try:
            deadline = time.monotonic() + 3
            while not (fake_cli / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (fake_cli / "child.pid").exists()
            pids = [int((fake_cli / name).read_text()) for name in ("supervisor.pid", "cli.pid", "child.pid")]
            owner.send_signal(death_signal)
            owner.wait(timeout=2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                for pid in pids:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                if all(not Path(f"/proc/{pid}").exists() for pid in pids):
                    break
                time.sleep(0.01)
            assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=2)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass


def test_supervisor_deadline_works_without_owner_cancellation(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    environment = {**os.environ, "PYTHONPATH": str(Path(transport.__file__).resolve().parent.parent)}
    supervisor = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from types import SimpleNamespace; "
         "from agent_run import notification_process as supervisor; "
         f"expiry = Path({str(fake_cli / 'expire')!r}); "
         "supervisor.time = SimpleNamespace(monotonic=lambda: 20.0 if expiry.exists() else 0.0); "
         "sys.argv = ['supervisor', '10', 'lark-cli', 'send']; "
         "raise SystemExit(supervisor.main())"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=environment,
    )
    try:
        # Move the supervisor clock only after the actual CLI subtree is ready.
        deadline = time.monotonic() + 3
        while not (fake_cli / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (fake_cli / "child.pid").exists()
        (fake_cli / "expire").touch()
        # Keep the owner pipe open: only the supervisor's deadline can stop it.
        assert supervisor.wait(timeout=2) == 124
        for name in ("cli.pid", "child.pid"):
            pid = int((fake_cli / name).read_text())
            assert not Path(f"/proc/{pid}").exists()
    finally:
        assert supervisor.stdin is not None
        supervisor.stdin.close()
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)


def _cleanup_recorded_processes(root: Path) -> None:
    """Independent fallback, after the assertions on production cleanup."""
    pids = []
    for name in ("cli.pid", "child.pid", "supervisor.pid"):
        path = root / name
        if path.exists():
            pids.append(int(path.read_text()))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while pids and time.monotonic() < deadline:
        for pid in list(pids):
            try:
                done = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                done = not Path(f"/proc/{pid}").exists()
            if done:
                pids.remove(pid)
        if pids:
            time.sleep(0.01)
