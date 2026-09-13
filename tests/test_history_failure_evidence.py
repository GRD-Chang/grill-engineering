from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from agent_run import cli_presentation


@pytest.mark.parametrize("source", ["git_integrity", "acceptance", "required_checks"])
@pytest.mark.parametrize("plain", [True, False])
def test_history_fallback_failure_sources(
    source: str, plain: bool, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "160")
    evidence: dict[str, Any]
    if source == "git_integrity":
        evidence = {
            "status": "fail", "reason": "checkout changed", "expected_head": "expected-head",
            "observed_head": "observed-head", "workspace_clean": False,
            "recovery_head": "recovered-head", "recovery_action": "reset-and-clean",
            "recovery_error": "recovery-error",
        }
        expected = ["checkout changed", "expected-head", "observed-head", "工作区清洁：否",
                    "recovered-head", "reset-and-clean", "recovery-error"]
    elif source == "acceptance":
        evidence = {"checks": {lane: {"status": "fail", "evidence": f"evidence-{lane}",
                                      "findings": [f"finding-{lane}"]}
                               for lane in ("e2e", "standards", "spec")}}
        expected = [f"{kind}-{lane}" for lane in ("e2e", "standards", "spec")
                    for kind in ("evidence", "finding")]
    else:
        evidence = {"result": "fail", "head_sha": "failed-check-head", "pr_number": 17,
                    "checks": [{"name": "failing-check", "bucket": "fail",
                                "link": "https://example.test/check"}]}
        expected = ["failing-check", "https://example.test/check"]
    evidence["policy_snapshot"] = {"private-marker": True}
    attempt = {"attempt_id": "attempt", "role": "reviewer", "work_subject": "ticket:3",
               "generation": 1, "ordinal": 1, "budget_window": 1, "status": "completed"}
    identity = {"candidate_sha": "candidate", "base_sha": "base", "effective_revision": "revision"}
    receipt = {**identity, "window": 1, "publication_sha": "publication-sha", "pr_number": 17,
               "failure_evidence_source": source, "failure_evidence": evidence}
    job = {"ticket_number": 3, "semantic_attempt_history": [attempt],
           "review_budget": {"window": 1, "review_artifacts": []},
           "fallback_publication_receipt": receipt}
    state = {"run_id": "run-evidence", "ticket_jobs": {"3": job},
             "semantic_agent_attempts": [attempt], "agent_invocation_history": [{
                 "work_subject": "ticket:3", "role": "reviewer", "status": "completed",
                 "semantic_attempt": attempt, "currentness_boundary": identity}]}
    original = deepcopy(state)
    monkeypatch.setattr(cli_presentation, "_use_rich_status", lambda plain: not plain)
    cli_presentation._print_history(state, as_json=False, plain=plain, details=True)
    output = capsys.readouterr().out
    for value in expected:
        assert value in output
    assert "publication-sha" not in output
    assert "failed-check-head" not in output
    assert "private-marker" not in output
    assert "policy_snapshot" not in output
    cli_presentation._print_history(state, as_json=True)
    before = json.loads(capsys.readouterr().out)
    cli_presentation._print_history(state, as_json=True, plain=plain, details=True)
    assert json.loads(capsys.readouterr().out) == before
    assert state == original

    receipt["candidate_sha"] = "other-candidate"
    cli_presentation._print_history(state, as_json=False, plain=plain, details=True)
    mismatched = capsys.readouterr().out
    assert "publication-sha" not in mismatched
    for value in expected:
        assert value not in mismatched

    receipt["candidate_sha"] = "candidate"
    receipt["failure_evidence"] = {"policy_snapshot": {"private-marker": True}}
    cli_presentation._print_history(state, as_json=False, plain=plain, details=True)
    empty = capsys.readouterr().out
    assert "失败依据" not in empty
