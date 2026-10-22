"""Physical completion using real source production, import and authentication.

The imported fixture and its Effect constructor are shared with the source
owner. Authorization callbacks exercise the physical write boundary; these
tests do not claim to exercise Core's dispatch/lease admission.
"""
import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.migration_run_install import install_imported_facts
from agent_run_kernel.migration_run_ownership import authenticate_prebuilt_workspace
from agent_run_kernel.model import KernelError
from test_kernel_idle_prebuilt import idle_copy as idle_copy, _effect


@pytest.fixture
def installed(idle_copy, request):
    snapshot, options, git, target, h0 = idle_copy
    original = snapshot.source.read_bytes(), snapshot.path.read_bytes()
    prebuilt, equal = getattr(request, "param", (True, False))
    checkout = options["checkout"]
    if prebuilt:
        git.run("worktree", "add", "--detach", str(checkout), h0)
    kernel = install_imported_facts(read_initial_import(snapshot, **options), target)
    if not equal:
        (git.root / "dependency.txt").write_text("Dependency-visible H1\n")
        git.run("add", "dependency.txt")
        git.run("commit", "-m", "Synthetic dependency completion")
    h1 = git.commit("HEAD").sha
    effect = _effect(kernel, git, "ticket:2", h1)
    assert effect.payload["prebuilt_workspace"]["workspace_oid"] == h0
    assert effect.before_sha == h1
    local = LocalEffects(git, owned_root=checkout.parent)
    yield git, local, effect, checkout, h0, h1
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes()) == original


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("ref_present,at_target", [(False, False), (True, False), (False, True), (True, True)])
def test_real_authenticated_prebuilt_partial_states(installed, ref_present, at_target):
    git, local, effect, checkout, h0, h1 = installed
    assert h0 != h1
    if ref_present:
        git.run("update-ref", effect.source_ref, h1, "0" * len(h1))
    if at_target:
        git.run("switch", "--detach", h1, cwd=checkout)
    before = files(checkout.parent.parent)
    binding = authenticate_prebuilt_workspace(git, effect)
    assert binding == effect.payload["prebuilt_workspace"]
    assert binding is not effect.payload["prebuilt_workspace"]
    assert local.reconcile(effect).disposition == ("target" if ref_present and at_target else "prior")
    assert files(checkout.parent.parent) == before
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert len(guards) == int(not ref_present) + int(not at_target)
    assert git.commit(effect.source_ref).sha == h1
    assert git.commit("HEAD").sha == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h1
    assert (checkout / "dependency.txt").read_text() == "Dependency-visible H1\n"
    assert effect.before_sha == h1 and binding["workspace_oid"] == h0


@pytest.mark.parametrize("installed", [(False, False)], indirect=True)
@pytest.mark.parametrize("ref_present,at_target", [(False, False), (True, False), (False, True), (True, True)])
def test_real_authenticated_lazy_states(installed, ref_present, at_target):
    git, local, effect, checkout, h0, h1 = installed
    binding = effect.payload["prebuilt_workspace"]
    assert binding["before_head"] is None and binding["checkout_status"] is None
    if at_target:
        git.run("worktree", "add", "--detach", str(checkout), h1)
    if ref_present:
        git.run("update-ref", effect.source_ref, h1, "0" * len(h1))
    assert local.reconcile(effect).disposition == ("target" if ref_present and at_target else "prior")
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert len(guards) == int(not ref_present) + int(not at_target)
    assert (checkout / "dependency.txt").read_text() == "Dependency-visible H1\n"


@pytest.mark.parametrize("installed", [(True, True)], indirect=True)
def test_real_authenticated_equal_h0_h1_creates_only_ref(installed):
    git, local, effect, checkout, h0, h1 = installed
    assert h0 == h1
    head = git.root / ".git" / "worktrees" / checkout.name / "HEAD"
    before = head.read_bytes()
    guards = []
    assert local.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert guards == ["write"] and head.read_bytes() == before
    assert git.commit(effect.source_ref).sha == h1


def test_real_authenticated_second_write_fence_and_reentry(installed):
    git, local, effect, checkout, h0, h1 = installed
    guards = []

    def authorize():
        guards.append("write")
        if len(guards) == 2:
            raise KernelError("executor_fenced")

    with pytest.raises(KernelError, match="executor_fenced"):
        local.execute(effect, authorize)
    assert guards == ["write", "write"]
    assert git.commit(effect.source_ref).sha == h1
    assert git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == h0
    assert local.reconcile(effect).disposition == "prior"
    guards.clear()
    assert LocalEffects(git, owned_root=checkout.parent).execute(effect, lambda: guards.append("write")).disposition == "target"
    assert guards == ["write"]


@pytest.mark.parametrize("operation,disposition", [("update-ref", "prior"), ("switch", "target")])
def test_real_authenticated_write_response_loss(installed, monkeypatch, operation, disposition):
    git, local, effect, checkout, h0, h1 = installed
    original = LocalGit.run

    def run(self, *arguments, **kwargs):
        result = original(self, *arguments, **kwargs)
        if arguments[:1] == (operation,):
            raise OSError("Actual write completed; response unavailable")
        return result

    with monkeypatch.context() as context:
        context.setattr(LocalGit, "run", run)
        with pytest.raises(OSError, match="response unavailable"):
            local.execute(effect, lambda: None)
    assert git.commit(effect.source_ref).sha == h1
    reopened = LocalEffects(git, owned_root=checkout.parent)
    assert reopened.reconcile(effect).disposition == disposition
    guards = []
    assert reopened.execute(effect, lambda: guards.append("write")).disposition == "target"
    assert len(guards) == int(operation == "update-ref")


@pytest.mark.parametrize("change", ["dirty", "different-head", "different-ref"])
def test_real_authenticated_external_change_refuses_writes(installed, change):
    git, local, effect, checkout, h0, h1 = installed
    if change == "dirty":
        (checkout / "local.txt").write_text("Keep external work\n")
    elif change == "different-head":
        h2 = git.run("commit-tree", git.commit(h1).tree, "-p", h1, input_text="Other local work\n").stdout.strip()
        git.run("switch", "--detach", h2, cwd=checkout)
    else:
        git.run("update-ref", effect.source_ref, h0, "0" * len(h0))
    before = files(checkout.parent.parent)
    assert local.reconcile(effect).disposition == "third"
    assert local.execute(effect, lambda: pytest.fail("External state cannot authorize writes")).disposition == "third"
    assert files(checkout.parent.parent) == before


def test_real_authenticator_read_failure_remains_unknown(installed, monkeypatch):
    git, local, effect, checkout, h0, h1 = installed
    original = LocalGit.run

    def run(self, *arguments, **kwargs):
        if arguments[:2] == ("worktree", "list"):
            raise KernelError("git_operation_failed", "Synthetic Git read failure")
        return original(self, *arguments, **kwargs)

    monkeypatch.setattr(LocalGit, "run", run)
    result = local.reconcile(effect)
    assert result.disposition == "unknown" and result.raw["reason"] == "local_read_failed"
    assert local.execute(effect, lambda: pytest.fail("Read failure cannot authorize writes")).disposition == "unknown"
