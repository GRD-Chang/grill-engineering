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

    monkeypatch.setattr(git_output, "bytearray", MeasuredBuffer, raising=False)
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
    monkeypatch.setattr(git_output.selectors, "DefaultSelector", failed_selector)
    with pytest.raises(OSError, match="selector failure"):
        run_git(["git", "status"], cwd=git_repo)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
