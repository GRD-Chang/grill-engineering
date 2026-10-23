"""Bounded physical Git transport, independent of the retired GitRepository engine."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from agent_run.git_errors import GitError
from agent_run.git_output import run_git


def test_git_reads_ignore_inherited_shallow_file_but_keep_repository_boundary(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.git_output import run_git

    def raw_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=git_repo, capture_output=True, text=True,
        )

    initial = raw_git("rev-parse", "HEAD").stdout.strip()
    assert raw_git("commit", "--allow-empty", "-m", "second").returncode == 0
    head = raw_git("rev-parse", "HEAD").stdout.strip()
    inherited = tmp_path / "host-shallow"
    inherited.write_text("host shallow file must not be read\n", encoding="utf-8")
    monkeypatch.setenv("GIT_SHALLOW_FILE", str(inherited))

    # The inherited path really redirects Git's graph before Runner isolation.
    assert raw_git("rev-list", "HEAD").returncode != 0
    result = run_git(["git", "rev-list", "HEAD"], cwd=git_repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [head, initial]

    # Removing an external override must not erase the repository's own limit.
    (git_repo / ".git" / "shallow").write_text(head + "\n", encoding="utf-8")
    result = run_git(["git", "rev-list", "HEAD"], cwd=git_repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [head]
    shallow = run_git(["git", "rev-parse", "--is-shallow-repository"], cwd=git_repo)
    assert shallow.returncode == 0
    assert shallow.stdout.strip() == "true"
    assert inherited.read_text(encoding="utf-8") == "host shallow file must not be read\n"


def test_git_dispatch_honors_path_changes_after_cached_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.git_output import run_git

    original_path = os.environ.get("PATH", "")
    for marker in ("first", "second"):
        directory = tmp_path / marker
        directory.mkdir()
        executable = directory / "git"
        executable.write_text(f"#!/bin/sh\nprintf '{marker}\\n'\n", encoding="utf-8")
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", f"{directory}:{original_path}")
        result = run_git(["git", "--version"], cwd=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == marker


@pytest.mark.parametrize("megabytes", [2, 4])
def test_failing_hook_keeps_only_bounded_error_tail(git_repo: Path, megabytes: int) -> None:
    head = run_git(["git", "rev-parse", "HEAD"], cwd=git_repo).stdout.strip()
    hook = git_repo / ".git/hooks/pre-commit"
    hook.write_text(
        "#!/usr/bin/env python3\nimport os\n"
        f"for _ in range({megabytes} * 256):\n"
        " os.write(1, b'o' * 4096)\n os.write(2, b'e' * 4096)\n"
        "os.write(2, b'\\nhook rejected candidate\\n')\nraise SystemExit(7)\n"
    )
    hook.chmod(0o700)
    (git_repo / "README.md").write_text("changed\n")
    assert run_git(["git", "add", "README.md"], cwd=git_repo).returncode == 0
    result = run_git(["git", "commit", "-m", "bounded hook"], cwd=git_repo)
    assert result.returncode != 0
    assert len(result.stderr.encode()) <= 64 * 1024
    assert "hook rejected candidate" in result.stderr
    assert "truncated" in result.stderr
    assert run_git(["git", "rev-parse", "HEAD"], cwd=git_repo).stdout.strip() == head


def test_both_git_pipes_are_drained_with_bounded_buffers(git_repo: Path, monkeypatch) -> None:
    from agent_run import git_output

    buffers = []

    class MeasuredBuffer(bytearray):
        def __init__(self):
            super().__init__()
            self.maximum = 0
            buffers.append(self)

        def extend(self, value):
            super().extend(value)
            self.maximum = max(self.maximum, len(self))

    from agent_run import _process_io

    monkeypatch.setattr(_process_io, "bytearray", MeasuredBuffer, raising=False)
    program = git_repo / "noisy.py"
    program.write_text(
        "import os\nfor _ in range(1024):\n"
        " os.write(1, b'o' * 4096)\n os.write(2, b'e' * 4096)\n"
        "os.write(2, b'\\nlast diagnostic\\n')\n"
    )
    with pytest.raises(GitError, match="stdout exceeds.*complete result unavailable") as error:
        run_git(["git", "-c", "alias.noisy=!python3 noisy.py", "noisy"], cwd=git_repo)
    assert "last diagnostic" in str(error.value)
    assert buffers[0].maximum <= git_output.MAX_GIT_OUTPUT_BYTES
    assert buffers[1].maximum <= git_output.MAX_GIT_ERROR_BYTES
    assert len(str(error.value).encode()) <= git_output.MAX_GIT_ERROR_BYTES


def test_git_reader_reaps_process_on_read_failure(git_repo: Path, monkeypatch) -> None:
    from agent_run import git_output

    processes = []
    real_popen = git_output.subprocess.Popen

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    real_read = git_output.os.read

    def failed_read(*args):
        if processes:
            raise OSError("injected pipe failure")
        return real_read(*args)

    monkeypatch.setattr(git_output.subprocess, "Popen", start)
    monkeypatch.setattr(git_output.os, "read", failed_read)
    with pytest.raises(OSError, match="pipe failure"):
        run_git(["git", "status"], cwd=git_repo)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_bounded_stdin_and_broken_pipe_do_not_deadlock(git_repo: Path) -> None:
    result = run_git(["git", "hash-object", "--stdin"], cwd=git_repo, input="data\n" * 100000)
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 40
    result = run_git(["git", "not-a-command"], cwd=git_repo, input="x" * 1000000)
    assert result.returncode != 0
    assert "not a git command" in result.stderr
    with pytest.raises(GitError, match="input exceeds"):
        run_git(["git", "apply", "-"], cwd=git_repo, input="x" * (1024 * 1024 + 1))


def test_non_utf8_git_data_is_rejected(git_repo: Path) -> None:
    program = git_repo / "invalid.py"
    program.write_text("import os\nos.write(1, b'patch\\xff')\n")
    with pytest.raises(GitError, match="not valid UTF-8"):
        run_git(["git", "-c", "alias.invalid=!python3 invalid.py", "invalid"], cwd=git_repo)


def test_git_stdin_and_both_outputs_make_progress_before_deadline(git_repo: Path) -> None:
    program = git_repo / "duplex.py"
    program.write_text(
        "import os, sys\n"
        "os.write(1, b'o' * 262144)\nos.write(2, b'e' * 262144)\n"
        "data = sys.stdin.buffer.read()\n"
        "print(len(data), flush=True)\n"
        "os.write(2, b'last diagnostic')\n"
    )
    result = run_git(["git", "-c", "alias.duplex=!python3 duplex.py", "duplex"],
                     cwd=git_repo, input="x" * 500000, timeout=5)
    assert result.returncode == 0
    assert result.stdout == "o" * 262144 + "500000\n"
    assert result.stderr.endswith("last diagnostic")
    assert len(result.stderr.encode()) <= 64 * 1024


def test_git_deadline_retains_diagnostics_and_reaps_child(git_repo: Path, monkeypatch) -> None:
    from agent_run import git_output

    processes = []
    real_popen = subprocess.Popen

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(git_output.subprocess, "Popen", start)
    program = git_repo / "blocked.py"
    program.write_text(
        "import os, signal\nos.write(1, b'partial output')\n"
        "os.write(2, b'last diagnostic')\nsignal.pause()\n"
    )
    with pytest.raises(GitError, match="timed out") as error:
        run_git(["git", "-c", "alias.blocked=!python3 blocked.py", "blocked"],
                cwd=git_repo, timeout=1)
    assert error.value.evidence["stdout"] == "partial output"
    assert error.value.evidence["stderr"] == "last diagnostic"
    assert error.value.evidence["timeout_seconds"] == 1
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_selector_creation_failure_reaps_git(git_repo: Path, monkeypatch) -> None:
    from agent_run import git_output

    processes = []
    real_popen = git_output.subprocess.Popen

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def failed_selector():
        raise OSError("injected selector failure")

    monkeypatch.setattr(git_output.subprocess, "Popen", start)
    from agent_run import _process_io

    monkeypatch.setattr(_process_io.selectors, "DefaultSelector", failed_selector)
    with pytest.raises(OSError, match="selector failure"):
        run_git(["git", "status"], cwd=git_repo)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
