from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey
from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import PROJECT_ROOT, load_only_run_state, run_cli, stdout_json
from test_cli_delivery import ticket


@pytest.mark.parametrize("case", ["exited", "unknown_executor", "foreign_receipt"])
def test_rejected_action_requires_exact_unapplied_exit_evidence(
    tmp_path: Path, case: str,
) -> None:
    task = TaskKey(tmp_path / "repo", "example/project", 1)
    control = TaskControlStore(tmp_path / "state")
    first = control.claim_action(task, kind="run", payload={"parent": 1})
    assert first.action_id is not None
    control.bind_run(task, first.action_id, "run-1")
    control.begin_executor(task, action_id=first.action_id, run_id="run-1")
    state: dict[str, Any] = {"run_id": "run-1", "status": "execution_failed"}
    prepare_action_application_receipt(state, first.action or {})
    control.finish_executor(task, action_id=first.action_id, generation=1)
    successor = control.claim_action(task, kind="resume", payload={"parent": 1})
    assert successor.action_id is not None
    control.begin_executor(task, action_id=successor.action_id, run_id=None)
    if case == "unknown_executor":
        control.fail_action(task, action_id=successor.action_id, failure="rejected")
    else:
        control.finish_executor(
            task, action_id=successor.action_id, generation=2, failure="rejected"
        )
    if case == "foreign_receipt":
        state["action_application_receipt"]["executor_generation"] = 99
    before = control.path_for(task).read_bytes()

    if case == "exited":
        result = control.reconcile_from_run(task, state)
        assert result is not None
        assert result["action"]["status"] == "failed"
        assert result["action"]["application_observed"] is False
    else:
        with pytest.raises(ActionReconciliationError):
            control.reconcile_from_run(task, state)
    assert control.path_for(task).read_bytes() == before


def test_public_resume_preserves_work_after_unclassified_transport_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    # Inject only the external transport boundary, in a real CLI/Executor
    # process. The Driver and its durable failure recording remain real.
    script = """
import sys
from agent_run.cli import main
from agent_run.github_fixture import FixtureGitHubPublisher
def failed_push(self, branch, head_sha, *, expected_remote_sha):
    raise TimeoutError("transport socket timed out before push")
FixtureGitHubPublisher.publish_branch = failed_push
raise SystemExit(main(sys.argv[1:]))
"""
    failed = subprocess.run(
        [sys.executable, "-c", script, "run", "1", "--json",
         "--github-fixture", str(fixture), "--agent-fixture", str(agents)],
        cwd=git_repo, env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        capture_output=True, text=True, check=False, timeout=30,
    )

    assert failed.returncode == 2, failed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "execution_failed"
    assert "transport socket timed out before push" in str(state["diagnostics"])
    job = state["active_ticket_job"]
    assert job["ticket_number"] == 3
    assert job["phase"] == "publishing"
    candidate = job["candidate_sha"]
    publication = job["publication_sha"]
    budget = dict(job["review_budget"])

    data = json.loads(fixture.read_text())
    data["issues"]["3"]["labels"] = ["ready-for-human"]
    fixture.write_text(json.dumps(data))
    rejected = run_cli(
        git_repo, fixture, "resume", "1", "--agent-fixture", str(agents)
    )
    assert rejected.returncode == 2, rejected.stderr
    assert load_only_run_state(git_repo) == state
    data["issues"]["3"]["labels"] = ["ready-for-agent"]
    fixture.write_text(json.dumps(data))

    resumed = run_cli(
        git_repo, fixture, "resume", "1", "--agent-fixture", str(agents)
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    resumed_state = load_only_run_state(git_repo)
    resumed_job = resumed_state["ticket_jobs"]["3"]
    assert resumed_job["candidate_sha"] == candidate
    assert resumed_job["publication_sha"] == publication
    assert resumed_job["review_budget"] == budget
    pulls = json.loads(fixture.read_text())["delivery"]["pull_requests"]
    assert len([pull for pull in pulls if pull.get("primary_ticket") == 3]) == 1
