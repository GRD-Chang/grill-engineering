"""Parent repair uses the ordinary PR boundary before updating a protected Run.

These journeys retain real Core, SQLite, Git and RemoteEffects. Only GitHub and
Agent responses are supplied by the existing isolated scenario adapters.
"""
from pathlib import Path
from io import StringIO

from agent_run.cli.render import render
from agent_run.model import DeliveryKind, Policy
from agent_run.queries.run import read
from support.parent_repair import _repairs, _requests, _run, _until
from support.offline_git import OfflineGit
from support.run_scoped_kernel import domain_scenario


def test_parent_finding_repair_pr_precedes_run_update_and_final_human_approval(tmp_path, monkeypatch, domain_scenario):
    scenario = domain_scenario(tmp_path / "journey", DeliveryKind.FINAL_RUN,
        reviews=("fail", "pass"), checks=("pending",),
        edits=({"parent-repair.txt": "Complete Parent repair\n"},))
    subject = scenario.delivery.key
    completion = scenario.kernel.source_status()["completions"]
    original_run = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert completion and scenario.port.issue_state(scenario.delivery.issue) == "OPEN"
    push = OfflineGit.push
    def protected_push(physical, ref, before, after):
        assert ref != scenario.delivery.source_ref, "Protected Run updates require an ordinary checked PR merge"
        return push(physical, ref, before, after)
    monkeypatch.setattr(OfflineGit, "push", protected_push)
    # The existing adapter loses acknowledgements after the actual remote write.
    scenario.port.lose_response.update({"pr_write", "merge"})
    waiting = _until(scenario, lambda state: state["phase"] == "repair_checks")
    repair = _repairs(scenario)[0]
    assert repair["state"] == "open" and not repair.get("merged")
    assert scenario.port.remote_commit(scenario.delivery.source_ref) == original_run
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref) is None
    assert [call["role"] for call in scenario.role_calls] == ["review", "development", "review"]
    assert waiting["budgets"]["integration"]["developments_used"] == 1
    assert waiting["budgets"]["integration"]["reviews_used"] == 2
    document = read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
    displayed = next(job for job in document.data["subjects"] if job["subject"] == subject)
    assert document.data["status"] == "waiting_checks"
    assert displayed["repair_pr"]["pr_number"] == repair["number"]
    output = StringIO()
    render(document, style="plain", details=False, stream=output)
    assert f"Run 修复 PR #{repair['number']}" in output.getvalue()
    candidate = waiting["candidate"]
    reviewed_tree = scenario.port.git.commit(repair["head"]["sha"]).tree
    scenario.checks[:] = ["pass"]
    assert _run(scenario).kind == "approval"
    repair = scenario.port.pr_by_number(repair["number"])
    assert repair.state == "MERGED" and repair.merged.tree == reviewed_tree
    run = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert run == repair.merged and scenario.port.git.is_ancestor(candidate, run.sha)
    assert scenario.port.git.parents(run.sha) == (original_run.sha, repair.head.sha)
    final = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert final.head == run and final.state == "OPEN"
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == scenario.initial
    assert scenario.kernel.source_status()["completions"] == completion
    before_approval = scenario.kernel.status(subject)
    assert before_approval["budgets"] == waiting["budgets"]
    assert len(_requests(scenario, "POST", "/pulls")) == 3  # Ticket, repair, final.
    assert len(_requests(scenario, "PUT", "/merge")) == 2  # Ticket and repair only.
    assert _run(scenario, approve=True).kind == "complete"
    default = scenario.port.remote_commit(scenario.delivery.target_ref)
    assert scenario.port.git.parents(default.sha)[1] == run.sha
    assert scenario.port.git.run("show", f"{default.sha}:parent-repair.txt").stdout == "Complete Parent repair\n"
    assert scenario.port.issue_state(scenario.delivery.issue) == "CLOSED"
    assert not any(write["operation"] == "run_branch" for write in scenario.writes())


def test_parent_without_finding_or_new_combination_goes_directly_to_final_pr(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "direct", DeliveryKind.FINAL_RUN)
    assert _run(scenario).kind == "approval"
    assert _repairs(scenario) == []
    assert scenario.kernel.status(scenario.delivery.key)["repair_history"] == []
    assert scenario.development_count == 0 and scenario.review_count == 1
    assert len([row for row in scenario.writes() if row["operation"] == "pr_write"]) == 1


def test_failed_repair_check_updates_the_same_pr_and_parent_budget(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "retry", DeliveryKind.FINAL_RUN,
        reviews=("fail", "pass"), checks=("fail",),
        edits=({"repair.txt": "First repaired result\n"}, {"repair.txt": "CI repaired result\n"}))
    state = _until(scenario, lambda state: state["phase"] == "development" and state["purpose"] == "ci")
    original = _repairs(scenario)[0]
    scenario.checks[:] = ["none"]
    assert _run(scenario).kind == "approval"
    repairs = _repairs(scenario)
    assert len(repairs) == 1 and repairs[0]["number"] == original["number"]
    assert repairs[0]["head"]["sha"] != original["head"]["sha"]
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]["developments_used"] == 1
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["integration"] == state["budgets"]["integration"]
    assert len([call for call in scenario.role_calls if call["role"] == "development"]) == 2
    assert len(scenario.kernel.status(scenario.delivery.key)["repair_history"]) == 1
    document = read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
    displayed = next(job for job in document.data["subjects"] if job["subject"] == scenario.delivery.key)
    assert displayed["repair_history"][0]["required_checks"]["result"] == "none"
    output = StringIO()
    render(document, style="plain", details=False, stream=output)
    assert "修复 PR 检查: 无强制检查" in output.getvalue()


def test_final_ci_failure_creates_a_new_repair_pr_without_reusing_merged_record(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "final-ci", DeliveryKind.FINAL_RUN,
        reviews=("fail", "pass"), checks=("pass",),
        edits=({"repair.txt": "First repair\n"}, {"repair.txt": "Final CI repair\n"}))
    _until(scenario, lambda state: state["phase"] == "checks")
    first = _repairs(scenario)[0]
    assert scenario.port.pr_by_number(first["number"]).state == "MERGED"
    scenario.checks[:] = ["fail"]
    _until(scenario, lambda state: state["phase"] == "development" and state["purpose"] == "ci")
    scenario.checks[:] = ["pass"]
    assert _run(scenario).kind == "approval"
    repairs = _repairs(scenario)
    assert len(repairs) == 2 and repairs[0] == first
    assert repairs[0]["number"] != repairs[1]["number"]
    state = scenario.kernel.status(scenario.delivery.key)
    assert len(state["repair_history"]) == 2
    assert state["budgets"]["ci"]["developments_used"] == 1
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == scenario.initial


def test_default_append_during_repair_checks_reaccepts_combination_without_extra_development(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "drift", DeliveryKind.FINAL_RUN,
        reviews=("fail", "pass"), checks=("pending",))
    _until(scenario, lambda state: state["phase"] == "repair_checks")
    repair = _repairs(scenario)[0]
    default = scenario.port.external_append(scenario.delivery.target_ref,
        {"latest-default.txt": "Independent default work\n"}, "latest-default")
    scenario.checks[:] = ["pass"]
    assert _run(scenario).kind == "approval"
    assert len(_repairs(scenario)) == 1 and _repairs(scenario)[0]["number"] == repair["number"]
    assert scenario.development_count == 1 and scenario.review_count == 3
    run = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert scenario.port.git.is_ancestor(default.sha, run.sha)
    assert scenario.port.git.run("show", f"{run.sha}:latest-default.txt").stdout == "Independent default work\n"


def test_externally_closed_repair_pr_preserves_identity_and_work(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "closed", DeliveryKind.FINAL_RUN,
        reviews=("fail", "pass"), checks=("pending",))
    before = _until(scenario, lambda state: state["phase"] == "repair_checks")
    repair = _repairs(scenario)[0]
    note = Path(scenario.delivery.checkout) / "retained-note.txt"
    note.write_text("Preserve unresolved repair work\n")
    scenario.port.external_pr_transition(repair["number"], "CLOSED", actor="external-maintainer")
    assert _run(scenario).kind == "paused"
    assert len(_repairs(scenario)) == 1
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == before["budgets"]
    assert note.read_text() == "Preserve unresolved repair work\n"
    assert scenario.port.pr_by_number(repair["number"]).state == "CLOSED"


def test_parent_budget_exhaustion_preserves_candidate_and_work_without_publishing(tmp_path, domain_scenario):
    scenario = domain_scenario(tmp_path / "budget", DeliveryKind.FINAL_RUN,
        reviews=("fail", "fail"), policy=Policy(run_repairs=1))
    assert _run(scenario).kind == "paused"
    state = scenario.kernel.status(scenario.delivery.key)
    assert "review_budget_exhausted" in state["gates"]
    assert state["budgets"]["integration"]["developments_used"] == 1
    assert state["budgets"]["integration"]["reviews_used"] == 2
    assert state["candidate"] and Path(scenario.delivery.checkout, "development-1.txt").is_file()
    assert _repairs(scenario) == []
