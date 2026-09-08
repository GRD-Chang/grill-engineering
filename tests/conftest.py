from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_profiles import AgentProfileStore
from agent_run.cli_presentation import _next_action
from agent_run.controller import Controller
from agent_run.delivery_policy import (
    DeliveryPolicyStore,
    resolve_delivery_policy,
)
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Agent Run Tests"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=repo,
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
    subprocess.run(["git", "add", "README.md", "pyproject.toml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


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
) -> subprocess.CompletedProcess[str]:
    """Create test state through Controller.start without a public CLI command.

    The process-shaped result keeps older state-oriented tests focused on their
    original subject while the public ``start`` command is removed.  Supported
    options are deliberately limited to the legacy fixture setup knobs still
    needed by those tests; this is not a CLI compatibility parser.
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

    state_root = (
        Path(state_dir_value).resolve()
        if state_dir_value is not None
        else repo / ".agent-run"
    )
    environment = extra_env or {}
    state_home = Path(
        environment.get(
            "XDG_STATE_HOME",
            os.environ.get("XDG_STATE_HOME", repo / ".agent-run-test-state"),
        )
    ).expanduser().resolve()
    config_home = Path(
        environment.get(
            "XDG_CONFIG_HOME",
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"),
        )
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
        FixtureGitHubReader(fixture),
        GitRepository(repo),
        StateStore(state_root),
        locator=RunLocatorIndex(state_home / "agent-run" / "run-locator.json"),
        profiles=profiles,
        delivery_policy=policy,
    )
    state, resumed = controller.start(int(parent), reuse_existing=reuse_existing)
    run_id = str(state["run_id"])
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
