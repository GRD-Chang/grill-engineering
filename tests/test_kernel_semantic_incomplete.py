"""Semantic Development continuation is bounded work, never output repair."""
import pytest

from agent_run.contracts.artifact import ArtifactContractError, parse_artifact, role_schema
from agent_run.model import wire_json
from agent_run.core.attempt_continuation import load_continuation
from test_kernel_attempt_continuation import _native_boundary


INCOMPLETE = wire_json({"result_kind": "incomplete", "summary": "Implementation is unfinished; continue the current task.", "human_blockers": None})


def test_only_development_accepts_structured_incomplete():
    assert "incomplete" in role_schema("development")["properties"]["result_kind"]["enum"]
    assert parse_artifact("development", INCOMPLETE)["result_kind"] == "incomplete"
    with pytest.raises(ArtifactContractError):
        parse_artifact("publication", INCOMPLETE)


def test_incomplete_uses_same_attempt_thread_and_deadline_with_new_invocation(tmp_path):
    kernel, owner, operations, attempt_id, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    before = kernel.status(original.subject)["budgets"]
    kernel.clock = lambda: 103.0
    step = operations.once()
    resumed = step.call
    assert resumed is not None
    assert resumed.attempt_id == attempt_id and resumed.thread_id == original.thread_id
    assert resumed.invocation_id != original.invocation_id
    assert resumed.deadline_at == original.deadline_at == 107.0
    assert resumed.mode == "resume" and resumed.output_ordinal == 0
    assert resumed.context["readonly"] is False
    assert kernel.status(original.subject)["budgets"] == before
    with kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, attempt_id)
        assert facts.semantic_used == 1 and facts.semantic_deadline == original.deadline_at
        assert facts.ordinary_used == facts.capacity_count == facts.output_ordinal == 0
        assert not tx.rows("SELECT id FROM effects")
        assert tx.get(tx.one("SELECT raw_id FROM invocation_outputs WHERE invocation=?", (original.invocation_id,))[0], original.subject) == INCOMPLETE
