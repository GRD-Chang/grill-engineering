"""Repository tip and native-auth regressions with synthetic gh subprocesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run.adapters.github import GhGitHubReader
import agent_run.adapters.auth.identity as identity
from agent_run.adapters.github_commands import MAX_COMMAND_OUTPUT_BYTES
from agent_run.errors import GitHubIdentityError, GitHubReadError


SHA = "a" * 40


def _gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str) -> dict[str, str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(f"#!{sys.executable}\n" + program)
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("HOME", str(tmp_path))
    return {"PATH": str(tools), "HOME": str(tmp_path)}


@pytest.mark.parametrize(("branch", "encoded"), [
    ("main", "main"), ("feature/ci", "feature%2Fci"),
    ("feature/#fix%", "feature%2F%23fix%25"),
])
def test_repository_tip_avoids_large_commit_capture(tmp_path, monkeypatch, branch, encoded):
    calls = tmp_path / "calls.jsonl"
    _gh(tmp_path, monkeypatch, f'''import json,sys
from pathlib import Path
args=sys.argv[1:]
with Path({str(calls)!r}).open('a') as f:f.write(json.dumps(args)+'\\n')
if args[:2]==['repo','view']:
    print(json.dumps({{"nameWithOwner":"example/project","defaultBranchRef":{{"name":{branch!r}}}}}))
elif args==['api',{('repos/example/project/git/ref/heads/' + encoded)!r}]:
    print(json.dumps({{"ref":{('refs/heads/' + branch)!r},"object":{{"type":"commit","sha":{SHA!r}}}}}))
elif args[:1]==['api'] and '/commits/' in args[1]:
    print(json.dumps({{"sha":{SHA!r},"files":[{{"patch":"x"*{MAX_COMMAND_OUTPUT_BYTES + 8192}}}]}}))
else:
    raise SystemExit(9)
''')
    repository = GhGitHubReader("example/project").repository()
    assert repository.default_head_sha == SHA
    assert repository.default_branch == branch
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert commands[-1] == ["api", "repos/example/project/git/ref/heads/" + encoded]
    assert not any("/commits/" in str(command) for command in commands)
    assert MAX_COMMAND_OUTPUT_BYTES == 1024 * 1024


@pytest.mark.parametrize(("status", "stderr", "kind", "auth_failed"), [
    (None, "error connecting to api.github.com: dial tcp: i/o timeout", "network", False),
    (401, "gh: Bad credentials (HTTP 401)", "unauthorized", True),
    (403, "gh: Forbidden (HTTP 403)", "forbidden", True),
    (429, "gh: Rate limited (HTTP 429)", "http_error", False),
    (503, "gh: Service unavailable (HTTP 503)", "http_error", False),
    (None, "arbitrary failure with private payload", "command_failed", False),
])
def test_native_auth_probe_keeps_safe_failure_category(tmp_path, monkeypatch, status, stderr, kind, auth_failed):
    headers = f"HTTP/2.0 {status} Synthetic\r\n\r\n" if status is not None else ""
    environment = _gh(tmp_path, monkeypatch, f'''import sys
sys.stdout.write({headers!r})
sys.stderr.write({(stderr + chr(10) + 'Authorization: Bearer synthetic-private-value')!r})
raise SystemExit(1)
''')
    expected = GitHubIdentityError if auth_failed else GitHubReadError
    with pytest.raises(expected) as caught:
        identity.validate_worker_auth({"source": "host"}, environment)
    diagnostic = caught.value.__cause__
    assert diagnostic is not None
    assert caught.value.__context__ is None and diagnostic.__context__ is None
    assert diagnostic.category == kind
    assert diagnostic.returncode == 1
    assert diagnostic.http_status == status
    assert diagnostic.authorization_failed is auth_failed
    for error in (caught.value, diagnostic):
        assert "synthetic-private-value" not in str(error)
        assert "arbitrary failure" not in str(error)
        assert len(str(error).encode()) < 512
    if not auth_failed:
        assert caught.value.code == "github_auth_unavailable"


def test_native_auth_probe_process_error_is_not_invalid_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GitHubReadError) as caught:
        identity.validate_worker_auth({"source": "host"}, {"PATH": str(tmp_path)})
    assert caught.value.code == "github_auth_unavailable"
    assert caught.value.__cause__.category == "process_error"
    assert caught.value.__cause__.errno == 2
    assert caught.value.__cause__.authorization_failed is False


def test_native_auth_probe_timeout_keeps_safe_cause(tmp_path, monkeypatch):
    environment = _gh(tmp_path, monkeypatch, "import signal\nsignal.pause()\n")
    from agent_run.runtime.command import capture_command
    captured = []

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 15, stderr=b"synthetic-private-value")

    def bounded(process, *, timeout, max_output):
        assert timeout == 15
        captured.append(process)
        return capture_command(process, timeout=0, max_output=max_output)

    monkeypatch.setattr(identity.subprocess, "run", timeout_run)
    monkeypatch.setattr(identity, "capture_command", bounded, raising=False)
    with pytest.raises(GitHubReadError) as caught:
        identity.validate_worker_auth({"source": "host"}, environment)
    assert caught.value.__cause__.category == "timeout"
    assert caught.value.__cause__.authorization_failed is False
    assert "synthetic-private-value" not in str(caught.value)
    assert len(captured) == 1 and captured[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(captured[0].pid, os.WNOHANG)


def test_native_auth_probe_success_ignores_body_and_tool_noise(tmp_path, monkeypatch):
    environment = _gh(tmp_path, monkeypatch, "import sys\nsys.stdout.write('HTTP/2.0 200 OK\\r\\n\\r\\n')\nsys.stderr.write('synthetic-private-value'*10000)\n")
    assert identity.validate_worker_auth({"source": "host"}, environment) is None


@pytest.mark.parametrize("response", [
    {"ref": "refs/heads/other", "object": {"type": "commit", "sha": SHA}},
    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": SHA}},
    {"ref": "refs/heads/main", "object": {"type": "commit"}},
])
def test_repository_tip_rejects_wrong_or_incomplete_reference(monkeypatch, response):
    responses = iter([{"nameWithOwner": "example/project", "defaultBranchRef": {"name": "main"}}, response])
    monkeypatch.setattr(GhGitHubReader, "_gh_json", lambda *args: next(responses))
    with pytest.raises(GitHubReadError) as caught:
        GhGitHubReader("example/project").repository()
    assert caught.value.code == "github_invalid_response"


def test_repository_ref_read_keeps_retry_and_error_propagation(monkeypatch):
    import agent_run.adapters.github_commands as commands
    calls = []

    def read(arguments, **kwargs):
        calls.append(arguments)
        if arguments[1] == "repo":
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"nameWithOwner": "example/project", "defaultBranchRef": {"name": "main"}}), "")
        return subprocess.CompletedProcess(arguments, 1, "", "HTTP 403: unavailable")

    monkeypatch.setattr(commands, "_run_bounded_command", read)
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: None)
    with pytest.raises(GitHubReadError) as caught:
        GhGitHubReader("example/project").repository()
    assert caught.value.code == "github_read_failed"
    assert str(caught.value) == "HTTP 403: unavailable"
    assert calls[1:] == [["gh", "api", "repos/example/project/git/ref/heads/main"]] * 3


def test_native_auth_probe_does_not_trust_status_in_stderr(tmp_path, monkeypatch):
    environment = _gh(tmp_path, monkeypatch, "import sys\nsys.stderr.write('HTTP 401: token=synthetic-private-value')\nraise SystemExit(1)\n")
    with pytest.raises(GitHubReadError) as caught:
        identity.validate_worker_auth({"source": "host"}, environment)
    assert caught.value.__cause__.http_status is None
    assert caught.value.__cause__.category == "command_failed"
    assert caught.value.__cause__.authorization_failed is False
    assert "synthetic-private-value" not in str(caught.value)


@pytest.mark.parametrize("header", ["X-RateLimit-Remaining: 0", "Retry-After: 30"])
def test_native_auth_probe_http403_rate_limit_is_not_invalid_credentials(tmp_path, monkeypatch, header):
    headers = f"HTTP/2.0 403 Forbidden\r\n{header}\r\n\r\n"
    environment = _gh(tmp_path, monkeypatch, f"import sys\nsys.stdout.write({headers!r})\nraise SystemExit(1)\n")
    with pytest.raises(GitHubReadError) as caught:
        identity.validate_worker_auth({"source": "host"}, environment)
    diagnostic = caught.value.__cause__
    assert diagnostic.category == "rate_limited" and diagnostic.http_status == 403
    assert diagnostic.authorization_failed is False and diagnostic.retryable is True
