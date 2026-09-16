from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent_run.agent_profiles import AgentProfileStore
from agent_run.cli_presentation import _next_action
from agent_run.controller import Controller
from agent_run.delivery_policy import (
    DeliveryPolicyStore,
    resolve_delivery_policy,
)
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore
from agent_run.task_control import TASK_CONTROL_PROTOCOL, TaskControlStore, TaskKey
from support.workspace import prepare_workspace


_GIT_LOCATION_VARIABLES = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_TEMPLATE_DIR",
}


def _inherited_git_override(variable: str) -> bool:
    return variable in _GIT_LOCATION_VARIABLES or variable.startswith("GIT_CONFIG")


def _user_environment(root: Path) -> dict[str, str]:
    python_directory = str(Path(sys.executable).parent)
    inherited_path = os.environ.get("PATH", os.defpath)
    environment = {
        "PATH": os.pathsep.join(
            [python_directory]
            + [entry for entry in inherited_path.split(os.pathsep) if entry != python_directory]
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for variable, directory in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        path = root / directory
        path.mkdir(parents=True, mode=0o700)
        environment[variable] = str(path)
    (Path(environment["HOME"]) / ".gitconfig").write_text(
        "[user]\n\tname = Agent Run Tests\n\temail = agent-run-tests@example.invalid\n",
        encoding="utf-8",
    )
    return environment


def _apply_test_environment(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if _inherited_git_override(variable):
            monkeypatch.delenv(variable)
    for variable, value in _user_environment(root).items():
        monkeypatch.setenv(variable, value)


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Helpers and CLI children share one private user environment per case."""

    _apply_test_environment(tmp_path / "user", monkeypatch)


@pytest.fixture(scope="session")
def git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once per worker; tests only receive independent copies."""

    root = tmp_path_factory.mktemp("git-template")
    environment = {
        key: value for key, value in os.environ.items() if not _inherited_git_override(key)
    } | _user_environment(root / "user")
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=environment, check=True, capture_output=True)
    # Automatic maintenance may outlive commit and mutate this shared template
    # during copytree. Disable dispatch before the first commit; copies inherit it.
    subprocess.run(
        ["git", "config", "--local", "maintenance.auto", "false"],
        cwd=repo,
        env=environment,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Agent Run Tests"], cwd=repo, env=environment, check=True)
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=repo,
        env=environment,
        check=True,
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = [\n'
        '  "fixture-ci::fixture-required-check::Run tests",\n'
        '  "CI::quality::Run tests",\n'
        ']\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "pyproject.toml"], cwd=repo, env=environment, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, env=environment, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo(tmp_path: Path, git_template: Path) -> Path:
    return Path(shutil.copytree(git_template, tmp_path / "repo"))


def write_fixture(path: Path, *, issues: dict[str, Any], **overrides: Any) -> Path:
    default_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path.parent,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    fixture: dict[str, Any] = {
        "repository": "example/project",
        "default_branch": "main",
        "default_head_sha": default_head,
        "parent": {
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [int(number) for number in issues],
            "sub_issue_order_reliable": True,
        },
        "issues": issues,
    }
    fixture.update(overrides)
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


def seed_run(
    repo: Path,
    fixture: Path,
    parent: str = "1",
    *options: str,
    extra_env: dict[str, str] | None = None,
    reuse_existing: bool = True,
    idle_control: bool = False,
) -> subprocess.CompletedProcess[str]:
    with patch.dict(os.environ, extra_env or {}):
        return _seed_run(
            repo, fixture, parent, *options,
            reuse_existing=reuse_existing, idle_control=idle_control,
        )


def _seed_run(
    repo: Path,
    fixture: Path,
    parent: str = "1",
    *options: str,
    reuse_existing: bool = True,
    idle_control: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Create test state through Controller.start without a public CLI command.

    The process-shaped result keeps older state-oriented tests focused on their
    original subject while the public ``start`` command is removed.  Supported
    options are deliberately limited to the legacy fixture setup knobs still
    needed by those tests; this is not a CLI compatibility parser.
    Explicit idle_control records that this direct setup reserved no Executor.
    """

    values = list(options)

    def take_option(name: str) -> str | None:
        if name not in values:
            return None
        index = values.index(name)
        try:
            value = values[index + 1]
        except IndexError as error:
            raise AssertionError(f"seed_run option {name} requires a value") from error
        del values[index : index + 2]
        return value

    state_dir_value = take_option("--state-dir")
    # Repository selection belongs to the public selector tests.  Fixture
    # readers already carry the exact repository identity used for seeding.
    take_option("--repo")

    policy_overrides: dict[str, Any] = {}
    for option, key in (
        ("--ticket-review-rounds", "ticket_review_rounds"),
        ("--parent-only-paired-rounds", "parent_only_paired_rounds"),
        ("--run-repair-rounds", "run_repair_rounds"),
    ):
        supplied = take_option(option)
        if supplied is not None:
            policy_overrides[key] = int(supplied)
    deadlines: dict[str, str] = {}
    for role in ("development", "review", "publication"):
        supplied = take_option(f"--{role}-deadline")
        if supplied is not None:
            deadlines[role] = supplied
    if deadlines:
        policy_overrides["invocation_deadlines"] = deadlines

    profile_overrides: dict[str, str | bool | None] = {}
    for role in ("development", "review", "publication"):
        for suffix in ("model", "effort"):
            supplied = take_option(f"--{role}-{suffix}")
            if supplied is not None:
                profile_overrides[f"{role}_{suffix}"] = supplied
    profile_preset = take_option("--profile-preset")
    if "--publication-from-development" in values:
        values.remove("--publication-from-development")
        profile_overrides["publication_from_development"] = True

    if values:
        raise AssertionError(f"unsupported seed_run options: {values!r}")

    reader = FixtureGitHubReader(fixture)
    workspace = prepare_workspace(repo, repository=reader.repository_hint())
    managed_git = workspace.open()
    state_root = (
        Path(state_dir_value).resolve()
        if state_dir_value is not None
        else workspace.state_root
    )
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser().resolve()
    policy_store = DeliveryPolicyStore(
        config_home / "agent-run" / "delivery-policy.json"
    )
    policy = resolve_delivery_policy(
        user_defaults=policy_store.load(),
        command_overrides=policy_overrides,
    )
    profiles = AgentProfileStore(state_root)
    controller = Controller(
        reader,
        managed_git,
        StateStore(state_root),
        locator=RunLocatorIndex.default(),
        profiles=profiles,
        delivery_policy=policy,
    )
    state, resumed = controller.start(int(parent), reuse_existing=reuse_existing)
    run_id = str(state["run_id"])
    if idle_control and not resumed:
        seed_idle_control(
            TaskControlStore(workspace.state_root),
            TaskKey(managed_git.root, str(state["repository"]), int(parent)),
            run_id,
            state_dir=state_root,
        )
    if profiles.load(run_id) is None:
        profiles.initialize(
            run_id,
            preset=profile_preset,
            overrides=profile_overrides,
        )
    active_ticket = state.get("active_ticket_job")
    output = {
        "result": "resumed" if resumed else "started",
        "run_id": run_id,
        "status": state["status"],
        "run_branch": state.get("run_branch", state.get("parent_branch")),
        "active_ticket": (
            active_ticket.get("ticket_number")
            if isinstance(active_ticket, dict)
            else None
        ),
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
        "next_action": _next_action(state),
    }
    successful_statuses = {
        "active",
        "ticket_completed",
        "waiting_checks",
        "parent_delivery_pending",
        "parent_approval_pending",
        "parent_closeout_pending",
        "publication_pending",
        "run_acceptance_pending",
        "run_publication_pending",
        "run_approval_pending",
        "completed",
        "abandoned",
        "waiting_merge",
        "waiting_external",
    }
    return subprocess.CompletedProcess(
        args=["test-seed-run", parent, *options],
        returncode=0 if state["status"] in successful_statuses else 2,
        stdout=json.dumps(output, ensure_ascii=False, sort_keys=True),
        stderr="",
    )


def seed_idle_control(
    control: TaskControlStore,
    task: TaskKey,
    run_id: str,
    *,
    state_dir: Path | None = None,
) -> None:
    """Record known idle ownership for a Run created directly by test setup.

    Use only before any Executor is reserved. Missing-ownership fault fixtures
    deliberately keep using bare seed_run, without this explicit evidence.
    """
    path = control.path_for(task)
    assert not path.exists(), "idle fixture must not replace existing ownership"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "protocol": TASK_CONTROL_PROTOCOL,
                "task": task.identity,
                "run_id": run_id,
                "run_state_dir": (
                    str(state_dir.resolve()) if state_dir is not None else None
                ),
                "next_generation": 1,
                "action": None,
                "action_history": [],
                "executor": None,
                "updated_at": "2026-09-07T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    assert control.load(task) is not None
