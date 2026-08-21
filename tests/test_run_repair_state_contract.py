from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_current_run_state,
)
from conftest import write_fixture
from test_cli import issue, load_only_run_state, run_cli, stdout_json


def _git_refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("phase", ["developing", "escalating", "merged"])
@pytest.mark.parametrize("repair_mode", [None, "legacy", 1])
def test_resume_rejects_noncanonical_run_repair_mode_before_mutation(
    git_repo: Path, repair_mode: object, phase: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    repair_job: dict[str, object] = {"phase": phase}
    if repair_mode is not None:
        repair_job["repair_mode"] = repair_mode
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": repair_job,
    }
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = deepcopy(state)
    fixture_before = fixture.read_text(encoding="utf-8")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == state_before
    assert fixture.read_text(encoding="utf-8") == fixture_before
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == head_before


@pytest.mark.parametrize("repair_mode", ["squash", "merge_resolution"])
def test_canonical_run_repair_modes_pass_state_validation(
    git_repo: Path, repair_mode: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": {"phase": "developing", "repair_mode": repair_mode},
    }

    require_current_run_state(state)


def test_direct_state_validation_rejects_missing_active_run_repair_mode(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": {"phase": "candidate"},
    }

    with pytest.raises(IncompatibleRunStateError, match="repair_mode"):
        require_current_run_state(state)


def _integrated_revalidation_merge() -> dict[str, str]:
    return {
        "base_sha": "base-sha",
        "default_base_sha": "default-sha",
        "candidate_sha": "candidate-sha",
        "publication_sha": "publication-sha",
    }


@pytest.mark.parametrize(
    ("phase", "marker"),
    [
        ("candidate", None),
        ("candidate", {"base_sha": "base-sha"}),
        (
            "candidate",
            {
                "base_sha": "base-sha",
                "default_base_sha": "default-sha",
                "candidate_sha": 1,
                "publication_sha": "publication-sha",
            },
        ),
        ("candidate", {**_integrated_revalidation_merge(), "unexpected": "field"}),
        *(
            (phase, _integrated_revalidation_merge())
            for phase in (
                "publishing",
                "publication_pending",
                "waiting_checks",
                "waiting_merge",
                "merging",
            )
        ),
    ],
    ids=[
        "not_object",
        "missing_field",
        "non_string_sha",
        "unknown_field",
        "publishing_phase",
        "publication_pending_phase",
        "waiting_checks_phase",
        "waiting_merge_phase",
        "merging_phase",
    ],
)
def test_resume_rejects_invalid_integrated_revalidation_merge_before_mutation(
    git_repo: Path, phase: str, marker: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": {
            "phase": phase,
            "repair_mode": "squash",
            "integrated_revalidation_merge": marker,
        },
    }
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = deepcopy(state)
    fixture_before = fixture.read_text(encoding="utf-8")
    refs_before = _git_refs(git_repo)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == state_before
    assert fixture.read_text(encoding="utf-8") == fixture_before
    assert _git_refs(git_repo) == refs_before


def test_canonical_integrated_revalidation_merge_passes_state_validation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": {
            "phase": "candidate",
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    require_current_run_state(state)


def test_integrated_revalidation_merge_rejects_a_non_lifecycle_phase(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "repair_job": {
            "phase": "completed",
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="phase"):
        require_current_run_state(state)
