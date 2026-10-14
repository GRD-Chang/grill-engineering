"""新 Action 的持久重入必须应用真实命令意图，而非仅生成应用标记。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.controller import Controller
from agent_run.executor_host import BoundExecutorHost, ExecutorSpec
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_driver import DirectRunOperations
from agent_run.run_lifecycle import LifecycleRequest, RunLifecycle, prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.cli_actions import _parent_agents
from support.published_run import prepare_published_run
from support.seeding import write_fixture
from support.workspace import managed_repo, managed_state, prepare_workspace


@pytest.mark.parametrize("kind", ["approve", "revise", "requeue"])
def test_unbound_reentry_applies_real_existing_run_command(
    git_repo: Path, tmp_path: Path, kind: str,
) -> None:
    if kind == "requeue":
        prepare_workspace(git_repo)
        fixture = write_fixture(git_repo / "github.json", issues={})
        git = GitRepository(managed_repo(git_repo))
        states = StateStore(managed_state(git_repo))
        controller = Controller(FixtureGitHubReader(fixture), git, states)
        initial, _ = controller.start(1)
        initial = ParentDeliveryEngine(
            git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
            agents=FixtureAgentBackend(_parent_agents(git_repo / "agents.json")),
        ).deliver(initial["run_id"])
        assert initial["status"] == "parent_approval_pending"
        changed = json.loads(fixture.read_text())
        changed["parent"]["body"] = "Changed authoritative requirement"
        fixture.write_text(json.dumps(changed), encoding="utf-8")
        initial, _ = controller.resume(initial["run_id"])
        assert initial["status"] == "requeue_required"
    else:
        fixture, initial = prepare_published_run(git_repo)
    run_id = initial["run_id"]
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    # 场景 builder 的 idle CLI ownership 不参与此处独立 Task Control 测试。
    # 以新控制目录真实准入空绑定 Action，不修改或清除已有控制记录。
    control = TaskControlStore(tmp_path / "control")
    payload = {"parent": 1, "run_id": run_id}
    if kind == "revise":
        payload["message"] = " Preserve this exact feedback "
    claim = control.claim_action(task, kind=kind, payload=payload)
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
    admitted = control.load(task)
    assert admitted["run_id"] is admitted["action"]["run_id"] is admitted["executor"]["run_id"] is None
    assert admitted["action"]["payload"]["run_id"] == run_id

    # 新读取实例和真实生产命令适配器；不携带 initial 作为 current。
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    reader = FixtureGitHubReader(fixture)
    boundary_agents = git_repo / "boundary-workers.json"
    boundary_agents.write_text(json.dumps({"developments": [{
        "thread_id": "new-generation", "human_blockers": ["Stop at first new-generation Worker"],
    }]}), encoding="utf-8")
    operations = DirectRunOperations(
        controller=Controller(reader, git, states), states=states, git=git,
        github_reader=reader, publisher_factory=lambda: FixtureGitHubPublisher(fixture, git),
        agents=FixtureAgentBackend(boundary_agents),
    )

    def select(action):
        target = action["payload"]["run_id"]
        prepare = lambda state: prepare_action_application_receipt(state, action)
        if kind == "revise":
            outcome = operations.revise(target, action["payload"]["message"], prepare_state=prepare)
        else:
            outcome = getattr(operations, kind)(target, prepare_state=prepare)
        return outcome.state, True

    lifecycle = RunLifecycle(
        states=states, control=TaskControlStore(control.root), task=task,
        host=BoundExecutorHost(action_id=claim.action_id, generation=reservation.generation),
        preflight=lambda: pytest.fail("reentry must read persisted target"),
        select_run=select, initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task, run_id=run_id, action_id=action_id, generation=generation,
        ),
        # 本例在真实命令意图后停止，不把 Worker/完整交付乘到组合测试。
        execute=lambda target: StateStore(states.root).load_current_run(target),
    )
    final, _, _ = lifecycle.execute_claimed(action_id=claim.action_id, generation=reservation.generation)
    persisted = StateStore(states.root).load_current_run(run_id)
    assert persisted == final
    bound = control.load(task)
    assert bound["run_id"] == bound["action"]["run_id"] == bound["executor"]["run_id"] == run_id
    receipt = persisted["action_application_receipt"]
    assert receipt["kind"] == kind
    assert receipt["action_id"] == claim.action_id
    assert receipt["executor_generation"] == reservation.generation
    assert bound["action"]["application_observed"] is True
    delivery = json.loads(fixture.read_text())["delivery"]
    if kind == "approve":
        assert persisted["status"] == "completed"
        grant = persisted["run_publication"]["approval_grant"]
        assert grant["pr_number"] == initial["run_publication"]["pr_number"]
        assert grant["repository"] == "example/project"
        final_pr = next(pr for pr in delivery["pull_requests"] if pr["number"] == grant["pr_number"])
        assert final_pr["state"] == "MERGED"
    elif kind == "revise":
        assert persisted["status"] == "run_acceptance_pending"
        request = persisted["run_acceptance"]["repair_request"]
        assert request["repair_source"] == "human_revision"
        assert request["human_feedback"] == payload["message"].strip()
        assert persisted["run_acceptance"]["phase"] == "repairing"
        assert persisted["run_publication"]["phase"] == "stale"
        assert "approval_grant" not in persisted["run_publication"]
        assert all(pr["state"] != "MERGED" for pr in delivery["pull_requests"] if pr.get("scope") == "final_run")
    else:
        assert persisted["parent_job"]["parent_generation"] == initial["parent_job"]["parent_generation"] + 1
        assert "candidate_sha" not in persisted["parent_job"]
        assert persisted["parent"]["body"] == "Changed authoritative requirement"
        assert delivery["pull_requests"][0]["state"] == "CLOSED"


@pytest.mark.parametrize("entrypoint", ["submit", "execute_claimed"])
@pytest.mark.parametrize("successor", [False, True])
def test_public_result_reads_the_receipt_store_after_executor_exit(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, successor: bool,
) -> None:
    from copy import deepcopy

    fixture = write_fixture(git_repo / "github.json", issues={})
    actual = StateStore(tmp_path / "actual-state")
    initial, _ = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), actual).start(1)
    run_id = str(initial["run_id"])
    initial["response_store_marker"] = "actual"
    actual.save_run(run_id, initial)
    unrelated = StateStore(tmp_path / "unrelated-state")
    other = deepcopy(initial)
    other["response_store_marker"] = "unrelated"
    unrelated.save_run(run_id, other)
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(tmp_path / "control")
    payload = {"parent": 1}
    claim = control.claim_action(task, kind="run", payload=payload)
    assert claim.action_id is not None
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
    control.bind_run(task, claim.action_id, run_id, state_dir=actual.root)

    class ReplacingHost(BoundExecutorHost):
        def ensure(self, *args, **kwargs):
            observation = super().ensure(*args, **kwargs)
            if successor:
                following = control.claim_action(task, kind="run", payload={"parent": 1, "message": "successor"})
                assert following.action_id != claim.action_id
                control.bind_run(task, following.action_id, run_id, state_dir=unrelated.root)
            return observation

    def select(action):
        current = actual.load_current_run(run_id)
        assert current is not None
        prepare_action_application_receipt(current, action)
        actual.save_run(run_id, current)
        return current, True

    lifecycle = RunLifecycle(
        states=unrelated, control=control, task=task,
        host=ReplacingHost(action_id=claim.action_id, generation=reservation.generation),
        preflight=lambda: actual.load_current_run(run_id), select_run=select,
        initialize_profile=None,
        executor_spec=lambda target, action_id, generation: ExecutorSpec(
            task=task, run_id=target, action_id=action_id, generation=generation,
        ),
        execute=lambda target: actual.load_current_run(target),
    )
    from agent_run.object_state import ObjectStateStore

    query_roots: list[Path] = []
    load_full = ObjectStateStore.load_full_run

    def response_query(store, target):
        assert not store._store._repository.in_transaction
        assert getattr(store._store._commit_context, "notifications", None) is None
        query_roots.append(store.root)
        return load_full(store, target)

    monkeypatch.setattr(ObjectStateStore, "load_full_run", response_query)
    if entrypoint == "submit":
        final, _, receipt = lifecycle.submit(LifecycleRequest(task=task, kind="run", payload=payload))
    else:
        final, _, receipt = lifecycle.execute_claimed(action_id=claim.action_id, generation=reservation.generation)
    assert query_roots == [actual.root]
    assert final == actual.load_current_run(run_id)
    assert final["response_store_marker"] == "actual"
    assert receipt.action_id == claim.action_id
    observed = control.snapshot(task, receipt.action_id)
    assert observed is not None
    assert observed["run_state_dir"] == str(actual.root.resolve())
    assert observed["executor"]["status"] == "exited"
    assert unrelated.load_current_run(run_id) == other
    if successor:
        assert control.load(task)["run_state_dir"] == str(unrelated.root.resolve())
