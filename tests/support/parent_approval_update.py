"""Real Git and bounded process barriers for Parent approval update tests."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any, Iterator

from support.cli import PROJECT_ROOT, load_only_run_state, run_cli, stdout_json
from support.cli_payloads import parent_publication, passing_acceptance
from support.cli_supervision import _interrupt_run, _parent_only_agents
from support.seeding import write_fixture
from support.workspace import managed_repo, managed_state


def git_command(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def ready_parent(repo: Path, *options: str) -> tuple[Path, dict[str, Any]]:
    fixture = write_fixture(repo / "github.json", issues={})
    agents = _parent_only_agents(repo / "initial-agents.json")
    result = run_cli(repo, fixture, "run", "1", "--agent-fixture", str(agents), *options)
    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["status"] == "parent_approval_pending"
    return fixture, load_only_run_state(repo)


def append_remote(
    repo: Path, fixture: Path, branch: str, filename: str, content: str,
) -> str:
    managed = managed_repo(repo)
    data = json.loads(fixture.read_text())
    branches = data["delivery"]["published_branches"]
    previous = branches.get(branch, data["default_head_sha"])
    collaborator = repo.parent / "approval-collaborator"
    git_command(managed, "worktree", "add", "--detach", str(collaborator), previous)
    try:
        (collaborator / filename).write_text(content)
        git_command(collaborator, "add", filename)
        git_command(collaborator, "commit", "-m", "append collaborator contribution")
        head = git_command(collaborator, "rev-parse", "HEAD")
    finally:
        git_command(managed, "worktree", "remove", "--force", str(collaborator))
    branches[branch] = head
    if branch == "main":
        data["default_head_sha"] = head
        git_command(managed, "update-ref", "refs/heads/main", head, previous)
        for pr in data["delivery"]["pull_requests"]:
            if pr["base_branch"] == branch:
                pr["base_sha"] = head
    else:
        for pr in data["delivery"]["pull_requests"]:
            if pr["branch"] == branch:
                pr["head_sha"] = head
    fixture.write_text(json.dumps(data))
    return head


def update_agents(repo: Path, *, conflict: bool = False) -> Path:
    path = repo / "update-agents.json"
    path.write_text(json.dumps({
        "developments": ([{
            "expected_thread_id": "parent-developer-1",
            "thread_id": "parent-developer-1",
            "summary": "Resolved the original merge conflict evidence.",
            "expected_files": {"partial.txt": "preserved conflict progress\n"},
            "write_files": {"parent-feature.txt": "done\nhuman contribution\n"},
        }] if conflict else []),
        "reviews": [passing_acceptance("update-reviewer", "Integrated Parent accepted")],
        "publications": [parent_publication()],
    }))
    return path


def control_record(repo: Path) -> dict[str, Any]:
    paths = list((managed_state(repo) / "task-control").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


_GATED_APPROVAL = """
import socket, sys
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.cli import main
from agent_run.external_supervision import ExternalSupervisor
from agent_run.git import GitRepository

barrier = socket.socket(fileno=int(sys.argv[1]))
barrier.settimeout(20)
boundary = sys.argv[2]

def hold():
    barrier.sendall(b'1')
    assert barrier.recv(1) == b'1', 'approval update barrier was not released'

original_integrate = GitRepository.integrate_remote_heads
def integrate(self, *args, **kwargs):
    result = original_integrate(self, *args, **kwargs)
    if boundary == 'synced':
        hold()
    return result
GitRepository.integrate_remote_heads = integrate

original_gate = FixtureAgentBackend._wait_for_invocation_gate
def invocation_gate(self, role, request, **kwargs):
    if (boundary == 'conflict' and role == 'developments') or (boundary == 'review' and role == 'reviews'):
        request['_invocation_event']('thread_started', reported_thread_id=(
            'parent-developer-1' if role == 'developments' else 'update-reviewer'
        ), attempt_count=1)
        if role == 'developments':
            assert request['repair_source'] == 'merge_conflict'
            Path(request['checkout'], 'partial.txt').write_text('preserved conflict progress\\n')
        hold()
    return original_gate(self, role, request, **kwargs)
FixtureAgentBackend._wait_for_invocation_gate = invocation_gate

original_retry = ExternalSupervisor.before_retry
def before_retry(self, state, **kwargs):
    if boundary == 'checks' and state.get('status') == 'waiting_checks':
        hold()
    return original_retry(self, state, **kwargs)
ExternalSupervisor.before_retry = before_retry
raise SystemExit(main(sys.argv[3:]))
"""


@contextmanager
def paused_update(
    repo: Path, fixture: Path, agents: Path, run_id: str, boundary: str,
) -> Iterator[subprocess.Popen[str]]:
    """Hold the Executor at a proven boundary, and always reap its processes."""
    observer, executor = socket.socketpair()
    with observer, executor:
        process = subprocess.Popen(
            [sys.executable, "-c", _GATED_APPROVAL, str(executor.fileno()), boundary,
             "approve", run_id, "--github-fixture", str(fixture),
             "--agent-fixture", str(agents), "--json"],
            cwd=repo, env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
            pass_fds=(executor.fileno(),), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        executor.close()
        try:
            observer.settimeout(20)
            assert observer.recv(1) == b"1", "approval did not reach its update boundary"
            yield process
        finally:
            _interrupt_run(process, repo)
