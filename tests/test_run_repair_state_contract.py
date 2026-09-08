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
from conftest import seed_run, write_fixture
from test_cli import issue, load_only_run_state, run_cli, stdout_json
from run_acceptance_test_support import _canonical_run_budget


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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    repair_job: dict[str, object] = {
        "phase": phase,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
    }
    if repair_mode is not None:
        repair_job["repair_mode"] = repair_mode
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
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
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_mode": repair_mode,
        },
    }

    require_current_run_state(state)


@pytest.mark.parametrize("missing", ["run_acceptance", "repair_job"])
def test_run_repair_snapshot_is_required_for_materialized_projections(
    git_repo: Path, missing: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    run_snapshot = deepcopy(state["policy_snapshot"])
    repair_snapshot = deepcopy(run_snapshot)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": run_snapshot,
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": repair_snapshot,
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_mode": "squash",
        },
    }
    if missing == "run_acceptance":
        del state["run_acceptance"]["policy_snapshot"]
    else:
        del state["run_acceptance"]["repair_job"]["policy_snapshot"]

    with pytest.raises(IncompatibleRunStateError, match="invalid .*policy_snapshot"):
        require_current_run_state(state)


def test_old_run_policy_snapshot_is_incompatible_without_inference(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    old_snapshot = deepcopy(state["policy_snapshot"])
    old_snapshot.pop("run_repair_rounds")
    state["policy_snapshot"] = old_snapshot

    with pytest.raises(IncompatibleRunStateError, match="invalid Policy Snapshot"):
        require_current_run_state(state)


def test_old_run_review_history_without_policy_snapshot_is_incompatible(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
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
        require_current_run_state(state)


def test_run_repair_budget_window_must_match_run_acceptance(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    run_budget = _canonical_run_budget()
    repair_budget = _canonical_run_budget()
    repair_budget["window"] = 2
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

    with pytest.raises(IncompatibleRunStateError, match="Budget Windows"):
        require_current_run_state(state)


def test_run_repair_cannot_use_ticket_fallback_authority(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "fallback_publication_receipt": {},
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="fallback Publication Authority"):
        require_current_run_state(state)


def test_direct_state_validation_rejects_missing_active_run_repair_mode(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "candidate",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="repair_mode"):
        require_current_run_state(state)


def test_waiting_run_repair_requires_an_exact_head_observation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "waiting_checks",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "pr_number": 17,
            "publication_sha": "repair-head",
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="required_checks_evidence"):
        require_current_run_state(state)


def test_legacy_projection_conflict_is_rejected_by_state_validation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "developing",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "required_checks": "fail",
            "required_checks_mode": "configured",
            "required_checks_evidence": {
                "pr_number": 17,
                "head_sha": "repair-head",
                "result": "pass",
                "checks": [{"name": "quality", "bucket": "pass"}],
            },
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="conflicts with Observation"):
        require_current_run_state(state)


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
    git_repo: Path, result: str, checks: list[dict[str, str]]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "waiting_checks",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "repair_mode": "squash",
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "pr_number": 17,
            "publication_sha": "repair-head",
            "required_checks_evidence": {
                "pr_number": 17,
                "head_sha": "repair-head",
                "result": result,
                "checks": checks,
            },
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="result conflicts"):
        require_current_run_state(state)


def test_fallback_receipt_observation_without_job_observation_is_incompatible(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["active_ticket_job"] = {
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
    }

    with pytest.raises(IncompatibleRunStateError, match="without the Job Observation"):
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": phase,
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
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
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "candidate",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    require_current_run_state(state)


def test_integrated_revalidation_merge_rejects_a_non_lifecycle_phase(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    seed_run(git_repo, fixture, "1")
    state = load_only_run_state(git_repo)
    state["run_acceptance"] = {
        "phase": "repairing",
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": _canonical_run_budget(),
        "review_budget_history": [],
        "repair_job": {
            "phase": "completed",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": _canonical_run_budget(),
            "review_budget_history": [],
            "repair_mode": "squash",
            "integrated_revalidation_merge": _integrated_revalidation_merge(),
        },
    }

    with pytest.raises(IncompatibleRunStateError, match="phase"):
        require_current_run_state(state)
