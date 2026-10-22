"""Public Abandon preserves the original ref while retiring a proved checkout."""
import json
from pathlib import Path

import pytest

import agent_run_kernel.effects as effects
from agent_run_kernel.model import KernelError, resource_wire
from test_kernel_idle_prebuilt import idle_copy as idle_copy
from test_kernel_prebuilt_actual_integration_review import (
    installed, partial_actual_workspace, unchanged_source_bytes as unchanged_source_bytes,
)


def finish_abandon(kernel, runner):
    for _ in range(32):
        step = runner.run_once()
        assert step.call is None
        if step.kind == "complete":
            assert step.reason == "abandoned"
            assert kernel.source_status()["abandonment"]["status"] == "complete"
            return
        assert step.kind == "progress", step
    pytest.fail("Explicit Abandon did not reach its bounded terminal state")


def test_partial_public_abandon_removes_checkout_preserves_h1_ref(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, original, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    resources = kernel.generation_resources()
    witness = next(item for item in resources if item.subject == original.subject)
    inspections = ports.inspect(resources)
    current = next(item for item in inspections if item.resource == witness)
    assert current.disposition == "clean" and current.head_sha == h0
    assert witness.head_sha == h1 and witness.prebuilt_workspace == original.payload["prebuilt_workspace"]
    assert kernel.control("abandon", "partial-explicit-abandon", resource_inspections=inspections) == "pending"
    finish_abandon(kernel, runner)
    assert not Path(witness.checkout).exists()
    assert git.commit(witness.source_ref).sha == h1
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT subject,receipt_id FROM effects WHERE operation='abandon_worktree' AND subject=?", (original.subject,))
        receipt = json.loads(tx.get(row["receipt_id"], row["subject"]))
        assert receipt["disposition"] == "target"
        assert receipt["raw"] == {"reason": "worktree_absent", "resource": resource_wire(witness)}
        assert not tx.rows("SELECT id FROM attempts")
    cleanup = next(effect for effect in ports.effects if effect.operation == "abandon_worktree" and effect.subject == original.subject)
    assert cleanup.before_sha == h1
    assert ports.reconcile(cleanup).disposition == "target"


@pytest.mark.parametrize("change,error", [("dirty", "dirty_worktree"), ("head", "abandon_resource_unproven"), ("ref", "abandon_resource_unproven")])
def test_partial_abandon_rejects_external_work(idle_copy, monkeypatch, change, error):
    kernel, execution, runner, ports, git, original, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    checkout = Path(original.payload["checkout"])
    if change == "dirty":
        (checkout / "keep.txt").write_text("Preserve local work\n")
    else:
        h2 = git.make_commit(git.commit(h1).tree, (h1,), "Other current work").sha
        if change == "head":
            git.run("switch", "--detach", h2, cwd=checkout)
        else:
            git.run("update-ref", original.source_ref, h2, h1)
    before = {str(path): path.read_bytes() for path in checkout.rglob("*") if path.is_file()}
    head = git.run("rev-parse", "HEAD", cwd=checkout).stdout
    ref = git.commit(original.source_ref).sha
    with pytest.raises(KernelError, match=error):
        kernel.control("abandon", "reject-external-work", resource_inspections=ports.inspect(kernel.generation_resources()))
    assert kernel.source_status()["abandonment"] is None
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout == head
    assert git.commit(original.source_ref).sha == ref
    assert {str(path): path.read_bytes() for path in checkout.rglob("*") if path.is_file()} == before


def test_already_authorized_workspace_completion_after_abandon_uses_h1(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, original, h0, h1 = installed(idle_copy)
    ports.hold = False
    actual = effects.run_git
    admitted = []

    def finish_after_abandon(command, *args, **kwargs):
        if command[3:4] == ["switch"] and not admitted:
            # LocalGit has completed the real Core/physical write guard. Fence
            # at the subprocess boundary, then let that released write finish.
            inspections = ports.inspect(kernel.generation_resources())
            assert next(item for item in inspections if item.resource.subject == original.subject).head_sha == h0
            admitted.append(kernel.control("abandon", "abandon-released-write", resource_inspections=inspections))
        return actual(command, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(effects, "run_git", finish_after_abandon)
        assert runner.run_once().kind == "progress"
    assert admitted == ["pending"]
    assert git.run("rev-parse", "HEAD", cwd=Path(original.payload["checkout"])).stdout.strip() == h1
    finish_abandon(kernel, runner)
    assert not Path(original.payload["checkout"]).exists()
    assert git.commit(original.source_ref).sha == h1
    cleanups = [effect for effect in ports.effects if effect.operation == "abandon_worktree" and effect.subject == original.subject]
    assert len(cleanups) == 1 and cleanups[0].before_sha == h1
    assert "prebuilt_workspace" not in cleanups[0].payload["resource"]


def test_external_ref_change_at_second_cleanup_guard_preserves_partial_checkout(idle_copy, monkeypatch):
    kernel, execution, runner, ports, git, original, h0, h1 = partial_actual_workspace(idle_copy, monkeypatch)
    assert kernel.control("abandon", "abandon-before-late-change", resource_inspections=ports.inspect(kernel.generation_resources())) == "pending"
    h2 = git.make_commit(git.commit(h1).tree, (h1,), "New external ref ownership").sha
    actual = ports.execute
    changed = []

    def execute(effect, authorize):
        if effect.operation == "abandon_worktree" and effect.subject == original.subject:
            def change_before_delete():
                if ports.authorizations.count(effect.effect_id) == 2:
                    git.run("update-ref", original.source_ref, h2, h1)
                    changed.append(True)
            ports.before_authorize = change_before_delete
        return actual(effect, authorize)

    monkeypatch.setattr(ports, "execute", execute)
    # Local effects recheck physical identity after the Core authority guard;
    # the existing boundary-changed exception must propagate before deletion.
    with pytest.raises(KernelError, match="local_effect_boundary_changed"):
        for _ in range(32):
            step = runner.run_once()
            assert step.call is None and step.kind == "progress", step
        pytest.fail("Cleanup did not reach its second write guard")
    assert changed == [True]
    checkout = Path(original.payload["checkout"])
    assert checkout.is_dir()
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h0
    assert git.commit(original.source_ref).sha == h2
    assert kernel.source_status()["abandonment"]["status"] == "pending"
