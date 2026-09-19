from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from support.workspace import managed_state

from agent_run.cli import (
    _print_interruption_result,
    build_parser,
)
from agent_run.state import StateStore
from cli_fixtures import run_agents
from conftest import seed_run, write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance, publication, ticket


def test_public_parser_exposes_only_run_as_the_ordinary_entrypoint() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert "start" not in help_text
    assert "--new-run" not in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "1", "--new-run"])


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("run", ["1"]),
        ("resume", ["1"]),
        ("approve", ["1"]),
        ("revise", ["1", "--message", "please revise"]),
        ("requeue", ["1"]),
    ],
)
def test_lifecycle_mutations_offer_explicit_machine_output(
    command: str, arguments: list[str]
) -> None:
    parsed = build_parser().parse_args([command, *arguments, "--json"])

    assert parsed.as_json is True


def test_run_scoped_configure_accepts_parent_or_exact_run_selector() -> None:
    parsed = build_parser().parse_args(
        ["configure", "1", "--development-model", "custom", "--json"]
    )

    assert parsed.run_id == "1"
    assert parsed.as_json is True


def _parent_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer",
                        "summary": "Implemented the Parent delivery.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer", "passed")],
            }
        ),
        encoding="utf-8",
    )
    return path


def _revision_agents(path: Path) -> Path:
    template_path = path.with_name("revision-template.json")
    template = run_agents(template_path)
    template_data = json.loads(template.read_text(encoding="utf-8"))
    label = path.stem
    data = {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": f"{label}-developer",
                "summary": "Applied the requested Run revision.",
                "write_files": {"run-revision.txt": f"{label}\n"},
            }
        ],
        "publications": [publication()],
        "reviews": [],
        "run_reviews": [
            passing_acceptance(f"{label}-reviewer", "The revision passed.")
        ],
        "run_publications": [dict(template_data["run_publications"][0])],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    template.unlink()
    return path


def _human_cli(
    repo: Path, fixture: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        repo,
        fixture,
        *arguments,
        machine_output=False,
    )


def _assert_human_status_and_history(
    repo: Path,
    fixture: Path,
    run_id: str,
    *,
    status_term: str,
    next_action: str,
) -> None:
    status = _human_cli(repo, fixture, "status", run_id)
    history = _human_cli(repo, fixture, "history", run_id)

    assert status.returncode == history.returncode == 0
    if status_term == "请批准合并 PR":
        state = load_only_run_state(repo)
        publication = state["parent_job"] if state.get("delivery_type") == "parent_only" else state["run_publication"]
        status_term = f"{status_term} #{publication['pr_number']}"
    assert f"状态:       {status_term}\n" in status.stdout
    assert next_action in status.stdout
    assert next_action in history.stdout
    assert run_id not in status.stdout
    assert run_id not in history.stdout
    assert "<run-id>" not in status.stdout
    assert "<run-id>" not in history.stdout


def test_approve_parent_receipt_is_human_safe_and_repeat_is_idempotent(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_agents(git_repo / "agents.json")
    started = _human_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert started.returncode == 0, started.stdout
    assert "交付状态: 等待人工批准" in started.stdout
    assert "下一步: agent-run approve 1 --repo example/project" in started.stdout
    assert "parent_approval_pending" not in started.stdout
    assert "<run-id>" not in started.stdout
    run_id = str(load_only_run_state(git_repo)["run_id"])
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="请批准合并 PR",
        next_action="agent-run approve 1 --repo example/project",
    )

    first = _human_cli(
        git_repo, fixture, "approve", "1", "--agent-fixture", str(agents)
    )

    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    assert "操作: approve" in first.stdout
    assert "动作状态: 最终交付已完成" in first.stdout
    assert "交付状态: 整个交付已完成" in first.stdout
    assert "下一步: 无" in first.stdout
    assert run_id not in first.stdout
    for machine_label in ("action_id", "payload_digest", "executor_generation", "PID"):
        assert machine_label not in first.stdout
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="任务已完成",
        next_action="下一步: 无",
    )
    control_path = next((managed_state(git_repo) / "task-control").glob("*.json"))
    first_control = json.loads(control_path.read_text(encoding="utf-8"))
    first_action_id = first_control["action"]["action_id"]
    delivery_before = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]

    repeated = run_cli(
        git_repo, fixture, "approve", "1", "--agent-fixture", str(agents)
    )

    assert repeated.returncode == 0, repeated.stdout
    repeated_output = stdout_json(repeated)
    assert repeated_output["action"]["submission"] == "attached"
    assert repeated_output["action"]["status"] == "applied"
    assert repeated_output["action_audit"]["action_id"] == first_action_id
    assert repeated_output["action_audit"]["payload_digest"]
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == delivery_before

    repeated_human = _human_cli(
        git_repo, fixture, "approve", "1", "--agent-fixture", str(agents)
    )

    assert repeated_human.returncode == 0, repeated_human.stdout
    assert "提交结果: 已附着到原操作" in repeated_human.stdout
    assert "Agent: 原操作已收口，无活动 Executor" in repeated_human.stdout
    assert "正在处理" not in repeated_human.stdout
    assert run_id not in repeated_human.stdout


def test_revise_parent_intent_creates_a_successor_at_the_next_human_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    initial_agents = run_agents(git_repo / "initial-agents.json")
    initial = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(initial_agents)
    )
    assert initial.returncode == 0, initial.stdout
    run_id = str(stdout_json(initial)["run_id"])
    agents = _revision_agents(git_repo / "revision-agents.json")

    first = _human_cli(
        git_repo,
        fixture,
        "revise",
        "1",
        "--message",
        "  please preserve this exact revision  ",
        "--agent-fixture",
        str(agents),
    )

    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    assert "操作: revise" in first.stdout
    assert "动作状态: 已应用" in first.stdout
    assert "动作完成不等于整个交付完成" in first.stdout
    assert "交付状态: 等待人工批准" in first.stdout
    assert "下一步: agent-run approve 1 --repo example/project" in first.stdout
    assert "run_approval_pending" not in first.stdout
    assert "<run-id>" not in first.stdout
    assert run_id not in first.stdout
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="请批准合并 PR",
        next_action="agent-run approve 1 --repo example/project",
    )
    state_before = load_only_run_state(git_repo)
    receipt = state_before["action_application_receipt"]
    assert receipt["kind"] == "revise"
    assert state_before["status"] == "run_approval_pending"
    assert state_before["run_acceptance"]["repair_generation"] == 1
    attempts_before = state_before["run_acceptance"]["modification_attempts"]
    invocation_count_before = len(state_before["agent_invocation_history"])
    successor_agents = _revision_agents(git_repo / "successor-revision-agents.json")

    repeated = run_cli(
        git_repo,
        fixture,
        "revise",
        "1",
        "--message",
        "  please preserve this exact revision  ",
        "--agent-fixture",
        str(successor_agents),
    )

    assert repeated.returncode == 0, repeated.stdout
    repeated_output = stdout_json(repeated)
    assert repeated_output["action"]["submission"] == "started"
    assert repeated_output["action_audit"]["action_id"] != receipt["action_id"]
    current = StateStore(managed_state(git_repo)).load_run(run_id)
    assert current is not None
    assert current["status"] == "run_approval_pending"
    assert current["run_acceptance"]["repair_generation"] == 2
    assert current["run_acceptance"]["modification_attempts"] == attempts_before == 1
    assert len(current["agent_invocation_history"]) == invocation_count_before + 4
    control_path = next((managed_state(git_repo) / "task-control").glob("*.json"))
    control = json.loads(control_path.read_text(encoding="utf-8"))
    assert control["action"]["action_id"] == repeated_output["action_audit"]["action_id"]
    assert any(
        item["action"]["action_id"] == receipt["action_id"]
        for item in control["action_history"]
    )


def test_revise_durable_save_crash_is_not_replayed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    initial_agents = run_agents(git_repo / "initial-agents.json")
    initial = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(initial_agents)
    )
    assert initial.returncode == 0, initial.stdout
    agents = _revision_agents(git_repo / "revision-agents.json")

    crashed = run_cli(
        git_repo,
        fixture,
        "revise",
        "1",
        "--message",
        "persist this revision",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "1",
    )

    assert crashed.returncode == 2
    crashed_state = load_only_run_state(git_repo)
    receipt = crashed_state["action_application_receipt"]
    assert receipt["kind"] == "revise"
    assert crashed_state["run_acceptance"]["repair_request"]["human_feedback"] == (
        "persist this revision"
    )
    repair_request = dict(crashed_state["run_acceptance"]["repair_request"])
    receipt_before = dict(receipt)
    repair_generation_before = crashed_state["run_acceptance"].get(
        "repair_generation", 0
    )
    invocation_count_before = len(crashed_state["agent_invocation_history"])
    fixture_before = fixture.read_bytes()
    control_path = next((managed_state(git_repo) / "task-control").glob("*.json"))
    control_before = json.loads(control_path.read_text(encoding="utf-8"))

    repeated = run_cli(
        git_repo,
        fixture,
        "revise",
        "1",
        "--message",
        "persist this revision",
        "--agent-fixture",
        str(agents),
    )

    assert repeated.returncode == 0, f"{repeated.stdout}\n{repeated.stderr}"
    repeated_output = stdout_json(repeated)
    assert repeated_output["action"]["submission"] == "attached"
    assert repeated_output["action"]["status"] == "applied"
    assert repeated_output["action_audit"]["action_id"] == receipt["action_id"]
    assert repeated_output["action_audit"]["payload_digest"] == receipt["payload_digest"]
    current = StateStore(managed_state(git_repo)).load_run(str(crashed_state["run_id"]))
    assert current is not None
    assert current["run_acceptance"]["repair_request"] == repair_request
    assert (
        current["run_acceptance"].get("repair_generation", 0)
        == repair_generation_before
    )
    assert len(current["agent_invocation_history"]) == invocation_count_before
    assert current["action_application_receipt"] == receipt_before
    assert fixture.read_bytes() == fixture_before
    control_after = json.loads(control_path.read_text(encoding="utf-8"))
    assert (
        control_after["action"]["action_id"]
        == control_before["action"]["action_id"]
    )
    assert (
        control_after["action"]["executor_generation"]
        == control_before["action"]["executor_generation"]
    )
    assert control_after["action"]["status"] == "completed"
    assert control_after["action"]["application_observed"] is True
    assert control_after["action_history"] == control_before["action_history"]


def test_run_failure_receipt_recovers_the_durable_parent_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _parent_agents(git_repo / "agents.json")

    crashed = _human_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "1",
    )

    assert crashed.returncode == 2
    state = load_only_run_state(git_repo)
    run_id = str(state["run_id"])
    assert "Repository: example/project" in crashed.stdout
    assert "Parent Issue: #1" in crashed.stdout
    assert "交付状态: 正在初始化" in crashed.stdout
    assert "下一步: agent-run run 1 --repo example/project" in crashed.stdout
    assert run_id not in crashed.stdout
    assert "starting" not in crashed.stdout
    assert "<run-id>" not in crashed.stdout
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="正在初始化",
        next_action="agent-run run 1 --repo example/project",
    )


@pytest.mark.parametrize(
    ("command", "extra_arguments"),
    [
        ("resume", []),
        ("approve", []),
        ("revise", ["--message", "change it"]),
        ("requeue", []),
        ("configure", ["--development-model", "custom"]),
    ],
)
def test_unknown_repository_failure_does_not_use_the_launch_checkout(
    git_repo: Path,
    command: str,
    extra_arguments: list[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert started.returncode == 0, started.stdout
    run_id = str(stdout_json(started)["run_id"])
    state = load_only_run_state(git_repo)
    internal_status = str(state["status"])
    selector = run_id if command == "configure" else "1"

    failed = _human_cli(
        git_repo,
        fixture,
        command,
        selector,
        *extra_arguments,
        "--repo",
        "wrong/project",
    )

    assert failed.returncode == 2
    assert "命令状态: 未执行" in failed.stdout
    assert "没有找到唯一匹配的交付" in failed.stdout
    assert "不会猜测目标" in failed.stdout
    assert load_only_run_state(git_repo) == state
    assert run_id not in failed.stdout
    assert internal_status not in failed.stdout
    assert "<run-id>" not in failed.stdout
    assert "state_dir" not in failed.stdout
    assert "repository_root" not in failed.stdout
    assert str(git_repo) not in failed.stdout
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="请批准合并 PR",
        next_action="agent-run approve 1 --repo example/project",
    )


def test_configure_parent_changes_only_the_profile_control_plane(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = str(stdout_json(seed_run(git_repo, fixture))["run_id"])
    state_root = managed_state(git_repo)
    state_path = state_root / "runs" / f"{run_id}.json"
    profile_path = state_root / "profiles" / f"{run_id}.json"
    profile_before = json.loads(profile_path.read_bytes())
    state_before = state_path.read_bytes()
    task_control = state_root / "task-control"
    control_before = {
        path.name: path.read_bytes() for path in task_control.glob("*.json")
    }
    tree_before = {
        path.relative_to(state_root): None if path.is_dir() else path.read_bytes()
        for path in state_root.rglob("*")
    }

    configured = _human_cli(
        git_repo, fixture, "configure", "1", "--development-model", "custom-model"
    )

    assert configured.returncode == 0, configured.stdout
    assert "配置状态: 已保存" in configured.stdout
    assert run_id not in configured.stdout
    assert state_path.read_bytes() == state_before
    assert {
        path.name: path.read_bytes() for path in task_control.glob("*.json")
    } == control_before
    tree_after = {
        path.relative_to(state_root): None if path.is_dir() else path.read_bytes()
        for path in state_root.rglob("*")
    }
    profile_relative = profile_path.relative_to(state_root)
    assert tree_after.keys() == tree_before.keys()
    assert {
        path: contents
        for path, contents in tree_after.items()
        if path != profile_relative
    } == {
        path: contents
        for path, contents in tree_before.items()
        if path != profile_relative
    }
    profile = json.loads(profile_path.read_bytes())
    assert profile["profile_revision"] == profile_before["profile_revision"] + 1
    assert len(profile["revisions"]) == len(profile_before["revisions"]) + 1
    assert profile["revisions"][-1]["profile_revision"] == profile[
        "profile_revision"
    ]
    assert profile["profiles"]["development"]["model"] == "custom-model"


def test_default_precondition_failure_hides_machine_run_identity(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert started.returncode == 0, started.stdout
    run_id = str(stdout_json(started)["run_id"])

    human = _human_cli(git_repo, fixture, "requeue", run_id)

    assert human.returncode == 2
    assert "命令状态: 未应用" in human.stdout
    assert "Parent Issue: #1" in human.stdout
    assert "交付状态: 等待人工批准" in human.stdout
    assert "下一步: agent-run approve 1 --repo example/project" in human.stdout
    assert "run_approval_pending" not in human.stdout
    assert "<run-id>" not in human.stdout
    assert run_id not in human.stdout
    assert "run_id" not in human.stdout
    assert "run_branch" not in human.stdout
    _assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        status_term="请批准合并 PR",
        next_action="agent-run approve 1 --repo example/project",
    )

    machine = run_cli(git_repo, fixture, "requeue", run_id)
    assert machine.returncode == 2
    assert stdout_json(machine)["run_id"] == run_id


def test_default_interruption_output_hides_machine_run_identity(capsys) -> None:
    run_id = "run-1-machine-only"
    state = {
        "run_id": run_id,
        "repository": "example/project",
        "parent": {"number": 1},
        "status": "waiting_external",
    }
    output = {
        "status": "waiting_external",
        "diagnostics": [
            {
                "code": "observation_interrupted",
                "message": "CLI 未改写 Delivery Run",
            }
        ],
        "next_action": f"agent-run resume {run_id}",
    }

    _print_interruption_result(state, output)

    rendered = capsys.readouterr().out
    assert "操作状态: 已中断" in rendered
    assert "Parent Issue: #1" in rendered
    assert "交付状态: 等待 GitHub 操作结果" in rendered
    assert "下一步: agent-run run 1 --repo example/project" in rendered
    assert "waiting_external" not in rendered
    assert "<run-id>" not in rendered
    assert run_id not in rendered


@pytest.mark.parametrize("command", ["approve", "revise", "requeue", "configure"])
def test_parent_mutation_selector_never_guesses_a_missing_run(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    arguments = [command, "1"]
    if command == "revise":
        arguments.extend(["--message", "change it"])
    elif command == "configure":
        arguments.extend(["--development-model", "custom"])

    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["diagnostics"][0]["code"] == "run_selector_not_found"
    assert not (managed_state(git_repo) / "runs").exists()


def test_parent_mutation_selector_reports_missing_repository_and_ambiguity(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    first = seed_run(git_repo, fixture, reuse_existing=False)
    second = seed_run(git_repo, fixture, reuse_existing=False)
    first_run_id = str(stdout_json(first)["run_id"])
    second_run_id = str(stdout_json(second)["run_id"])
    assert first_run_id != second_run_id

    mismatch = run_cli(
        git_repo,
        fixture,
        "configure",
        "1",
        "--development-model",
        "custom",
        "--repo",
        "other/project",
    )
    assert mismatch.returncode == 2
    assert stdout_json(mismatch)["diagnostics"][0]["code"] == (
        "run_selector_not_found"
    )

    ambiguous = run_cli(
        git_repo, fixture, "configure", "1", "--development-model", "custom"
    )
    assert ambiguous.returncode == 2
    assert stdout_json(ambiguous)["diagnostics"][0]["code"] == (
        "run_selector_ambiguous"
    )

    human_ambiguous = _human_cli(
        git_repo, fixture, "configure", "1", "--development-model", "custom"
    )
    assert human_ambiguous.returncode == 2
    assert "命令状态: 未执行" in human_ambiguous.stdout
    assert "候选交付:" in human_ambiguous.stdout
    assert first_run_id not in human_ambiguous.stdout
    assert second_run_id not in human_ambiguous.stdout
    assert str(git_repo) not in human_ambiguous.stdout
    assert "state_dir" not in human_ambiguous.stdout
    assert "repository_root" not in human_ambiguous.stdout
    assert len(list((managed_state(git_repo) / "runs").glob("*.json"))) == 2
