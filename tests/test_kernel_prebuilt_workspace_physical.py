"""Physical client tests with a mocked source-auth interface, not proof tests.

The opaque payload is deliberately not a source certificate. Real source/Git
authentication is validated separately when the source implementation lands.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.model import Effect, Execution, KernelError
import agent_run_kernel.migration_run_ownership as ownership


def workspace(git_repo, tmp_path, monkeypatch, *, lazy=False, equal=False):
    git = LocalGit(git_repo)
    h0 = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    checkout = owned / "ticket-2"
    if not lazy:
        git.run("worktree", "add", "--detach", str(checkout), h0.sha)
    if not equal:
        (git_repo / "next.txt").write_text("Dependency-visible H1\n")
        git.run("add", "--all")
        git.run("commit", "-m", "advance dependency-visible target")
    h1 = git.commit("HEAD")
    token = object()
    effect = Effect("workspace-client", "ticket:2", "workspace", "example/project",
                    "refs/heads/ticket-2", "refs/heads/run", h1.sha, None, h1, None,
                    None, None, {"checkout": str(checkout), "owned_root": str(owned),
                    "common_git_dir": str(git_repo / ".git"), "create_ref": True,
                    "prebuilt_workspace": token}, Execution("client-run", "owner", 1))
    calls = []

    def authenticate(selected, original):
        assert selected.root == git.root and original is effect
        assert original.payload["prebuilt_workspace"] is token
        calls.append(original.effect_id)
        # Only the projection consumed by this client; no forged proof/marker.
        return {"workspace_oid": h0.sha, "before_head": None if lazy else h0.sha + "\n",
                "checkout_status": None if lazy else []}

    monkeypatch.setattr(ownership, "authenticate_prebuilt_workspace", authenticate, raising=False)
    return git, LocalEffects(git, owned_root=owned), effect, checkout, h0, h1, calls


def snapshot(root: Path):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("ref_present,at_target", [(False, False), (True, False), (False, True), (True, True)])
def test_prebuilt_states_are_read_only_then_finish_only_missing_steps(git_repo, tmp_path, monkeypatch, ref_present, at_target):
    git, local, effect, checkout, h0, h1, calls = workspace(git_repo, tmp_path, monkeypatch)
    if ref_present:
        git.run("update-ref", effect.source_ref, h1.sha, "0" * len(h1.sha))
    if at_target:
        git.run("switch", "--detach", h1.sha, cwd=checkout)
    before = snapshot(tmp_path)
    result = local.reconcile(effect)
    assert snapshot(tmp_path) == before
    assert calls
    assert result.disposition == ("target" if ref_present and at_target else "prior")
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert len(guards) == int(not ref_present) + int(not at_target)
    assert git.commit(effect.source_ref) == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h1.sha
    assert (checkout / "next.txt").read_text() == "Dependency-visible H1\n"
    assert effect.before_sha == h1.sha and effect.payload["prebuilt_workspace"] is not None


@pytest.mark.parametrize("at_target,ref_present", [(False, False), (False, True), (True, False), (True, True)])
def test_lazy_creation_or_existing_complete_target(git_repo, tmp_path, monkeypatch, at_target, ref_present):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch, lazy=True)
    if at_target:
        git.run("worktree", "add", "--detach", str(checkout), h1.sha)
    if ref_present:
        git.run("update-ref", effect.source_ref, h1.sha, "0" * len(h1.sha))
    assert local.reconcile(effect).disposition == ("target" if at_target and ref_present else "prior")
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert len(guards) == int(not at_target) + int(not ref_present)
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h1.sha


def test_equal_h0_h1_only_creates_missing_ref(git_repo, tmp_path, monkeypatch):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch, equal=True)
    assert h0 == h1
    before_head = (git_repo / ".git" / "worktrees" / checkout.name / "HEAD").read_bytes()
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert guards == ["write"] and git.commit(effect.source_ref) == h1
    assert (git_repo / ".git" / "worktrees" / checkout.name / "HEAD").read_bytes() == before_head


def test_second_write_fence_preserves_partial_ref_and_reentry(git_repo, tmp_path, monkeypatch):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    guards = []

    def fence():
        guards.append("write")
        if len(guards) == 2:
            raise KernelError("executor_fenced")

    with pytest.raises(KernelError, match="executor_fenced"):
        local.execute(effect, fence)
    assert guards == ["write", "write"] and git.commit(effect.source_ref) == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h0.sha
    assert local.reconcile(effect).disposition == "prior"
    guards.clear()
    reopened = LocalEffects(git, owned_root=checkout.parent)
    assert reopened.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert guards == ["write"] and git.commit(effect.source_ref) == h1


def test_cas_response_loss_reopens_as_partial_prior(git_repo, tmp_path, monkeypatch):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    original = LocalGit.run
    lost = []

    def run(self, *arguments, **kwargs):
        result = original(self, *arguments, **kwargs)
        if arguments[:1] == ("update-ref",) and not lost:
            lost.append(True)
            raise OSError("Actual CAS succeeded; response unavailable")
        return result

    with monkeypatch.context() as failure:
        failure.setattr(LocalGit, "run", run)
        with pytest.raises(OSError, match="response unavailable"):
            local.execute(effect, lambda: None)
    assert git.commit(effect.source_ref) == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h0.sha
    assert local.reconcile(effect).disposition == "prior"
    guards = []
    assert LocalEffects(git, owned_root=checkout.parent).execute(effect, lambda: guards.append("write")).disposition == "target"
    assert guards == ["write"]


def test_switch_response_loss_is_read_back_without_another_write(git_repo, tmp_path, monkeypatch):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    original = LocalGit.run

    def run(self, *arguments, **kwargs):
        result = original(self, *arguments, **kwargs)
        if arguments[:1] == ("switch",):
            raise OSError("Actual switch succeeded; response unavailable")
        return result

    guards = []
    with monkeypatch.context() as failure:
        failure.setattr(LocalGit, "run", run)
        with pytest.raises(OSError, match="response unavailable"):
            local.execute(effect, lambda: guards.append("write"))
    assert guards == ["write", "write"]
    reopened = LocalEffects(git, owned_root=checkout.parent)
    assert reopened.reconcile(effect).disposition == "target"
    assert reopened.execute(effect, lambda: pytest.fail("Complete target cannot grant another write")).disposition == "target"
    assert git.commit(effect.source_ref) == h1


def test_switch_preserves_ignored_file_that_would_be_overwritten(git_repo, tmp_path, monkeypatch):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    # A clean worktree can contain ignored local files; switch must preserve them.
    exclude = git_repo / ".git" / "info" / "exclude"
    with exclude.open("a") as stream:
        stream.write("\nnext.txt\n")
    (checkout / "next.txt").write_text("Preserve local ignored content\n")
    assert git.run("status", "--porcelain", cwd=checkout).stdout == ""
    with pytest.raises(KernelError, match="git_operation_failed"):
        local.execute(effect, lambda: None)
    assert git.commit(effect.source_ref) == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h0.sha
    assert (checkout / "next.txt").read_text() == "Preserve local ignored content\n"


@pytest.mark.parametrize("code,disposition", [("foreign_local_checkout", "third"), ("local_read_failed", "unknown")])
def test_authentication_failure_never_becomes_absence_or_write(git_repo, tmp_path, monkeypatch, code, disposition):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    called = []

    def reject(*args):
        called.append(code)
        raise KernelError(code)

    monkeypatch.setattr(ownership, "authenticate_prebuilt_workspace", reject, raising=False)
    before = snapshot(tmp_path)
    assert local.reconcile(effect).disposition == disposition
    assert local.execute(effect, lambda: pytest.fail("Unproved state cannot write")).disposition == disposition
    assert called == [code, code] and snapshot(tmp_path) == before


@pytest.mark.parametrize("value", [None, 1])
def test_present_invalid_field_is_sent_to_authenticator(git_repo, tmp_path, monkeypatch, value):
    git, local, effect, checkout, h0, h1, _ = workspace(git_repo, tmp_path, monkeypatch)
    effect = replace(effect, payload={**effect.payload, "prebuilt_workspace": value})
    called = []

    def reject(selected, original):
        assert original.payload["prebuilt_workspace"] is value
        called.append(True)
        raise KernelError("foreign_local_checkout")

    monkeypatch.setattr(ownership, "authenticate_prebuilt_workspace", reject, raising=False)
    assert local.reconcile(effect).disposition == "third" and called == [True]
