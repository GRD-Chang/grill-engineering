"""共享测试支撑：resume；每次调用保持独立状态。"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path
import pytest
from support.workspace import managed_repo, managed_state
import agent_run.cli as cli_module
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_invocation import record_operator_stop
from agent_run.controller import Controller
from agent_run.executor_host import _process_start_token
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.publication_operation_retry import record_publication_operation_failure
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.cli_supervision import _parent_only_agents
from support.cli_fixtures import run_agents
from support.cli import load_only_run_state, run_cli, run_internal_stage, stdout_json
from support.cli_payloads import passing_acceptance, ticket
from support.seeding import seed_run

from support.state import include_in_state_snapshot
from support.state import persist_run_fixture


def _parent_resume_agents(git_repo: Path, boundary: str) -> Path:
    outcome: dict[str, object]
    if boundary == "execution_failed":
        filename = "failed-agents.json"
        outcome = {"error_after_writes": "fixture process failed"}
    elif boundary == "ready_for_human":
        filename = "human-blocked-agents.json"
        outcome = {"human_blockers": ["Maintainer approval is required."]}
    else:
        raise ValueError(f"unsupported Parent resume boundary: {boundary}")
    agents = git_repo / filename
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-development",
                        **outcome,
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    return agents


def _fail_parent_run(git_repo: Path, fixture: Path) -> None:
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(_parent_resume_agents(git_repo, "execution_failed")),
    )
    assert blocked.returncode == 2, blocked.stderr


def _resumed_parent_agents(git_repo: Path) -> Path:
    resumed_agents = _parent_only_agents(git_repo / "resumed-agents.json")
    agent_data = json.loads(resumed_agents.read_text(encoding="utf-8"))
    agent_data["developments"][0].update(
        {
            "expected_thread_id": "parent-development",
            "thread_id": "parent-development",
        }
    )
    resumed_agents.write_text(json.dumps(agent_data), encoding="utf-8")
    return resumed_agents


def _block_parent_for_human(git_repo: Path, fixture: Path) -> None:
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(_parent_resume_agents(git_repo, "ready_for_human")),
    )
    assert blocked.returncode == 2, blocked.stderr
    assert load_only_run_state(git_repo)["status"] == "ready_for_human"


def _prepare_terminal_resume_boundary(
    git_repo: Path, fixture: Path, boundary: str,
) -> None:
    """为 Control 拒绝矩阵准备已退出的真实 Parent 工作与匹配回执。

    只省去被测动作之前的 CLI/Executor 进程监督；Controller、Parent
    Delivery、Git checkout、预算和 Control API 均真实执行。三种边界的
    完整进程流程仍由 test_ordinary_run_preserves_explicit_authorization_boundaries
    覆盖；本函数不缓存或复制其他用例的可变状态。
    """
    if boundary not in {"execution_failed", "ready_for_human", "operator_stopped"}:
        raise ValueError(f"unsupported terminal resume boundary: {boundary}")
    started = seed_run(git_repo, fixture)
    assert started.returncode == 0, started.stderr
    state = load_only_run_state(git_repo)
    run_id = str(state["run_id"])
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    control = TaskControlStore(states.root)
    task = TaskKey(git.root, str(state["repository"]), 1)
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action_id is not None and claim.action is not None
    generation = control.begin_executor(
        task, action_id=claim.action_id, run_id=run_id, runner_binding="0" * 16,
    ).generation
    control.bind_run(
        task, claim.action_id, run_id, generation=generation, state_dir=states.root,
    )
    control.mark_handshake(
        task, action_id=claim.action_id, generation=generation,
        pid=os.getpid(), process_start_token=_process_start_token(os.getpid()),
    )
    prepare_action_application_receipt(state, claim.action)
    persist_run_fixture(states, run_id, state)
    control.record_application(
        task, action_id=claim.action_id, run_id=run_id, generation=generation,
        payload_digest=str(claim.action["payload_digest"]), state_dir=states.root,
    )
    agents = (
        _parent_only_agents(git_repo / "initial-agents.json")
        if boundary == "operator_stopped"
        else _parent_resume_agents(git_repo, boundary)
    )
    engine = ParentDeliveryEngine(
        git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
        agents=FixtureAgentBackend(agents),
    )
    if boundary == "execution_failed":
        with pytest.raises(ValueError, match="fixture process failed") as failed:
            engine.deliver(run_id)
        assert Controller(FixtureGitHubReader(fixture), git, states).record_execution_failure(
            run_id, str(failed.value),
        )
    else:
        result = engine.deliver(run_id)
        if boundary == "operator_stopped":
            assert result["status"] == "parent_approval_pending"
            current = states.load_current_run(run_id)
            assert current is not None
            record_operator_stop(
                current, save=lambda stopped: persist_run_fixture(states, run_id, stopped),
            )
        else:
            assert result["status"] == boundary
    assert load_only_run_state(git_repo)["status"] == boundary
    control.finish_executor(
        task, action_id=claim.action_id, generation=generation, result_status=boundary,
        failure="fixture process failed" if boundary == "execution_failed" else None,
    )


def _establish_ordinary_run_boundary(
    git_repo: Path,
    fixture: Path,
    boundary: str,
) -> None:
    if boundary == "execution_failed":
        _fail_parent_run(git_repo, fixture)
    elif boundary == "ready_for_human":
        _block_parent_for_human(git_repo, fixture)
    elif boundary == "blocked_waiting_human":
        _block_parent_for_human(git_repo, fixture)
        state = load_only_run_state(git_repo)
        state.update({"status": "blocked", "terminal_kind": "waiting_human"})
        persist_run_fixture(StateStore(managed_state(git_repo)), str(state["run_id"]), state)
    elif boundary == "permanent_blocked":
        blocked = run_cli(git_repo, fixture, "run", "1")
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "publication_pending":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        state = load_only_run_state(git_repo)
        publication = state["run_publication"]
        assert isinstance(publication, dict)
        for _ in range(5):
            record_publication_operation_failure(
                publication, RuntimeError("publication readback failed")
            )
        publication["phase"] = "publication_pending"
        state.update(
            {"status": "publication_pending", "terminal_kind": "publication_pending"}
        )
        persist_run_fixture(StateStore(managed_state(git_repo)), str(state["run_id"]), state)
    elif boundary == "unsupported_scope_change":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data["parent"]["sub_issues"] = [3]
        fixture_data["issues"] = {"3": ticket()}
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        blocked = run_internal_stage(
            git_repo,
            fixture,
            "deliver",
            str(stdout_json(started)["run_id"]),
        )
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "deterministic_contradiction":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        final_pr = next(
            pull
            for pull in fixture_data["delivery"]["pull_requests"]
            if pull.get("scope") == "final_run"
        )
        final_pr["head_repository"] = "foreign/project"
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        state = load_only_run_state(git_repo)
        state.update(
            {"status": "waiting_external", "terminal_kind": "waiting_external"}
        )
        state["run_publication"]["phase"] = "waiting_external"
        persist_run_fixture(StateStore(managed_state(git_repo)), str(state["run_id"]), state)
        blocked = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "contradiction-agents.json")),
        )
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "abandonment_pending":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        state = load_only_run_state(git_repo)
        state.update(
            {"status": "abandonment_pending", "terminal_kind": "abandonment_pending"}
        )
        persist_run_fixture(StateStore(managed_state(git_repo)), str(state["run_id"]), state)
    elif boundary == "progress_exhausted":
        blocked = run_cli(git_repo, fixture, "run", "1")
        assert blocked.returncode == 2, blocked.stderr
        state = load_only_run_state(git_repo)
        state["terminal_kind"] = "waiting_human"
        persist_run_fixture(StateStore(managed_state(git_repo)), str(state["run_id"]), state)
    else:
        agents = (
            run_agents(git_repo / "initial-agents.json")
            if boundary.startswith("run_approval_pending")
            else _parent_only_agents(git_repo / "initial-agents.json")
        )
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(agents),
        )
        assert started.returncode == 0, started.stderr
        if boundary == "run_approval_pending_after_revise":
            revision_agents = run_agents(git_repo / "revision-agents.json")
            revision_data = json.loads(
                revision_agents.read_text(encoding="utf-8")
            )
            revision_data["developments"] = [
                {
                    "expected_thread_id": None,
                    "thread_id": "run-revision-developer",
                    "summary": "Applied the requested Run revision.",
                    "write_files": {"run-revision.txt": "revised\n"},
                }
            ]
            revision_data["reviews"] = []
            revision_data["run_reviews"] = [
                passing_acceptance(
                    "run-revision-reviewer", "The revision passed."
                )
            ]
            revision_agents.write_text(json.dumps(revision_data), encoding="utf-8")
            revised = run_cli(
                git_repo,
                fixture,
                "revise",
                "1",
                "--message",
                "preserve the explicit approval boundary",
                "--agent-fixture",
                str(revision_agents),
            )
            assert revised.returncode == 0, f"{revised.stdout}\n{revised.stderr}"
        if boundary == "operator_stopped":
            state = load_only_run_state(git_repo)
            record_operator_stop(
                state,
                save=lambda stopped: persist_run_fixture(StateStore(managed_state(git_repo)), str(stopped["run_id"]), stopped),
            )
    expected_status = (
        "blocked"
        if boundary in {"blocked_waiting_human", "permanent_blocked"}
        else boundary.removesuffix("_after_revise")
    )
    assert load_only_run_state(git_repo)["status"] == expected_status


def _tree_snapshot(root: Path) -> dict[Path, bytes | None]:
    return {
        path.relative_to(root): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*") if include_in_state_snapshot(path)
    }


def _assert_production_run_rejects_before_readiness(
    git_repo: Path,
    fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_arguments: tuple[str, ...],
    *,
    expected_status: str,
    state_root: Path | None = None,
) -> None:
    state_root = state_root or managed_state(git_repo)
    state_tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    environment_carrier = git_repo / "runtime" / "environment-carrier.json"
    capsys.readouterr()

    for readiness_failure in ("active-runner-missing", "systemd-unavailable"):
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

        with monkeypatch.context() as readiness:
            readiness.chdir(git_repo)

            readiness.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
            readiness.setattr(cli_module, "runner_usage_lease", observed_runner_lease)
            readiness.setattr(
                cli_module, "_running_active_runner", active_runner_available
            )
            readiness.setattr(
                cli_module, "SystemdUserExecutorHost", UnavailableSystemdHost
            )
            readiness.setattr(
                cli_module,
                "GhGitHubReader",
                lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
            )
            readiness.setattr("agent_run.workspace_cli.GhGitHubReader", lambda _repo=None, *, working_directory: FixtureGitHubReader(fixture))

            return_code = cli_module.main(
                ["run", "1", "--repo", "example/project", *run_arguments, "--json"]
            )

        output = json.loads(capsys.readouterr().out)
        assert return_code == 2
        assert output["result"] == "rejected"
        assert output["status"] == expected_status
        assert calls == {"lease": 0, "active_runner": 0, "host": 0, "readiness": 0}
        assert not environment_carrier.exists()
        assert _tree_snapshot(state_root) == state_tree_before
        assert fixture.read_bytes() == fixture_before
