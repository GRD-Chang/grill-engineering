from __future__ import annotations

import json
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path

import pytest

import agent_run.cli as cli_module
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.github_retry import MAX_READ_ATTEMPTS
from agent_run.state import StateStore
from cli_run_supervision_support import _parent_only_agents
from conftest import seed_run, write_fixture
from test_cli import load_only_run_state, run_cli, run_internal_stage, stdout_json
from test_cli_delivery import final_run_agents, parent_round_agents, ticket
from test_ticket_191_resume import (
    _assert_production_run_rejects_before_readiness,
    _establish_ordinary_run_boundary,
    _tree_snapshot,
)
from run_acceptance_test_support import (
    _canonical_run_budget,
    _completed_run,
    _repair_artifact,
)


@pytest.mark.parametrize(
    "subject_location",
    ["ticket", "parent", "run_acceptance", "run_repair"],
)
@pytest.mark.parametrize(
    "blocked_reason",
    ["review_budget_exhausted", "modification_budget_exhausted"],
)
def test_ordinary_run_gate_covers_every_review_budget_checkpoint_subject(
    subject_location: str,
    blocked_reason: str,
) -> None:
    subject = {
        "phase": "blocked",
        "blocked_reason": blocked_reason,
        "pending_semantic_attempt": None,
    }
    state: dict[str, object] = {
        "status": "blocked",
        "terminal_kind": "waiting_human",
    }
    if subject_location == "ticket":
        state["active_ticket_job"] = subject
    elif subject_location == "parent":
        state["parent_job"] = subject
    elif subject_location == "run_acceptance":
        state["run_acceptance"] = subject
    else:
        state["run_acceptance"] = {"repair_job": subject}

    assert cli_module._ordinary_run_requires_explicit_action(state)


@pytest.mark.parametrize(
    "subject_location",
    ["ticket", "run_acceptance", "run_repair"],
)
@pytest.mark.parametrize(
    "blocked_reason",
    ["review_budget_exhausted", "modification_budget_exhausted"],
)
@pytest.mark.parametrize(
    "run_arguments",
    [
        pytest.param((), id="ordinary"),
        pytest.param(
            ("--development-model", "future-model"),
            id="with-profile-options",
        ),
        pytest.param(
            ("--ticket-review-rounds", "1"),
            id="with-policy-options",
        ),
    ],
)
def test_ordinary_run_preserves_every_non_parent_review_budget_subject(
    git_repo: Path,
    subject_location: str,
    blocked_reason: str,
    run_arguments: tuple[str, ...],
) -> None:
    state, states, _git = _completed_run(git_repo)
    budget = _canonical_run_budget()
    budget["checkpoint_reason"] = blocked_reason
    subject = {
        "phase": "blocked",
        "blocked_reason": blocked_reason,
        "pending_semantic_attempt": None,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": budget,
        "review_budget_history": [],
    }
    if subject_location == "ticket":
        job = state["ticket_jobs"]["2"]
        job.update(
            {
                "phase": "blocked",
                "blocked_reason": blocked_reason,
                "pending_semantic_attempt": None,
            }
        )
        job["review_budget"]["checkpoint_reason"] = blocked_reason
        state["active_ticket_job"] = deepcopy(job)
    else:
        acceptance = {
            **subject,
            "acceptance_generation": 1,
            "modification_attempts": 0,
            "validation_attempts": 0,
            "reviewer_thread_ids": [],
            "development_thread_id": None,
            "development_thread_history": [],
        }
        if subject_location == "run_repair":
            acceptance.update(
                {
                    "phase": "ready_for_human",
                    "repair_generation": 1,
                    "repair_job": {
                        **subject,
                        "repair_mode": "squash",
                        "repair_source": "acceptance",
                        "acceptance_artifact": _repair_artifact(),
                        "candidate_sha": state["base"]["sha"],
                        "development_thread_id": "run-repair-developer",
                        "development_thread_history": [],
                    },
                }
            )
        state["run_acceptance"] = acceptance
    state.update({"status": "blocked", "terminal_kind": "waiting_human"})
    states.save_run(str(state["run_id"]), state)
    state_root = git_repo / ".agent-run"
    state_before = _tree_snapshot(state_root)
    fixture = git_repo / "github.json"
    fixture_before = fixture.read_bytes()

    rejected = run_cli(git_repo, fixture, "run", "1", *run_arguments)

    assert rejected.returncode == 2, rejected.stderr
    assert stdout_json(rejected)["status"] == "blocked"
    assert _tree_snapshot(state_root) == state_before
    assert fixture.read_bytes() == fixture_before
    assert not list((state_root / "task-control").glob("*.json"))


@pytest.mark.parametrize(
    "subject_location",
    ["ticket", "parent", "run_acceptance", "run_repair", "run_publication"],
)
@pytest.mark.parametrize(
    "run_arguments",
    [
        pytest.param((), id="ordinary"),
        pytest.param(
            ("--development-model", "future-model"),
            id="with-profile-options",
        ),
        pytest.param(
            ("--ticket-review-rounds", "1"),
            id="with-policy-options",
        ),
    ],
)
def test_ordinary_run_preserves_local_gate_during_supervision_timeout(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    subject_location: str,
    run_arguments: tuple[str, ...],
) -> None:
    state, states, _git = _completed_run(git_repo)
    subject = {
        "phase": "blocked",
        "blocked_reason": "review_budget_exhausted",
        "pending_semantic_attempt": None,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
    }
    subject["review_budget"]["checkpoint_reason"] = "review_budget_exhausted"
    if subject_location == "ticket":
        job = state["ticket_jobs"]["2"]
        job.update(subject)
        state["active_ticket_job"] = deepcopy(job)
    elif subject_location == "parent":
        state["parent_job"] = {
            **subject,
            "parent_generation": 1,
            "modification_attempts": 0,
            "validation_attempts": 0,
            "publication_attempts": 0,
            "reviewer_thread_ids": [],
            "development_thread_id": None,
            "development_thread_history": [],
        }
    elif subject_location == "run_acceptance":
        state["run_acceptance"] = {
            **subject,
            "acceptance_generation": 1,
            "modification_attempts": 0,
            "validation_attempts": 0,
            "reviewer_thread_ids": [],
            "development_thread_id": None,
            "development_thread_history": [],
        }
    elif subject_location == "run_repair":
        state["run_acceptance"] = {
            **subject,
            "phase": "ready_for_human",
            "acceptance_generation": 1,
            "modification_attempts": 0,
            "validation_attempts": 0,
            "reviewer_thread_ids": [],
            "development_thread_id": None,
            "development_thread_history": [],
            "repair_generation": 1,
            "repair_job": {
                **subject,
                "repair_mode": "squash",
                "repair_source": "acceptance",
                "acceptance_artifact": _repair_artifact(),
                "candidate_sha": state["base"]["sha"],
                "development_thread_id": "run-repair-developer",
                "development_thread_history": [],
            },
        }
    else:
        state["run_publication"] = {
            "phase": "ready_for_human",
            "blocked_reason": "agent_requires_human",
            "human_blocker_phase": "run_publication",
            "human_blockers": ["Publication requires maintainer input."],
        }
    state.update(
        {"status": "supervision_timeout", "terminal_kind": "supervision_timeout"}
    )
    states.save_run(str(state["run_id"]), state)
    state_root = git_repo / ".agent-run"
    state_before = _tree_snapshot(state_root)
    fixture = git_repo / "github.json"
    fixture_before = fixture.read_bytes()

    rejected = run_cli(git_repo, fixture, "run", "1", *run_arguments)

    assert rejected.returncode == 2, rejected.stderr
    assert stdout_json(rejected)["status"] == "supervision_timeout"
    assert _tree_snapshot(state_root) == state_before
    assert fixture.read_bytes() == fixture_before
    assert not list((state_root / "task-control").glob("*.json"))

    _assert_production_run_rejects_before_readiness(
        git_repo,
        fixture,
        monkeypatch,
        capsys,
        run_arguments,
        expected_status="supervision_timeout",
    )
    assert _tree_snapshot(state_root) == state_before
    assert fixture.read_bytes() == fixture_before
    assert not list((state_root / "task-control").glob("*.json"))


def test_production_run_locates_custom_state_gate_before_readiness(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    state_root = tmp_path / "custom-state"
    state_home = tmp_path / "state-home"
    started = seed_run(
        git_repo,
        fixture,
        "1",
        "--state-dir",
        str(state_root),
        extra_env={"XDG_STATE_HOME": str(state_home)},
    )
    state = StateStore(state_root).load_run(str(stdout_json(started)["run_id"]))
    assert state is not None
    state["run_publication"] = {
        "phase": "ready_for_human",
        "blocked_reason": "agent_requires_human",
        "human_blocker_phase": "run_publication",
        "human_blockers": ["Publication requires maintainer input."],
    }
    state.update(
        {"status": "supervision_timeout", "terminal_kind": "supervision_timeout"}
    )
    StateStore(state_root).save_run(str(state["run_id"]), state)

    _assert_production_run_rejects_before_readiness(
        git_repo,
        fixture,
        monkeypatch,
        capsys,
        (),
        expected_status="supervision_timeout",
        state_root=state_root,
        state_home=state_home,
    )
    assert not (git_repo / ".agent-run").exists()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            {"status": "requeue_required"}, True, id="requeue-is-dedicated"
        ),
        pytest.param(
            {"status": "blocked", "terminal_kind": "waiting_human"},
            True,
            id="human-blocker-is-dedicated",
        ),
        pytest.param(
            {"status": "blocked", "terminal_kind": "permanent_blocked"},
            True,
            id="permanent-block-is-dedicated",
        ),
        pytest.param(
            {"status": "publication_pending"},
            True,
            id="exhausted-publication-retry-is-dedicated",
        ),
        pytest.param(
            {"status": "unsupported_scope_change"},
            True,
            id="scope-change-is-dedicated",
        ),
        pytest.param(
            {"status": "deterministic_contradiction"},
            True,
            id="contradiction-is-dedicated",
        ),
        pytest.param(
            {"status": "abandonment_pending"},
            True,
            id="abandonment-recovery-is-dedicated",
        ),
        pytest.param(
            {"status": "progress_exhausted", "terminal_kind": "waiting_human"},
            True,
            id="exhausted-progress-is-dedicated",
        ),
        pytest.param(
            {"status": "supervision_timeout"},
            False,
            id="run-may-supervise-timeout",
        ),
        pytest.param(
            {"status": "run_publication_pending"},
            False,
            id="run-may-start-publication",
        ),
    ],
)
def test_ordinary_run_gate_preserves_command_specific_next_steps(
    state: dict[str, str],
    expected: bool,
) -> None:
    assert cli_module._ordinary_run_requires_explicit_action(state) is expected


def _assert_run_preserves_boundary(
    git_repo: Path,
    fixture: Path,
    *,
    expected_status: str,
    run_arguments: tuple[str, ...],
) -> None:
    state_root = git_repo / ".agent-run"
    state_path = next((state_root / "runs").glob("*.json"))
    control_path = next((state_root / "task-control").glob("*.json"))
    state_before = state_path.read_bytes()
    control_before = control_path.read_bytes()
    control_document_before = json.loads(control_before)
    tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    unused_agents = _parent_only_agents(git_repo / "unused-agents.json")
    unused_data = json.loads(unused_agents.read_text(encoding="utf-8"))
    unused_data["developments"][0]["write_files"] = {
        "unexpected-successor.txt": "unexpected\n"
    }
    unused_agents.write_text(json.dumps(unused_data), encoding="utf-8")

    rejected = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        *run_arguments,
        "--agent-fixture",
        str(unused_agents),
    )

    assert rejected.returncode == 2, rejected.stderr
    assert stdout_json(rejected)["status"] == expected_status
    assert state_path.read_bytes() == state_before
    assert control_path.read_bytes() == control_before
    assert _tree_snapshot(state_root) == tree_before
    assert fixture.read_bytes() == fixture_before
    control_document_after = json.loads(control_path.read_bytes())
    assert control_document_after["action"]["executor_generation"] == (
        control_document_before["action"]["executor_generation"]
    )
    assert control_document_after["action_history"] == control_document_before[
        "action_history"
    ]
    assert not (git_repo / "unexpected-successor.txt").exists()


@pytest.mark.parametrize(
    "run_arguments",
    [
        pytest.param((), id="ordinary"),
        pytest.param(
            ("--development-model", "future-model"),
            id="with-profile-options",
        ),
        pytest.param(
            ("--parent-only-paired-rounds", "2"),
            id="with-policy-options",
        ),
    ],
)
@pytest.mark.parametrize(
    "blocked_reason",
    ["review_budget_exhausted", "modification_budget_exhausted"],
)
def test_ordinary_run_preserves_parent_budget_checkpoint(
    git_repo: Path,
    run_arguments: tuple[str, ...],
    blocked_reason: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    checkpoint_agents = git_repo / "checkpoint-agents.json"
    checkpoint_agents.write_text(
        json.dumps(parent_round_agents(1, passing_last=False)), encoding="utf-8"
    )
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--parent-only-paired-rounds",
        "1",
        "--agent-fixture",
        str(checkpoint_agents),
    )
    assert blocked.returncode == 2, blocked.stderr
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["blocked_reason"] == "review_budget_exhausted"
    if blocked_reason == "modification_budget_exhausted":
        state["parent_job"]["blocked_reason"] = blocked_reason
        state["parent_job"]["review_budget"]["checkpoint_reason"] = blocked_reason
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)

    _assert_run_preserves_boundary(
        git_repo,
        fixture,
        expected_status="blocked",
        run_arguments=run_arguments,
    )


def test_approval_refresh_exhaustion_does_not_create_a_successor_action(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _establish_ordinary_run_boundary(git_repo, fixture, "parent_approval_pending")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {
            "code": "github_read_failed",
            "message": f"graph read {attempt} has not converged",
        }
        for attempt in range(MAX_READ_ATTEMPTS + 1)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    state_root = git_repo / ".agent-run"
    state_path = next((state_root / "runs").glob("*.json"))
    control_path = next((state_root / "task-control").glob("*.json"))
    state_before = state_path.read_bytes()
    control_before = control_path.read_bytes()
    tree_before = _tree_snapshot(state_root)
    unused_agents = _parent_only_agents(git_repo / "unused-agents.json")

    waiting = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(unused_agents),
    )

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_external"
    assert state_path.read_bytes() == state_before
    assert control_path.read_bytes() == control_before
    assert _tree_snapshot(state_root) == tree_before
    assert json.loads(fixture.read_text(encoding="utf-8"))[
        "delivery_graph_read_failures"
    ] == []


def _establish_run_publication_pending(git_repo: Path) -> tuple[Path, Path]:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "run-publication-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr
    accepted = run_internal_stage(
        git_repo,
        fixture,
        "accept-run",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert accepted.returncode == 0, accepted.stderr
    assert stdout_json(accepted)["status"] == "run_publication_pending"
    return fixture, agents


def test_ordinary_run_advances_normal_run_publication_pending(
    git_repo: Path,
) -> None:
    fixture, agents = _establish_run_publication_pending(git_repo)

    advanced = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert advanced.returncode == 0, advanced.stderr
    assert stdout_json(advanced)["status"] == "run_approval_pending"
    control_path = next(
        (git_repo / ".agent-run" / "task-control").glob("*.json")
    )
    control = json.loads(control_path.read_bytes())
    assert control["action"]["kind"] == "run"
    assert control["action"]["executor_generation"] == 1
    assert control["action"]["status"] == "completed"
    assert control["action_history"] == []


@pytest.mark.parametrize(
    "current_status", ["supervision_timeout", "run_publication_pending"]
)
@pytest.mark.parametrize(
    "readiness_failure", ["active-runner-missing", "systemd-unavailable"]
)
def test_progressing_run_reports_execution_readiness_when_unavailable(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_status: str,
    readiness_failure: str,
) -> None:
    if current_status == "run_publication_pending":
        fixture, _agents = _establish_run_publication_pending(git_repo)
    else:
        state, states, _git = _completed_run(git_repo)
        state.update(
            {"status": "supervision_timeout", "terminal_kind": "supervision_timeout"}
        )
        states.save_run(str(state["run_id"]), state)
        fixture = git_repo / "github.json"

    state_root = git_repo / ".agent-run"
    state_tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    environment_carrier = git_repo / "runtime" / "environment-carrier.json"
    calls = {"lease": 0, "active_runner": 0, "host": 0, "readiness": 0}

    class UsageLease:
        def fileno(self) -> int:
            return 1

    class UnavailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            calls["host"] += 1

        def check_readiness(self) -> None:
            calls["readiness"] += 1
            raise cli_module.SystemdExecutionReadinessError(
                "user systemd unavailable"
            )

    def observed_runner_lease(_path: Path) -> object:
        calls["lease"] += 1
        return nullcontext(UsageLease())

    def active_runner_available() -> bool:
        calls["active_runner"] += 1
        return readiness_failure == "systemd-unavailable"

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
    monkeypatch.setattr(cli_module, "runner_usage_lease", observed_runner_lease)
    monkeypatch.setattr(cli_module, "_running_active_runner", active_runner_available)
    monkeypatch.setattr(
        cli_module, "SystemdUserExecutorHost", UnavailableSystemdHost
    )
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
    )

    return_code = cli_module.main(
        ["run", "1", "--repo", "example/project", "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    assert return_code == 2
    assert output["result"] == "error"
    assert output["status"] == "blocked"
    assert output["diagnostics"][0]["code"] == "execution_readiness"
    expected_calls = (
        {"lease": 1, "active_runner": 1, "host": 0, "readiness": 0}
        if readiness_failure == "active-runner-missing"
        else {"lease": 1, "active_runner": 1, "host": 1, "readiness": 1}
    )
    assert calls == expected_calls
    assert not environment_carrier.exists()
    assert _tree_snapshot(state_root) == state_tree_before
    assert fixture.read_bytes() == fixture_before


@pytest.mark.parametrize(
    "run_arguments",
    [
        pytest.param((), id="ordinary"),
        pytest.param(
            ("--development-model", "future-model"),
            id="with-profile-options",
        ),
        pytest.param(
            ("--ticket-review-rounds", "1"),
            id="with-policy-options",
        ),
    ],
)
def test_ordinary_run_preserves_requeue_required_boundary(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_arguments: tuple[str, ...],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    state = states.load_run(run_id)
    assert state is not None
    job = state["active_ticket_job"]
    assert isinstance(job, dict)
    job.update(
        {
            "ticket_branch_generation": 1,
            "ticket_branch": f"agent-run/{run_id}/ticket-3",
            "phase": "developing",
            "review_budget": {
                "window": 1,
                "development_attempts": 0,
                "reviewer_invocations": 0,
                "final_ci_fix_used": False,
                "review_artifacts": [],
                "checkpoint_reason": None,
            },
            "review_budget_history": [],
            "effective_revision": "stale-revision",
            "base_sha": GitRepository(git_repo).resolve(str(state["run_branch"])),
        }
    )
    state["ticket_jobs"] = {"3": job}
    states.save_run(run_id, state)
    stale = run_cli(git_repo, fixture, "run", "1")
    assert stale.returncode == 2, stale.stderr
    assert stdout_json(stale)["status"] == "requeue_required"

    _assert_run_preserves_boundary(
        git_repo,
        fixture,
        expected_status="requeue_required",
        run_arguments=run_arguments,
    )
    _assert_production_run_rejects_before_readiness(
        git_repo,
        fixture,
        monkeypatch,
        capsys,
        run_arguments,
        expected_status="requeue_required",
    )
