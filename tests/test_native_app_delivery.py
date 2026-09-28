"""Native tool substitutes consume a renewable config in one Agent process."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.credential_lifecycle import ReadCredential, RenewableCredential
from agent_run.worker_credentials import NativeCredentialDelivery
from agent_run.worker_sandbox import WorkerSandboxError
from agent_run.process_cleanup import terminate_process_group


def test_same_agent_children_consume_rotated_native_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    updated = threading.Event()
    minted = iter([ReadCredential("token-A", 160), ReadCredential("token-B", 300)])
    original = RenewableCredential

    def lifecycle(provider: Any, **options: Any) -> RenewableCredential:
        publish = options.pop("on_update")
        def update(credential: ReadCredential) -> None:
            publish(credential)
            if credential.token == "token-B":
                updated.set()
        return original(provider, clock=lambda: now[0], renewal_margin=10,
                        on_update=update, **options)

    monkeypatch.setattr("agent_run.worker_credentials.RenewableCredential", lifecycle)
    tool = tmp_path / "gh"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:] == ['api', 'repos/other/project/issues', '--method', 'POST']\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "print(json.loads((pathlib.Path(os.environ['GH_CONFIG_DIR']) / 'hosts.yml').read_text())"
        "['github.com']['oauth_token'])\n"
    )
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    delivery: list[NativeCredentialDelivery] = []
    def make_delivery(*args: Any, **kwargs: Any) -> NativeCredentialDelivery:
        result = NativeCredentialDelivery(*args, **kwargs)
        delivery.append(result)
        return result
    monkeypatch.setattr("agent_run.codex.NativeCredentialDelivery", make_delivery)
    agent = (
        "import subprocess, sys\n"
        "args = ['gh', 'api', 'repos/other/project/issues', '--method', 'POST']\n"
        "print(subprocess.check_output(args, text=True).strip(), flush=True)\n"
        "sys.stdin.readline()\n"
        "print(subprocess.check_output(args, text=True).strip(), flush=True)\n"
    )
    def worker(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        environment = options["environment"]
        assert "host-secret" not in environment.values()
        config = Path(environment["GH_CONFIG_DIR"]) / "hosts.yml"
        assert config.stat().st_mode & 0o777 == 0o600
        with subprocess.Popen([sys.executable, "-c", agent], env=environment,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              start_new_session=True) as process:
            try:
                assert process.stdout is not None and process.stdin is not None
                assert select.select([process.stdout], [], [], 3)[0], "Agent did not reach its first tool call"
                assert process.stdout.readline().strip() == "token-A"
                now[0] = 155
                delivery[0].lifecycle.wake()
                assert updated.wait(3)
                process.stdin.write("continue\n")
                process.stdin.flush()
                stdout, _ = process.communicate(timeout=3)
                assert stdout.strip() == "token-B"
                assert process.returncode == 0
            finally:
                terminate_process_group(process, timeout=3)
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text('{"result_kind":"development","summary":"ok","human_blockers":null}')
        return subprocess.CompletedProcess(arguments, 0,
            '{"type":"thread.started","thread_id":"same-agent"}\n', "")
    monkeypatch.setattr("agent_run.codex.run_worker_process", worker)
    result = CodexCliBackend(credential_provider=lambda: next(minted)).develop(
        {"checkout": str(tmp_path), "ticket": {"number": 3}})
    assert result.thread_id == "same-agent"
    assert not delivery[0].directory.exists()


def test_delivery_instances_do_not_share_credentials(tmp_path: Path) -> None:
    with NativeCredentialDelivery(lambda: "scope-one", tmp_path / "one") as one, \
         NativeCredentialDelivery(lambda: "scope-two", tmp_path / "two") as two:
        one.start()
        two.start()
        assert json.loads((one.directory / "hosts.yml").read_text())["github.com"]["oauth_token"] == "scope-one"
        assert json.loads((two.directory / "hosts.yml").read_text())["github.com"]["oauth_token"] == "scope-two"
    assert not (one.directory / "hosts.yml").exists()
    assert not (two.directory / "hosts.yml").exists()


def test_expired_credentials_abort_worker_with_execution_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def worker(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        options["abort_event"].set()
        raise WorkerSandboxError("worker_credential_renewal_failed: repair credentials and resume")
    monkeypatch.setattr("agent_run.codex.run_worker_process", worker)
    with pytest.raises(CodexProcessError) as raised:
        CodexCliBackend(credential_provider=lambda: "initial").develop(
            {"checkout": str(tmp_path), "ticket": {"number": 3}})
    assert raised.value.machine_error == "credential_unavailable"
    assert "resume" in str(raised.value)


def test_app_native_worker_receives_scope_without_private_key_or_gh_mount(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / "private.pem"
    key.write_text("private-material")
    key.chmod(0o600)
    identity = {"source": "app", "app_profile": {
        "app_id": "123", "installation_id": "456", "private_key_path": str(key)},
        "app_read_only": True, "app_current_repository_only": True}
    seen: list[tuple[bool, str | None]] = []
    def provider(profile: Any, *, read_only: bool, repository: str | None) -> Any:
        assert profile.private_key_path == key
        seen.append((read_only, repository))
        return lambda: "scoped-app"
    monkeypatch.setattr("agent_run.codex._GitHubAppCredentialProvider", provider)
    tool = tmp_path / "gh"
    tool.write_text(f"#!{sys.executable}\n" +
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:] == ['auth', 'status']\n"
        "assert json.loads((pathlib.Path(os.environ['GH_CONFIG_DIR'])/'hosts.yml').read_text())"
        "['github.com']['oauth_token'] == 'scoped-app'\n")
    tool.chmod(0o700)
    worker = tmp_path / "agent"
    worker.write_text(f"#!{sys.executable}\n" +
        "import json, pathlib, subprocess, sys\n"
        "try:\n"
        f"    private = pathlib.Path({str(key)!r}).read_text()\n"
        "except OSError:\n    private = ''\n"
        "assert 'private-material' not in private\n"
        f"assert pathlib.Path({str(tool)!r}).read_text().startswith('#!')\n"
        "subprocess.run(['gh', 'auth', 'status'], check=True)\n"
        "assert subprocess.run(['git', 'config', '--local', 'test.forbidden', '1'], capture_output=True).returncode != 0\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps("
        "{'result_kind':'development','summary':'ok','human_blockers':None}))\n"
        "print(json.dumps({'type':'thread.started','thread_id':'app-native'}))\n")
    worker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = CodexCliBackend(executable=str(worker), worker_identity=identity).develop(
        {"checkout": str(git_repo), "repository": "example/project", "ticket": {"number": 3}})
    assert result.thread_id == "app-native"
    assert seen == [(True, "example/project")]
    assert key.read_text() == "private-material"


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
    monkeypatch.setattr("agent_run.worker_credentials.RenewableCredential", lifecycle)
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
