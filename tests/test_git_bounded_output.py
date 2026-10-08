from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_run.git import GitError, GitRepository


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
    git = GitRepository(git_repo)
    head = git.checkout_head(git_repo)
    hook = git_repo / ".git/hooks/pre-commit"
    hook.write_text(
        "#!/usr/bin/env python3\nimport os\n"
        f"for _ in range({megabytes} * 256):\n"
        " os.write(1, b'o' * 4096)\n os.write(2, b'e' * 4096)\n"
        "os.write(2, b'\\nhook rejected candidate\\n')\nraise SystemExit(7)\n"
    )
    hook.chmod(0o700)
    (git_repo / "README.md").write_text("changed\n")
    with pytest.raises(GitError) as error:
        git.commit_candidate(git_repo, ticket_number=189, attempt=1, expected_head=head)
    assert len(str(error.value).encode()) <= 64 * 1024
    assert "hook rejected candidate" in str(error.value)
    assert "truncated" in str(error.value)
    assert git.checkout_head(git_repo) == head


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
        GitRepository(git_repo)._run("-c", "alias.noisy=!python3 noisy.py", "noisy")
    assert "last diagnostic" in str(error.value)
    assert buffers[0].maximum <= git_output.MAX_GIT_OUTPUT_BYTES
    assert buffers[1].maximum <= git_output.MAX_GIT_ERROR_BYTES
    assert len(str(error.value).encode()) <= git_output.MAX_GIT_ERROR_BYTES


def test_oversized_patch_is_rejected_before_apply(git_repo: Path, monkeypatch) -> None:
    from agent_run import git as git_module

    git = GitRepository(git_repo)
    (git_repo / "large.txt").write_text("old\n")
    git._run("add", "large.txt")
    git._run("commit", "-m", "base")
    before = git.checkout_head(git_repo)
    (git_repo / "large.txt").write_text("change\n" * 200000)
    git._run("commit", "-am", "finding snapshot")
    snapshot = git.checkout_head(git_repo)
    git._run("checkout", "--detach", before)
    (git_repo / "README.md").write_text("default update\n")
    git._run("commit", "-am", "default update")
    default = git.checkout_head(git_repo)
    dispatched: list[str] = []
    real_run = git_module.run_git

    def dispatch(arguments, **kwargs):
        dispatched.append(arguments[1])
        return real_run(arguments, **kwargs)

    monkeypatch.setattr(git_module, "run_git", dispatch)
    with pytest.raises(GitError, match="stdout exceeds"):
        git._replay_finding_delta(
            git_repo, run_head_sha=before, default_head_sha=default,
            candidate_sha=before, snapshot_sha=snapshot,
        )
    assert "apply" not in dispatched
    assert git.checkout_head(git_repo) == default
    assert (git_repo / "large.txt").read_text() == "old\n"


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
        GitRepository(git_repo)._run("status")
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_bounded_stdin_and_broken_pipe_do_not_deadlock(git_repo: Path) -> None:
    git = GitRepository(git_repo)
    result = git._run_in(git_repo, "hash-object", "--stdin", input="data\n" * 100000)
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 40
    result = git._run_in(git_repo, "not-a-command", input="x" * 1000000)
    assert result.returncode != 0
    assert "not a git command" in result.stderr
    with pytest.raises(GitError, match="input exceeds"):
        git._run_in(git_repo, "apply", "-", input="x" * (1024 * 1024 + 1))


def test_non_utf8_git_data_is_rejected(git_repo: Path) -> None:
    program = git_repo / "invalid.py"
    program.write_text("import os\nos.write(1, b'patch\\xff')\n")
    with pytest.raises(GitError, match="not valid UTF-8"):
        GitRepository(git_repo)._run("-c", "alias.invalid=!python3 invalid.py", "invalid")


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
        GitRepository(git_repo)._run("status")
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_git_timeout_retains_output_and_reaps_hook_group(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    from agent_run import git_output, git_integration_operation

    hook = git_repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'before timeout\n' >&2\nexec sleep 10\n")
    hook.chmod(0o700)
    processes = []
    real_popen = git_output.subprocess.Popen

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(git_output.subprocess, "Popen", start)
    monkeypatch.setattr(git_integration_operation, "INTEGRATION_COMMAND_TIMEOUT_SECONDS", 0.25)
    git = GitRepository(git_repo)
    head = git.checkout_head(git_repo)
    (git_repo / "README.md").write_text("bounded integration\n")
    with pytest.raises(GitError, match="timed out") as error:
        git.commit_integration_work(
            git_repo, expected_head_sha=head, expected_merge_sha=None, message="bounded",
        )
    assert error.value.evidence["command"] == ["git", "commit", "-m", "bounded"]
    assert error.value.evidence["head_sha"] == head
    assert error.value.evidence["timeout_seconds"] == 0.25
    assert "before timeout" in error.value.evidence["stderr"]
    assert all(process.poll() is not None for process in processes)
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True,
    ).stdout == "M  README.md\n"


def test_mechanical_merge_recovery_bounds_real_merge_driver_and_preserves_scene(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import json
    import shlex
    import signal
    import time
    from agent_run import git as git_module, git_output

    git = GitRepository(git_repo)
    (git_repo / ".gitattributes").write_text("README.md merge=bounded\n")
    git._run("add", ".gitattributes")
    git._run("commit", "-m", "merge attribute")
    base = git.checkout_head(git_repo)
    (git_repo / "README.md").write_text("candidate\n")
    git._run("commit", "-am", "candidate")
    candidate = git.checkout_head(git_repo)
    git._run("checkout", "--detach", base)
    (git_repo / "README.md").write_text("source\n")
    git._run("commit", "-am", "source")
    source = git.checkout_head(git_repo)
    git._run("checkout", "--detach", candidate)
    pause = tmp_path / "pause-driver"
    pid_path = tmp_path / "driver-pid"
    driver = tmp_path / "merge-driver.py"
    driver.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"if Path({str(pause)!r}).exists():\n"
        f" Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        " print('driver running', flush=True)\n time.sleep(10)\n"
    )
    git._run("config", "merge.bounded.driver", f"python3 {shlex.quote(str(driver))} %O %A %B")
    merged = git.integrate_remote_heads(git_repo, candidate_sha=candidate,
                                       source_sha=source, target_sha=base)
    assert merged.status == "clean"
    original_head = merged.head_sha
    processes = []
    real_popen = git_output.subprocess.Popen

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    # Own only the recorded orphan driver, so the timeout test itself leaves
    # neither an executing descendant nor a zombie under a container PID 1.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER
    try:
        monkeypatch.setattr(git_output.subprocess, "Popen", start)
        monkeypatch.setattr(git_module, "GIT_COMMAND_TIMEOUT_SECONDS", 0.25)
        pause.touch()
        with pytest.raises(GitError, match="timed out") as caught:
            git.integrate_remote_heads(git_repo, candidate_sha=candidate,
                                       source_sha=source, target_sha=base)
        evidence = caught.value.evidence
        assert evidence["command"] == ["git", "merge-tree", "--write-tree", candidate, source]
        assert evidence["timeout_seconds"] == 0.25
        assert "driver running" in json.loads(evidence["stdout_json"])
        assert evidence["head_sha"] == original_head
        assert evidence["merge_head_sha"] is None
        assert all(process.poll() is not None for process in processes)
        assert git.checkout_head(git_repo) == original_head
        assert git._run("diff", "--quiet").returncode == 0
        assert git._run("diff", "--cached", "--quiet").returncode == 0
        # Git's killed driver may leave its scratch files in the scene; those
        # are preserved for the recorded failure and can be cleaned in repair.
        assert all(line.startswith("?? .merge_file_") for line in git._run("status", "--porcelain").stdout.splitlines())
    finally:
        for scratch in git_repo.glob(".merge_file_*"):
            scratch.unlink()
        try:
            if pid_path.exists():
                driver_pid = int(pid_path.read_text())
                deadline = time.monotonic() + 2
                while True:
                    waited, _ = os.waitpid(driver_pid, os.WNOHANG)
                    if waited == driver_pid:
                        break
                    if time.monotonic() >= deadline:
                        os.kill(driver_pid, signal.SIGKILL)
                        os.waitpid(driver_pid, 0)
                        pytest.fail("merge driver remained executing after the Git timeout")
                    time.sleep(0.01)
        finally:
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0
