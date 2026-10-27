from __future__ import annotations

import agent_run.runtime.process_cleanup

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_run.install.installer import _runtime_identity
from support.environment import _user_environment as _isolated_environment
from support.environment import _file_snapshot


@pytest.mark.parametrize("mode", ["discard", "discard_input", "stdout", "merged"])
@pytest.mark.parametrize("overflow", [False, True])
def test_doctor_probe_stream_policy_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch, mode: str, overflow: bool,
) -> None:
    import agent_run.install.doctor as doctor

    processes = []
    order = []
    original_popen, original_cleanup = subprocess.Popen, agent_run.runtime.process_cleanup.terminate_process_group

    def start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def cleanup(process, **kwargs):
        order.append("cleanup")
        if process.stdout is not None:
            assert not process.stdout.closed
        original_cleanup(process, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "Popen", start)
    monkeypatch.setattr(doctor, "_terminate_process_group", cleanup)
    monkeypatch.setattr(doctor, "_COMMAND_TIMEOUT_SECONDS", 1)
    capture = mode in {"stdout", "merged"}
    program = "import os, sys, signal\n"
    if mode != "discard":
        program += "assert sys.stdin.buffer.read() == b'ping'\n"
    if overflow:
        program += f"os.write({2 if mode == 'merged' else 1}, b'x' * 9)\n"
        if capture:
            program += "signal.pause()\n"
    else:
        program += "os.write(1, b'ping')\nos.write(2, b'note')\n"
    arguments = [sys.executable, "-c", program]
    if capture:
        result = doctor._run_bounded_output_probe(
            arguments, input_data=b'ping', max_output_bytes=8,
            timeout_seconds=1, merge_stderr=mode == "merged",
        )
        assert result == ((None, False, b'') if overflow else
                          (0, False, b'pingnote' if mode == "merged" else b'ping'))
    else:
        assert doctor._run_bounded_probe(
            arguments, input_data=b'ping' if mode == "discard_input" else None,
        ) == (0, False)
    order.append("returned")
    assert order == ["cleanup", "returned"]
    assert processes[0].poll() is not None
    assert all(stream is None or stream.closed for stream in
               (processes[0].stdin, processes[0].stdout, processes[0].stderr))


@pytest.mark.parametrize("capture", [False, True])
def test_doctor_probe_early_stdin_close_keeps_result_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: bool,
) -> None:
    import agent_run.install.doctor as doctor

    ready = tmp_path / "closed"
    processes = []
    original_popen = subprocess.Popen

    def start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            process.kill()
            process.wait()
            raise AssertionError("probe did not close its pipes")
        return process

    monkeypatch.setattr(doctor.subprocess, "Popen", start)
    monkeypatch.setattr(doctor, "_COMMAND_TIMEOUT_SECONDS", 0.05)
    program = (
        "import os, signal\nfrom pathlib import Path\n"
        "os.close(0)\nos.close(1)\nos.close(2)\n"
        f"Path({str(ready)!r}).write_text('closed')\nsignal.pause()\n"
    )
    arguments = [sys.executable, "-c", program]
    if capture:
        assert doctor._run_bounded_output_probe(
            arguments, input_data=b'ping', timeout_seconds=0.05,
        ) == (None, False, b'')
    else:
        assert doctor._run_bounded_probe(arguments, input_data=b'ping') == (None, True)
    assert processes[0].poll() is not None
    assert all(stream is None or stream.closed for stream in
               (processes[0].stdin, processes[0].stdout, processes[0].stderr))


@pytest.mark.parametrize(
    ("installed", "codex_available", "systemd_available"),
    [(True, False, True), (True, True, False), (False, True, True), (True, True, True)],
)
def test_doctor_cli_separates_installation_and_execution_readiness(
    tmp_path: Path,
    installed: bool,
    codex_available: bool,
    systemd_available: bool,
) -> None:
    environment = _isolated_environment(tmp_path / "environment")
    home = Path(environment["HOME"])
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    environment["PATH"] = str(user_bin)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in (
        "git", "openssl", "bwrap", "gh", "systemd-run", "systemctl", "rm", "codex",
    ):
        if name == "codex" and not codex_available:
            continue
        executable = user_bin / name
        code = 1 if name == "systemctl" and not systemd_available else 0
        output = ""
        if name == "git":
            output = "git version 2.40.0"
        elif name == "systemctl":
            output = "257"
        elif name == "gh":
            output = "--paginate --slurp --method --header"
        elif name == "codex":
            output = "--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color"
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {code}\n")
        executable.chmod(0o700)
    if installed:
        root = Path(environment["XDG_DATA_HOME"]) / "agent-run"
        snapshot = root / "snapshots" / "fixture"
        package = snapshot / "lib" / "python3" / "site-packages" / "agent_run"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (snapshot / "manifest.json").write_text(json.dumps({
            "content_identity": _runtime_identity(package),
        }))
        entry = snapshot / "bin" / "agent-run"
        entry.parent.mkdir()
        entry.write_text(
            f'#!{sys.executable}\nfrom agent_run.cli.main import main\nraise SystemExit(main())\n'
        )
        entry.chmod(0o700)
        generation = root / "generations" / "fixture"
        generation.mkdir(parents=True)
        (generation / "current").symlink_to(snapshot)
        (root / "active").symlink_to(generation)
        (user_bin / "agent-run").symlink_to(
            root / "active" / "current" / "bin" / "agent-run"
        )
        command = [str(user_bin / "agent-run")]
    else:
        command = [sys.executable, "-m", "agent_run"]

    before = _file_snapshot(tmp_path)
    result = subprocess.run(
        [*command, "doctor", "--json"], env=environment, cwd=tmp_path,
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["installation_readiness"]["status"] == (
        "ready" if installed else "issues"
    )
    assert "codex" not in report["installation_readiness"]["checks"]
    assert report["execution_readiness"]["status"] == (
        "ok" if codex_available and systemd_available else "unavailable"
    )
    assert "codex" in report["execution_readiness"]["checks"]
    assert report["status"] == (
        "ready" if installed and codex_available and systemd_available else "issues"
    )
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("dependency", "script", "status"),
    [
        ("git", "printf 'git version 2.39.5\\n'", "unsupported"),
        ("git", "printf 'not a version\\n'", "unsupported"),
        ("codex", "exit 0", "unsupported"),
        ("gh", "echo '--paginate --method --header'", "unsupported"),
        ("gh", "[ \"$1\" = auth ] && exit 1\necho '--paginate --slurp --method --header'", "not_logged_in"),
        ("codex", "[ \"$1\" = login ] && exit 1\nprintf '%s\\n' '--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color'", "not_logged_in"),
        ("bwrap", "[ \"$1\" = --version ] && exit 0\nexit 1", "unavailable"),
    ],
)
def test_doctor_rejects_version_only_or_incompatible_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], dependency: str, script: str, status: str,
) -> None:
    from agent_run.cli.main import main

    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / dependency
    executable.write_text("#!/bin/sh\n" + script + "\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    key = {"bwrap": "bubblewrap", "gh": "github"}.get(dependency, dependency)
    assert report["checks"][key]["status"] == status
    assert report["execution_readiness"]["status"] == "unavailable"


@pytest.mark.parametrize("as_json", [False, True])
def test_doctor_preserves_current_settings_profile_and_task(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], as_json: bool,
) -> None:
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.contracts.task import TaskKey
    from agent_run.cli.main import main
    from support.repository_fixture import read_binding
    from support.cli_fixtures import _published_profile_task

    # The existing native fixture uses freeze_creation and Kernel.create_task,
    # never the retired Controller/AgentProfileStore seeder.
    kernel = _published_profile_task(git_repo)
    monkeypatch.setenv("DOCTOR_FIXTURE_PAT", "synthetic-doctor-token")
    assert main(["settings", "configure", "--dev-model", "future-only-model",
                 "--worker-github-source", "pat", "--github-pat-env", "DOCTOR_FIXTURE_PAT",
                 "--json"]) == 0
    capsys.readouterr()
    assert main(["configure", "profile-public", "--repo", "example/project",
                 "--dev-model", "current-run-model", "--json"]) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["profile_revision"] == 2
    assert configured["profiles"]["development"]["model"] == "current-run-model"
    workspace = ManagedWorkspace.for_repository("example/project")
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    binding = read_binding(task, kernel.storage.path)
    assert binding == ("profile-public", kernel.storage.path.resolve())

    # No real credentials, service, namespace or dependency probe can run.
    tools = tmp_path / "empty-tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", str(tools))
    before = _file_snapshot(tmp_path)
    assert main(["doctor", *(["--json"] if as_json else [])]) == 0
    output = capsys.readouterr().out
    assert "synthetic-doctor-token" not in output
    assert "current-run-model" not in output and "future-only-model" not in output
    if as_json:
        report = json.loads(output)
        assert report["result"] == "doctor" and report["status"] == "issues"
        assert report["checks"]["worker_read_provider"] == {"provider": "pat", "status": "ok"}
        assert report["checks"]["git"]["status"] == "missing"
        assert report["execution_readiness"]["status"] == "unavailable"
    else:
        assert "agent-run doctor" in output and "pat" in output
    assert _file_snapshot(tmp_path) == before
    assert read_binding(task, kernel.storage.path) == binding


@pytest.mark.parametrize("module", ["agent_run", 'agent_run'])
@pytest.mark.parametrize("command", [[], ["doctor"]])
def test_public_help_is_read_only_without_host_dependencies(
    tmp_path: Path, module: str, command: list[str],
) -> None:
    environment = _isolated_environment(tmp_path / "environment")
    environment.update(PYTHONPATH=str(Path(__file__).parents[1] / "src"),
                       PYTHONDONTWRITEBYTECODE="1", PATH=str(tmp_path / "no-tools"))
    before = _file_snapshot(tmp_path)
    result = subprocess.run([sys.executable, "-m", module, *command, "--help"],
                            env=environment, cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "agent-run" in result.stdout and "--help" in result.stdout
    assert _file_snapshot(tmp_path) == before
