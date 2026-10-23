"""Abandon retires only the original owned checkout under explicit authority."""
from pathlib import Path
import shutil

import pytest

from agent_run.model import Effect, Execution, ResourceWitness
from agent_run.errors import KernelError
from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.local_git import LocalEffects
from agent_run.model import resource_wire
from agent_run.contracts.write_authority import FinalAdmission, WriteAuthority


def _original(git_repo: Path, tmp_path: Path):
    git = LocalGit(git_repo)
    head = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    checkout = owned / "development"
    git.run("worktree", "add", "--detach", str(checkout), head.sha)
    resource = ResourceWitness("ticket:2", 1, "development", str(checkout), str(owned),
                               str(git_repo / ".git"), "refs/heads/task", head.sha)
    return git, head, resource


@pytest.mark.parametrize("case", ["clean", "dirty", "discard", "absent", "foreign", "head", "missing"])
def test_abandon_worktree_preserves_foreign_or_unsaved_work_and_requires_original_authority(
    git_repo: Path, tmp_path: Path, case: str,
) -> None:
    git, head, resource = _original(git_repo, tmp_path)
    checkout = Path(resource.checkout)
    if case in {"dirty", "discard"}:
        (checkout / "unsaved.txt").write_text("Original unsaved work.\n")
    elif case == "absent":
        git.run("worktree", "remove", str(checkout))
    elif case == "foreign":
        resource = ResourceWitness(resource.subject, resource.generation, resource.kind, resource.checkout,
                                   resource.owned_root, str(tmp_path / "foreign.git"), resource.source_ref, head.sha)
    elif case == "head":
        later = git.make_commit(head.tree, (head.sha,), "Later checkout work")
        git.run("reset", "--hard", later.sha, cwd=checkout)
    elif case == "missing":
        shutil.rmtree(checkout)
    effect = Effect("abandon-original", resource.subject, "abandon_worktree", "example/project",
                    resource.source_ref, "refs/heads/run", head.sha, None, None, head, None, None,
                    {"resource": resource_wire(resource), "control_action": "original-action",
                     "discard_worktree": case == "discard"}, Execution("run", "executor", 1))
    authorized: list[str] = []
    local = LocalEffects(git, owned_root=Path(resource.owned_root))
    result = local.execute(effect, WriteAuthority(lambda: lambda: authorized.append("original-action")))
    if case in {"clean", "discard", "absent"}:
        assert result.disposition == "target"
        assert result.raw["resource"] == resource_wire(resource)
        assert result.raw["reason"] == "worktree_absent"
        assert authorized and not checkout.exists()
        assert local.reconcile(effect) == result
    else:
        assert result.disposition == ("unknown" if case == "missing" else "third")
        assert result.raw["reason"] == {"dirty": "worktree_dirty", "foreign": "foreign_common_git_dir",
                                         "head": "worktree_registration_changed", "missing": "registered_path_missing"}[case]
        assert authorized == []
        assert checkout.exists() == (case != "missing")
        if case == "dirty":
            assert (checkout / "unsaved.txt").read_text() == "Original unsaved work.\n"


def test_abandon_absent_checkout_rechecks_after_authorization(git_repo: Path, tmp_path: Path) -> None:
    git, head, resource = _original(git_repo, tmp_path)
    checkout = Path(resource.checkout)
    git.run("worktree", "remove", str(checkout))
    effect = Effect("abandon-original", resource.subject, "abandon_worktree", "example/project",
                    resource.source_ref, "refs/heads/run", head.sha, None, None, head, None, None,
                    {"resource": resource_wire(resource), "control_action": "original-action", "discard_worktree": False},
                    Execution("run", "executor", 1))
    authorized: list[str] = []

    prepared: list[str] = []
    def prepare() -> FinalAdmission:
        prepared.append("original-action")
        checkout.mkdir()
        (checkout / "foreign.txt").write_text("Preserve newly arrived directory.\n")
        return lambda: authorized.append("original-action")

    with pytest.raises(KernelError, match="local_effect_boundary_changed"):
        LocalEffects(git, owned_root=Path(resource.owned_root)).execute(effect, WriteAuthority(prepare))
    assert prepared == ["original-action"]
    assert authorized == []
    assert (checkout / "foreign.txt").read_text() == "Preserve newly arrived directory.\n"
