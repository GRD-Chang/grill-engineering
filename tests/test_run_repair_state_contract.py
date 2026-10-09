from __future__ import annotations

from support.run_artifacts import _canonical_run_budget

from copy import deepcopy
import json
import shutil
from pathlib import Path
import subprocess
from typing import Any

import pytest

from support.workspace import managed_repo, managed_state

from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_current_run_state,
)
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from support.environment import _apply_test_environment
from support.seeding import seed_run, write_fixture
from support.cli import issue, load_only_run_state, run_cli, stdout_json


def _with_canonical_ci_budget(state: dict[str, Any]) -> dict[str, Any]:
    """Keep these repair-mode fixtures valid under the independent CI contract."""

    run = state.get("run_acceptance")
    if isinstance(run, dict):
        run.setdefault("ci_recovery", new_ci_recovery_budget())
        run.setdefault("review_budget", _canonical_run_budget())
        run.setdefault("review_budget_history", [])
    return state


def _require_repair_state(state: dict[str, Any]) -> None:
    require_current_run_state(_with_canonical_ci_budget(state))


@pytest.fixture(scope="module")
def _valid_run_state_template(
    tmp_path_factory: pytest.TempPathFactory, git_template: Path
) -> dict[str, Any]:
    """Prepare canonical persisted state once, isolated from every test's user."""

    root = tmp_path_factory.mktemp("repair-state-template")
    repo = Path(shutil.copytree(git_template, root / "repo"))
    with pytest.MonkeyPatch.context() as environment:
        _apply_test_environment(root / "user", environment)
        fixture = write_fixture(repo / "github.json", issues={"2": issue(2)})
        seed_run(repo, fixture, "1")
        state = load_only_run_state(repo)
        _require_repair_state(state)
    return state


@pytest.fixture
def valid_run_state(_valid_run_state_template: dict[str, Any]) -> dict[str, Any]:
    # Nested job/budget mutations must never leak into the next parameter case.
    return deepcopy(_valid_run_state_template)


def _git_refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("phase", ["developing", "escalating", "accepted"])
@pytest.mark.parametrize("repair_mode", [None, "legacy", 1])
def test_resume_rejects_noncanonical_run_repair_mode_before_mutation(
    git_repo: Path, repair_mode: object, phase: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    repair_job: dict[str, object] = {
        "phase": phase,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
    }
    if repair_mode is not None:
        repair_job["repair_mode"] = repair_mode
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": repair_job,
    }
    state_path = next((managed_state(git_repo) / "runs").glob("*.json"))
    _with_canonical_ci_budget(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = deepcopy(state)
    fixture_before = fixture.read_text(encoding="utf-8")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_repo(git_repo),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "recovery_materials_missing"
    assert json.loads(state_path.read_text(encoding="utf-8")) == state_before
    assert fixture.read_text(encoding="utf-8") == fixture_before
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_repo(git_repo),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == head_before


@pytest.mark.parametrize("repair_mode", ["squash", "merge_resolution"])
def test_canonical_run_repair_modes_pass_state_validation(
    valid_run_state: dict[str, Any], repair_mode: str
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": repair_mode,
        },
    }

    _require_repair_state(state)


@pytest.mark.parametrize("missing", ["run_acceptance", "repair_job"])
def test_run_repair_snapshot_is_required_for_materialized_projections(
    valid_run_state: dict[str, Any], missing: str
) -> None:
    state = valid_run_state
    run_snapshot = deepcopy(state["policy_snapshot"])
    repair_snapshot = deepcopy(run_snapshot)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": run_snapshot,
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": repair_snapshot,
            "repair_mode": "squash",
        },
    }
    if missing == "run_acceptance":
        del state["run_acceptance"]["policy_snapshot"]
    else:
        del state["run_acceptance"]["repair_job"]["policy_snapshot"]

    with pytest.raises(IncompatibleRunStateError, match="invalid .*policy_snapshot"):
        _require_repair_state(state)


def test_old_run_policy_snapshot_is_incompatible_without_inference(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    old_snapshot = deepcopy(state["policy_snapshot"])
    old_snapshot.pop("run_repair_rounds")
    state["policy_snapshot"] = old_snapshot

    with pytest.raises(IncompatibleRunStateError, match="invalid Policy Snapshot"):
        _require_repair_state(state)


def test_old_run_review_history_without_policy_snapshot_is_incompatible(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["active_ticket_job"] = None
    state["run_acceptance"] = {
        "phase": "blocked",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": {
            **_canonical_run_budget(),
            "window": 2,
        },
        "review_budget_history": [
            {
                "phase": "blocked",
                "modification_attempts": 0,
                "validation_attempts": 0,
                "review_budget": _canonical_run_budget(),
            }
        ],
    }

    with pytest.raises(
        IncompatibleRunStateError, match="invalid canonical run_acceptance.review_budget"
    ):
        _require_repair_state(state)


@pytest.mark.parametrize("repair_window", [1, 2])
def test_run_repair_rejects_duplicate_budget_owners(
    valid_run_state: dict[str, Any], repair_window: int,
) -> None:
    state = valid_run_state
    run_budget = _canonical_run_budget()
    repair_budget = _canonical_run_budget()
    repair_budget["window"] = repair_window
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": run_budget,
        "review_budget_history": [],
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "review_budget": repair_budget,
            "review_budget_history": [],
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="must not persist delivery budgets"):
        _require_repair_state(state)


def test_run_repair_cannot_use_ticket_fallback_authority(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "fallback_publication_receipt": {},
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="fallback Publication Authority"):
        _require_repair_state(state)


def test_direct_state_validation_rejects_missing_active_run_repair_mode(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": "candidate",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="repair_mode"):
        _require_repair_state(state)


def test_final_run_pr_requires_an_exact_head_observation(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["run_publication"] = {
        "phase": "waiting_checks",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "pr_number": 17,
        "publication_sha": "final-head",
    }

    with pytest.raises(IncompatibleRunStateError, match="required_checks_evidence"):
        _require_repair_state(state)


def test_legacy_projection_conflict_is_rejected_by_state_validation(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["run_publication"] = {
        "phase": "waiting_checks",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "pr_number": 17,
        "publication_sha": "final-head",
        "required_checks": "fail",
        "required_checks_mode": "configured",
        "required_checks_evidence": {
            "pr_number": 17,
            "head_sha": "final-head",
            "result": "pass",
            "checks": [{"name": "quality", "bucket": "pass"}],
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="conflicts with Observation"):
        _require_repair_state(state)


@pytest.mark.parametrize(
    ("result", "checks"),
    [
        ("pass", []),
        ("pass", [{"name": "quality", "bucket": "fail"}]),
        ("none", [{"name": "quality", "bucket": "pass"}]),
        ("pending", [{"name": "quality", "bucket": "pass"}]),
        ("fail", [{"name": "quality", "bucket": "pending"}]),
        ("unknown", []),
    ],
)
def test_state_contract_rejects_required_checks_result_bucket_contradictions(
    valid_run_state: dict[str, Any], result: str, checks: list[dict[str, str]]
) -> None:
    state = valid_run_state
    state["run_publication"] = {
        "phase": "waiting_checks",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "pr_number": 17,
        "publication_sha": "final-head",
        "required_checks_evidence": {
            "pr_number": 17,
            "head_sha": "final-head",
            "result": result,
            "checks": checks,
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="result conflicts"):
        _require_repair_state(state)


def test_fallback_receipt_observation_without_job_observation_is_incompatible(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    job = state["ticket_jobs"]["2"]
    job.update(
        {
            "phase": "candidate",
            "fallback_publication_receipt": {
                "pr_number": 17,
                "publication_sha": "candidate-head",
                "required_checks_evidence": {
                    "pr_number": 17,
                    "head_sha": "candidate-head",
                    "result": "fail",
                    "checks": [],
                },
            },
        },
    )
    state["active_ticket_job"] = deepcopy(job)

    with pytest.raises(IncompatibleRunStateError, match="without the Job Observation"):
        _require_repair_state(state)


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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": phase,
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "integrated_revalidation_merge": marker,
        },
    }
    state_path = next((managed_state(git_repo) / "runs").glob("*.json"))
    _with_canonical_ci_budget(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = deepcopy(state)
    fixture_before = fixture.read_text(encoding="utf-8")
    refs_before = _git_refs(managed_repo(git_repo))

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "recovery_materials_missing"
    assert json.loads(state_path.read_text(encoding="utf-8")) == state_before
    assert fixture.read_text(encoding="utf-8") == fixture_before
    assert _git_refs(managed_repo(git_repo)) == refs_before


def test_retired_integrated_revalidation_merge_is_rejected(
    valid_run_state: dict[str, Any],
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": "candidate",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="retired internal PR lifecycle"):
        _require_repair_state(state)


@pytest.mark.parametrize("phase", ["candidate", "accepted", "developing"])
def test_retired_merge_marker_is_rejected_in_current_repair_phases(
    valid_run_state: dict[str, Any], phase: str,
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": phase,
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="retired internal PR lifecycle"):
        _require_repair_state(state)


def test_resume_rejects_previous_budget_protocol_without_changing_materials(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    assert state["review_budget_protocol"] == 2
    state["review_budget_protocol"] = 1
    state_path = next((managed_state(git_repo) / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    refs_before = _git_refs(managed_repo(git_repo))

    rejected = run_cli(git_repo, fixture, "resume", run_id)

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert state_path.read_bytes() == before
    assert fixture.read_bytes() == fixture_before
    assert _git_refs(managed_repo(git_repo)) == refs_before


@pytest.mark.parametrize("phase", [
    "publishing", "publication_pending", "waiting_checks", "waiting_merge",
    "merging", "merged",
])
def test_parent_repair_rejects_retired_internal_pr_phase(
    valid_run_state: dict[str, Any], phase: str,
) -> None:
    state = valid_run_state
    state["run_acceptance"] = {
        "phase": "repairing", "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "repair_job": {
            "phase": phase, "repair_mode": "squash",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
        },
    }
    with pytest.raises(IncompatibleRunStateError, match="retired internal PR phase"):
        _require_repair_state(state)
