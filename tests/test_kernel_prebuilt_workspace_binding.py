"""Core client contract only: the source owner's authenticated port is mocked.

These tests do not implement or validate import proof or physical ownership.
"""
from pathlib import Path
import json

import pytest

from agent_run_kernel import Kernel, KernelError
from agent_run_kernel import migration_run_ownership
from support.core_operations import CoreOperations, scripted_return
from test_kernel_workspace_allocation import dependent_ticket, workspace_intent


@pytest.fixture(autouse=True)
def no_idle_proof_by_default(monkeypatch):
    # The owner has frozen this port; its production implementation is pending.
    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", lambda *args: None, raising=False)


def fragment(scenario):
    job = scenario.delivery
    return {"format": "copied-idle-workspace/1", "source_sha256": "a" * 64,
        "run_id": scenario.spec.run_id, "subject": job.key, "generation": 1,
        "source_record": {"table": "run_graph_members", "key": [scenario.spec.run_id, "original-graph-version", job.issue]},
        "repository_root": str(scenario.port.git.root), "common_git_dir": job.common_git_dir,
        "copy_uuid": "b" * 32, "disposition": "new-unused", "source_ref": job.source_ref,
        "expected_oid": None, "checkout": job.checkout, "owned_root": job.owned_root,
        "workspace_oid": scenario.initial.sha, "before_head": scenario.initial.sha + "\n", "checkout_status": []}


def test_first_binding_keeps_authenticated_h0_separate_from_dependency_h1(tmp_path, monkeypatch):
    scenario, h1 = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    certified = fragment(scenario)
    calls = []

    def source_port(storage, tx, run, job):
        assert storage is kernel.storage and run["id"] == scenario.spec.run_id
        assert (job["id"], job["generation"], job["phase"]) == (subject, 1, "workspace")
        assert job["base_sha"] == scenario.initial.sha != h1.sha
        assert not tx.rows("SELECT id FROM attempts WHERE subject=?", (subject,))
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation='workspace'", (subject,))
        calls.append(subject)
        return certified

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", source_port)
    effect = workspace_intent(scenario, monkeypatch).effect
    assert calls == [subject]
    assert effect.before_sha == h1.sha and effect.base == h1
    assert effect.payload["prebuilt_workspace"] == certified
    assert certified["workspace_oid"] == scenario.initial.sha
    assert kernel.delivery(subject).base == h1
    assert kernel.status(subject)["pending_attempt"] is None


def test_no_applicable_binding_preserves_the_ordinary_workspace_payload(tmp_path, monkeypatch):
    scenario, h1 = dependent_ticket(tmp_path)
    effect = workspace_intent(scenario, monkeypatch).effect
    assert effect.before_sha == h1.sha and effect.base == h1
    assert "prebuilt_workspace" not in effect.payload


def test_unproven_binding_rolls_back_first_allocation_without_fallback(tmp_path, monkeypatch):
    scenario, h1 = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    before = kernel.delivery(subject)
    budgets = kernel.status(subject)["budgets"]

    def unproven(*args):
        raise KernelError("prebuilt_workspace_unproven")

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", unproven)
    with pytest.raises(KernelError, match="prebuilt_workspace_unproven"):
        CoreOperations(kernel, scenario.execution, scenario.make_ports).once()
    assert kernel.delivery(subject) == before and before.base != h1
    assert kernel.status(subject)["budgets"] == budgets
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation='workspace'", (subject,))
        assert not tx.rows("SELECT id FROM attempts WHERE subject=?", (subject,))


@pytest.mark.parametrize("sent", [False, True], ids=["prepared", "sent"])
def test_restart_reuses_original_binding_and_h1_without_another_source_lookup(tmp_path, monkeypatch, sent):
    scenario, h1 = dependent_ticket(tmp_path)
    certified = fragment(scenario)
    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", lambda *args: certified)
    original = workspace_intent(scenario, monkeypatch, sent=sent).effect
    scenario.port.external_remote_append(scenario.delivery.target_ref, {"later.txt": "Later target\n"}, "after-binding")
    scenario.kernel = Kernel(scenario.kernel.storage.path)

    def must_not_rebind(*args):
        pytest.fail("An original workspace intent must not resolve its binding again")

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", must_not_rebind)
    repeated = workspace_intent(scenario, monkeypatch)
    assert repeated.phase == ("reconcile" if sent else "execute")
    assert repeated.effect == original
    assert original.before_sha == h1.sha and original.payload["prebuilt_workspace"] == certified
    assert scenario.kernel.delivery(scenario.delivery.key).base == h1


@pytest.mark.parametrize("whole_run", [False, True], ids=["subject-stop", "run-stop"])
def test_stop_does_not_resolve_or_allocate_a_first_binding(tmp_path, monkeypatch, whole_run):
    scenario, _ = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    runner = scenario.runner()
    kernel.control("stop", "stop-before-binding", subject=None if whole_run else subject)

    def must_not_read(*args):
        pytest.fail("Stop grants neither a proof lookup nor a workspace allocation")

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", must_not_read)
    step = runner.run_once()
    assert step.kind == ("paused" if whole_run else "quiescent")
    assert step.call is None and kernel.status(subject)["pending_attempt"] is None
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation='workspace'", (subject,))


def test_later_generation_never_rebinds_a_gen1_idle_resource(tmp_path, monkeypatch):
    from dataclasses import replace

    scenario, h1 = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    for _ in range(80):
        step = operations.once()
        if step.call is not None:
            scripted_return(kernel, operations.runner, step.call, scenario.agent)
        if kernel.status(subject)["phase"] == "checks":
            break
    else:
        raise AssertionError("Original generation did not publish its real PR")
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body + "\nClarified request") if issue.number == scenario.delivery.issue else issue
        for issue in scenario.requirements.issues))
    world = kernel.observe_control(subject, scenario.make_ports)
    old = kernel.delivery(subject)
    replacement = replace(old, source_ref=old.source_ref + "-g2", base=world.target,
        checkout=str(Path(old.checkout).with_name("ticket-g2")))
    kernel.control("requeue", "normal-new-generation", subject=subject, world=world, replacement=replacement)

    def must_not_rebind(*args):
        pytest.fail("Generation 2 cannot claim the imported Generation 1 resource")

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", must_not_rebind)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    operations.until(lambda step: kernel.status(subject)["generation"] == 2, limit=8)
    original = workspace_intent(scenario, monkeypatch).effect
    assert original.before_sha == h1.sha and "prebuilt_workspace" not in original.payload
    assert kernel.status(subject)["generation"] == 2


def test_same_gen1_source_change_reuses_binding_after_partial_prior_retirement(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_run_kernel.effects import LocalGit

    scenario, h1 = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    certified = fragment(scenario)
    calls = []

    def source_port(*args):
        calls.append(subject)
        return certified

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", source_port)
    original = workspace_intent(scenario, monkeypatch).effect
    checkout = Path(original.payload["checkout"])
    run = LocalGit.run

    def interrupted(git, *arguments, **kwargs):
        if arguments[:2] == ("worktree", "add") and str(checkout) in arguments:
            raise OSError("Synthetic interruption after the authorized ref write")
        return run(git, *arguments, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(LocalGit, "run", interrupted)
        assert scenario.runner().run_once().reason == "original_write_readback_required"
    assert scenario.port.git.run("rev-parse", original.source_ref).stdout.strip() == h1.sha
    assert not checkout.exists()
    scenario.requirements = replace(scenario.requirements,
        parent=replace(scenario.requirements.parent, body=scenario.requirements.parent.body + "\nClarified parent"))
    later = scenario.port.external_remote_append(scenario.delivery.target_ref, {"later.txt": "Later Run work\n"}, "after-first-binding")
    assert later != h1
    scenario.kernel = kernel = Kernel(kernel.storage.path)

    def must_not_resolve_again(*args):
        pytest.fail("A same-generation replacement must reuse its durable binding")

    monkeypatch.setattr(migration_run_ownership, "prebuilt_workspace_binding", must_not_resolve_again)
    assert scenario.runner().run_once().kind == "progress"
    with kernel.storage.transaction(write=False) as tx:
        old = tx.one("SELECT status,attempts,payload_id FROM effects WHERE id=?", (original.effect_id,))
        assert (old["status"], old["attempts"]) == ("retired", 1)
        assert json.loads(tx.get(old["payload_id"], subject))["prebuilt_workspace"] == certified
        assert tx.rows("SELECT position FROM history WHERE kind='stale_intent_not_applied' AND identity=?", (original.effect_id,))
        assert not tx.rows("SELECT id FROM attempts WHERE subject=?", (subject,))
    replacement = workspace_intent(scenario, monkeypatch).effect
    assert kernel.status(subject)["generation"] == 1
    assert replacement.effect_id != original.effect_id and replacement.before_sha == original.before_sha == h1.sha
    assert kernel.delivery(subject).base == h1
    assert calls == [subject]
    assert replacement.payload["prebuilt_workspace"] == certified == original.payload["prebuilt_workspace"]

    with kernel.storage.transaction(write=False) as tx:
        assert len(tx.rows("SELECT id FROM effects WHERE subject=? AND generation=1 AND operation='workspace'", (subject,))) == 2
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original.effect_id,))) == ("retired", 1)
