"""A Ticket fixes its dependency-visible Run head at first workspace intent."""

from pathlib import Path

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Requirements
from agent_run.adapters.git_objects import LocalGit
from agent_run.model import Role
from support.offline import GraphScenario
from test_kernel_requirements import native_input
from test_kernel_remote_effects import EffectBoundary, effect_boundary
from support.core_operations import CoreOperations, scripted_return


def dependent_ticket(tmp_path: Path):
    scenario = GraphScenario(tmp_path / "dependencies", Requirements.from_wire(native_input()["initial_requirements"]))
    assert scenario.kernel.source_status()["remaining"]["ticket:4"] == "blocked_by_incomplete_tickets"
    assert scenario.run().kind == "complete"
    head = scenario.port.remote_commit("refs/heads/run")
    assert head is not None and head != scenario.initial
    scenario.select("ticket:4")
    assert not Path(scenario.delivery.checkout).exists()
    assert not scenario.port.git.run("for-each-ref", "--format=%(refname)", scenario.delivery.source_ref).stdout
    return scenario, head


def workspace_intent(scenario, monkeypatch, *, sent=False):
    with effect_boundary(scenario, monkeypatch, "workspace", sent=sent):
        with pytest.raises(EffectBoundary) as boundary:
            CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).until(lambda step: False, limit=16)
    return boundary.value


def first_development(scenario):
    step = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).until(
        lambda step: step.call is not None, limit=8)
    assert step.call.role == Role.DEVELOPMENT
    return step


def test_dependent_ticket_first_development_sees_delivered_dependency_code(tmp_path: Path, monkeypatch) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    prepared = workspace_intent(scenario, monkeypatch).effect
    assert prepared.operation == "workspace"
    assert prepared.before_sha == head.sha and prepared.base == head
    assert kernel.delivery(subject).base == head
    assert kernel.status(subject)["source_sha"] == head.sha
    scenario.kernel = Kernel(kernel.storage.path)
    assert workspace_intent(scenario, monkeypatch).effect == prepared
    assert scenario.runner().run_once().kind == "progress"
    development = first_development(scenario)
    assert development.call.context["base_sha"] == head.sha
    checkout = Path(development.call.context["checkout"])
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == head.sha
    assert (checkout / "development-1.txt").read_text() == "Change 1\n"
    scripted_return(scenario.kernel, scenario.runner(), development.call, scenario.agent)


@pytest.mark.parametrize("dispatched", [False, True], ids=["prepared", "sent"])
def test_workspace_intent_keeps_allocated_base_after_run_branch_moves(tmp_path: Path, monkeypatch, dispatched: bool) -> None:
    scenario, allocated = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    boundary = workspace_intent(scenario, monkeypatch, sent=dispatched)
    original = boundary.effect
    assert original.operation == "workspace"
    later = scenario.port.external_remote_append(scenario.delivery.target_ref, {"later-run.txt": "Later Run work\n"}, "after-allocation")
    assert later != allocated
    scenario.kernel = kernel = Kernel(kernel.storage.path)
    repeated = workspace_intent(scenario, monkeypatch)
    assert repeated.phase == ("reconcile" if dispatched else "execute")
    assert repeated.effect == original and kernel.delivery(subject).base == allocated
    if dispatched:
        # The previously authorized physical call completes after restart.
        result = scenario.make_ports(scenario.runner().binding).execute(original, boundary.authorize)
        assert result.disposition == "target"
    assert scenario.runner().run_once().kind == "progress"
    development = first_development(scenario)
    assert development.call.context["base_sha"] == allocated.sha
    assert (Path(development.call.context["checkout"]) / "development-1.txt").exists()
    assert not (Path(development.call.context["checkout"]) / "later-run.txt").exists()
    with kernel.storage.transaction(write=False) as tx:
        workspaces = tx.rows("SELECT before_sha,attempts FROM effects WHERE subject=? AND operation='workspace'", (subject,))
        assert len(workspaces) == 1 and workspaces[0]["before_sha"] == allocated.sha and workspaces[0]["attempts"] == 1
    scripted_return(scenario.kernel, scenario.runner(), development.call, scenario.agent)


def test_new_workspace_never_resets_a_foreign_preexisting_ticket_ref(tmp_path: Path, monkeypatch) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    scenario.port.git.run("update-ref", scenario.delivery.source_ref, scenario.initial.sha, "0" * len(scenario.initial.sha))
    effect = workspace_intent(scenario, monkeypatch).effect
    assert effect.before_sha == head.sha
    assert scenario.port.local.reconcile(effect).disposition == "third"

    def intent():
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT id,subject,generation,status,attempts,payload_id,receipt_id,before_sha,base_sha,source_ref,target_ref "
                         "FROM effects WHERE id=?", (effect.effect_id,))
            assert tuple(row[key] for key in ("status", "attempts", "receipt_id")) == ("prepared", 0, None)
            assert not tx.rows("SELECT position FROM history WHERE kind='effect_dispatched' AND identity=?", (effect.effect_id,))
            assert len(tx.rows("SELECT position FROM history WHERE kind='effect_prepared' AND identity=?", (effect.effect_id,))) == 1
            return tuple(row)

    original = intent()
    budgets = kernel.status(subject)["budgets"]
    git_files = lambda: {str(path.relative_to(scenario.port.git.root)): path.read_bytes()
                        for path in (scenario.port.git.root / ".git").rglob("*") if path.is_file()}
    before_git = git_files()
    execute, git_run = scenario.port.execute, LocalGit.run
    calls, authorizations, physical_writes = [], [], []
    inside_workspace = False

    def recorded_git(local, *arguments, **kwargs):
        write = bool(arguments) and (arguments[0] in {"update-ref", "reset", "switch", "commit", "commit-tree", "add", "merge", "write-tree"}
            or arguments[0] == "worktree" and len(arguments) > 1 and arguments[1] in {"add", "remove", "move", "prune", "repair"}
            or arguments[0] == "hash-object" and "-w" in arguments)
        if inside_workspace and write:
            physical_writes.append(arguments)
        return git_run(local, *arguments, **kwargs)

    def recorded_execute(current, authorize):
        nonlocal inside_workspace
        assert current == effect
        calls.append(current)

        def guarded():
            authorizations.append(current.effect_id)
            authorize()

        inside_workspace = True
        try:
            return execute(current, guarded)
        finally:
            inside_workspace = False

    monkeypatch.setattr(LocalGit, "run", recorded_git)
    monkeypatch.setattr(scenario.port, "execute", recorded_execute)
    refused = scenario.runner().run_once()
    assert (refused.kind, refused.reason) == ("paused", "write_not_dispatched")
    assert calls == [effect] and authorizations == physical_writes == []
    assert intent() == original
    assert scenario.port.git.commit(scenario.delivery.source_ref) == scenario.initial
    assert not Path(scenario.delivery.checkout).exists()
    requests = [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "workspace"]
    # This is the offline adapter's attempt journal, not a physical Git write.
    assert len(requests) == 1 and requests[0]["intent"] == effect.effect_id
    assert kernel.status(subject)["gates"] == ["write_conflict"]

    # The old Scenario helper eagerly marked this request sent before its
    # physical preflight. The formal old Host never did: it retained prepared/0.
    # No sent uncertainty exists to reconcile; the global runner is quiescent
    # while the subject remains manually paused on its original conflict.
    scenario.kernel = kernel = Kernel(kernel.storage.path)
    for _ in range(3):
        assert kernel.operation_state().disposition == "quiescent"
        assert scenario.runner().run_once().kind == "quiescent"
        paused = scenario.run()
        assert (paused.kind, paused.subject, paused.reason) == ("paused", subject, "write_conflict")
        assert intent() == original
        assert kernel.status(subject)["gates"] == ["write_conflict"]
        assert kernel.status(subject)["budgets"] == budgets
    with pytest.raises(KernelError, match="unresolved_write_conflict"):
        kernel.control("resume", "cannot-adopt-foreign-ref", subject=subject)
    assert intent() == original and kernel.status(subject)["gates"] == ["write_conflict"]
    assert calls == [effect] and authorizations == physical_writes == []
    assert [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "workspace"] == requests
    assert scenario.port.git.commit(scenario.delivery.source_ref) == scenario.initial
    assert not Path(scenario.delivery.checkout).exists()
    assert git_files() == before_git
