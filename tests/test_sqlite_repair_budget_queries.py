"""当前 Repair 只查询准确代次，保存不删除未读取的旧额度。"""

from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from agent_run.delivery_policy import DeliveryPolicy
from agent_run.sqlite_change_jobs import projection_archives
from agent_run.sqlite_state import SQLiteStateRepository


RUN_ID = "repair-budget-query"


def _repository(tmp_path: Path, *, current: int | None = 2001) -> SQLiteStateRepository:
    repository = SQLiteStateRepository(tmp_path)
    repository.save(RUN_ID, {
        "run_id": RUN_ID,
        "run_acceptance": {
            "phase": "repairing",
            "repair_job": {"phase": "developing", "repair_generation": current} if current else None,
            "repair_integration_budgets": {
                str(generation): {"policy_snapshot": {"revision": str(generation)}}
                for generation in (1, 2, 2001)
            },
        },
    })
    return repository


def _save(repository: SQLiteStateRepository, state: dict) -> None:
    repository.save_objects(RUN_ID, state, fields={"run_acceptance"}, archive_scope=projection_archives(state))


def test_current_repair_budget_query_and_save_leave_old_generations_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import sqlite_change_job_queries as queries

    repository = _repository(tmp_path)
    original = deepcopy(repository.load(RUN_ID)["run_acceptance"]["repair_integration_budgets"])
    load_owner = queries._load_owner
    decoded = []

    def exact_budget(connection, run_id, job_id, **kwargs):
        if ":repair-budget:" in job_id:
            decoded.append(job_id)
            assert job_id.endswith(":2001"), "current save decoded an older Repair budget"
        return load_owner(connection, run_id, job_id, **kwargs)

    with monkeypatch.context() as bounded:
        bounded.setattr(queries, "_load_owner", exact_budget)
        state = repository.load_objects(RUN_ID)[0]
        budgets = state["run_acceptance"]["repair_integration_budgets"]
        assert set(budgets) == {"2001"}
        budgets["2001"]["policy_snapshot"]["revision"] = "current-update"
        _save(repository, state)

    assert decoded
    reopened = SQLiteStateRepository(tmp_path).load(RUN_ID)["run_acceptance"]["repair_integration_budgets"]
    assert reopened["1"] == original["1"]
    assert reopened["2"] == original["2"]
    assert reopened["2001"]["policy_snapshot"]["revision"] == "current-update"


def test_new_repair_generation_preserves_previous_budgets_and_repeated_current_save(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = deepcopy(repository.load(RUN_ID)["run_acceptance"]["repair_integration_budgets"])
    state = repository.load_objects(RUN_ID)[0]
    acceptance = state["run_acceptance"]
    acceptance["repair_job"] = {"phase": "developing", "repair_generation": 2002}
    acceptance["repair_integration_budgets"]["2002"] = {"policy_snapshot": {"revision": "new"}}
    _save(repository, state)
    assert set(state["run_acceptance"]["repair_integration_budgets"]) == {"2002"}
    state["run_acceptance"]["repair_job"]["phase"] = "candidate"
    _save(repository, state)
    reopened = repository.load(RUN_ID)["run_acceptance"]
    assert reopened["repair_job"]["phase"] == "candidate"
    assert reopened["repair_integration_budgets"] == {**original, "2002": {"policy_snapshot": {"revision": "new"}}}


def test_without_current_repair_budgets_stay_unread_until_explicit_history_query(tmp_path: Path) -> None:
    repository = _repository(tmp_path, current=None)
    state = repository.load_objects(RUN_ID)[0]
    assert state["run_acceptance"]["repair_integration_budgets"] == {}
    state["run_acceptance"]["phase"] = "reviewing"
    _save(repository, state)
    assert set(repository.load(RUN_ID)["run_acceptance"]["repair_integration_budgets"]) == {"1", "2", "2001"}


@pytest.mark.parametrize("removal", ["field", "generation", "plain_budgets"])
def test_partial_budget_removal_cannot_erase_previous_usage(tmp_path: Path, removal: str) -> None:
    repository = _repository(tmp_path)
    original = repository.load(RUN_ID)
    state = repository.load_objects(RUN_ID)[0]
    acceptance = state["run_acceptance"]
    if removal == "field":
        del acceptance["repair_integration_budgets"]
    elif removal == "generation":
        acceptance["repair_integration_budgets"].clear()
    else:
        acceptance["repair_integration_budgets"] = dict(acceptance["repair_integration_budgets"])
    with pytest.raises(ValueError, match="Cannot remove recorded Repair"):
        _save(repository, state)
    assert repository.load(RUN_ID) == original


def test_partial_budget_cannot_overwrite_unread_generation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = repository.load(RUN_ID)
    state = repository.load_objects(RUN_ID)[0]
    state["run_acceptance"]["repair_integration_budgets"]["1"] = {"policy_snapshot": {"revision": "replacement"}}
    with pytest.raises(ValueError, match="Cannot overwrite unread Repair"):
        _save(repository, state)
    assert repository.load(RUN_ID) == original


def test_scope_survives_copy_and_sanitization_but_cannot_move_to_another_run(tmp_path: Path) -> None:
    from agent_run.state import _sanitize_durable_errors
    from agent_run.work_projection import RepairBudgetProjection, restore_work_scopes

    repository = _repository(tmp_path)
    state = repository.load_objects(RUN_ID)[0]
    copied = deepcopy(state)
    durable = _sanitize_durable_errors(copied)
    restore_work_scopes(durable, copied, preserve_owner_references=True)
    budgets = durable["run_acceptance"]["repair_integration_budgets"]
    assert isinstance(budgets, RepairBudgetProjection)
    assert budgets.scope == state["run_acceptance"]["repair_integration_budgets"].scope
    durable["run_acceptance"]["phase"] = "reviewing"
    _save(repository, durable)
    other_id = "another-run"
    repository.save(other_id, {"run_id": other_id, "run_acceptance": {"repair_integration_budgets": {}}})
    with pytest.raises(ValueError, match="Repair budget scope belongs to another owner"):
        repository.save_objects(other_id, durable, fields={"run_acceptance"})
    assert repository.load(other_id)["run_acceptance"]["repair_integration_budgets"] == {}


def test_retiring_current_repair_preserves_budgets_in_live_owner_and_explicit_archive(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = repository.load(RUN_ID)["run_acceptance"]["repair_integration_budgets"]
    state = repository.load_objects(RUN_ID)[0]
    archived = deepcopy(state["run_acceptance"])
    archived.update(owner_kind="run_acceptance", work_subject=f"run:{RUN_ID}", generation=1)
    repository.append_array(RUN_ID, "retired_run_acceptance", [archived])
    state["run_acceptance"]["repair_job"] = None
    _save(repository, state)
    assert state["run_acceptance"]["repair_integration_budgets"] == {}
    state["run_acceptance"]["phase"] = "reviewing"
    _save(repository, state)
    full = repository.load(RUN_ID)
    assert full["run_acceptance"]["repair_integration_budgets"] == original
    assert full["retired_run_acceptance"][0]["repair_integration_budgets"] == original


def test_missing_current_relation_is_rejected_without_loading_a_different_budget(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("DELETE FROM work_job_repair_budgets WHERE run_id=? AND repair_generation=2001", (RUN_ID,))
        connection.commit()
    with pytest.raises(ValueError, match="Missing exact Repair integration budget"):
        repository.load_objects(RUN_ID)


def test_current_resync_renewal_preserves_old_usage_and_full_history(tmp_path: Path) -> None:
    from agent_run.integration_resync_budget import renew_resync_budget

    repository = _repository(tmp_path)
    complete = repository.load(RUN_ID)
    policy = DeliveryPolicy().snapshot()
    limit = DeliveryPolicy().integration_resync_limit
    for owner in complete["run_acceptance"]["repair_integration_budgets"].values():
        owner.update(policy_snapshot=deepcopy(policy), integration_resync={
            "generation": 1, "window": limit + 1, "used": limit, "limit": limit, "open": False,
            "source_sha": "source", "target_sha": "target", "history": [{
                "generation": 1, "window": window, "source_sha": f"source-{window}", "target_sha": "target",
            } for window in range(2, limit + 2)],
        })
    _save(repository, complete)
    before = repository.load(RUN_ID)["run_acceptance"]["repair_integration_budgets"]
    state = repository.load_objects(RUN_ID)[0]
    current = state["run_acceptance"]["repair_integration_budgets"]["2001"]
    renew_resync_budget(current, policy_snapshot=policy)
    _save(repository, state)
    after = SQLiteStateRepository(tmp_path).load(RUN_ID)["run_acceptance"]["repair_integration_budgets"]
    assert after["1"] == before["1"]
    assert after["2"] == before["2"]
    assert after["2001"]["integration_resync"] == {
        **before["2001"]["integration_resync"], "generation": 2, "used": 0,
        "history": [*before["2001"]["integration_resync"]["history"], {"generation": 2, "renewed": True}],
    }


def test_current_budget_limit_failure_rolls_back_other_changes_and_history(tmp_path: Path) -> None:
    from agent_run.state_limits import MAX_CURRENT_BYTES

    repository = _repository(tmp_path)
    before = repository.load(RUN_ID)
    state = repository.load_objects(RUN_ID)[0]
    state["run_acceptance"]["phase"] = "candidate"
    state["run_acceptance"]["repair_integration_budgets"]["2001"]["integration_resync"] = {
        "generation": 1, "window": 1, "used": 0, "limit": 1, "open": True,
        "source_sha": "x" * MAX_CURRENT_BYTES, "target_sha": "target", "history": [],
    }
    with pytest.raises(ValueError, match="record exceeds"):
        _save(repository, state)
    assert SQLiteStateRepository(tmp_path).load(RUN_ID) == before


@pytest.mark.parametrize("current", [2001, None])
def test_current_comparison_uses_same_budget_identity_for_full_and_partial_queries(
    tmp_path: Path, current: int | None,
) -> None:
    from agent_run.object_state import current_projection_facts

    repository = _repository(tmp_path, current=current)
    complete = repository.load(RUN_ID)
    partial = repository.load_objects(RUN_ID)[0]
    assert current_projection_facts(complete) == current_projection_facts(partial)
    if current is not None:
        partial["run_acceptance"]["repair_integration_budgets"][str(current)]["policy_snapshot"]["revision"] = "changed"
        assert current_projection_facts(complete) != current_projection_facts(partial)


def test_restoring_budget_scope_preserves_active_parent_and_budget_owner_references(tmp_path: Path) -> None:
    from agent_run.state import _sanitize_durable_errors
    from agent_run.work_projection import restore_work_scopes

    repository = _repository(tmp_path)
    source = repository.load_objects(RUN_ID)[0]
    current = _sanitize_durable_errors(source)
    parent = current["run_acceptance"]
    budget = parent["repair_integration_budgets"]["2001"]
    restore_work_scopes(current, source, preserve_owner_references=True)
    assert current["run_acceptance"] is parent
    assert parent["repair_integration_budgets"]["2001"] is budget
    parent["phase"] = "reviewing"
    budget["policy_snapshot"]["revision"] = "retained-reference"
    _save(repository, current)
    full = repository.load(RUN_ID)["run_acceptance"]
    assert full["phase"] == "reviewing"
    assert full["repair_integration_budgets"]["2001"]["policy_snapshot"]["revision"] == "retained-reference"
    assert set(full["repair_integration_budgets"]) == {"1", "2", "2001"}


@pytest.mark.parametrize("field", ["review_budget_history", "completed_repair_jobs", "semantic_attempt_history"])
@pytest.mark.parametrize("current", [2001, None])
def test_one_history_field_does_not_expand_unrelated_repair_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, current: int | None,
) -> None:
    from agent_run import sqlite_change_job_queries as queries

    repository = _repository(tmp_path, current=current)
    complete = repository.load(RUN_ID)
    complete["run_acceptance"][field] = []
    _save(repository, complete)
    original = deepcopy(repository.load(RUN_ID))
    state = repository.load_objects(RUN_ID)[0]
    owner = state["run_acceptance"]
    assert field not in owner
    load_owner = queries._load_owner

    def selected_history(connection, run_id, job_id, **kwargs):
        assert ":repair-budget:" not in job_id, "one history field expanded unrelated Repair budgets"
        return load_owner(connection, run_id, job_id, **kwargs)

    with monkeypatch.context() as bounded:
        bounded.setattr(queries, "_load_owner", selected_history)
        assert owner.materialize(field) == []
    owner["phase"] = "reviewing"
    _save(repository, state)
    full = repository.load(RUN_ID)
    assert full["run_acceptance"][field] == []
    assert full["run_acceptance"]["repair_integration_budgets"] == original["run_acceptance"]["repair_integration_budgets"]
