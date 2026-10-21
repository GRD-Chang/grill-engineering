"""Imported ref retirement remains bound to its original clone and local OID."""
import os
from pathlib import Path

import pytest

from agent_run_kernel import Effect, Execution, KernelError
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects


UUID = "a7" * 16


def _original(git_repo: Path, tmp_path: Path) -> tuple[LocalGit, LocalEffects, Effect, Path]:
    git = LocalGit(git_repo)
    base = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    marker = git_repo / ".git" / "agent-run-checkout-id"
    marker.write_text(UUID + "\n", encoding="ascii")
    ref = "refs/heads/agent-run/imported/run"
    git.run("update-ref", ref, base.sha)
    effect = Effect("imported-local-cleanup", "final:imported", "cleanup_local_ref", "example/project",
        ref, "refs/heads/main", base.sha, None, None, base, None, None,
        {"checkout": str(owned / "final"), "owned_root": str(owned), "common_git_dir": str(git_repo / ".git"),
         "branch_ref": ref, "expected_head": base.sha, "source_uuid": UUID}, Execution("imported", "host", 1))
    return git, LocalEffects(git, owned_root=owned), effect, marker


@pytest.mark.parametrize("physical", ["exact", "absent", "wrong", "missing", "symlink", "hardlink", "short", "directory", "symbolic"])
def test_imported_ref_cleanup_requires_the_exact_regular_single_link_uuid_marker(
    git_repo: Path, tmp_path: Path, physical: str,
) -> None:
    git, local, effect, marker = _original(git_repo, tmp_path)
    if physical == "absent":
        git.run("update-ref", "-d", effect.source_ref, effect.before_sha)
    elif physical == "wrong":
        marker.write_text("b8" * 16 + "\n", encoding="ascii")
    elif physical == "missing":
        marker.unlink()
    elif physical in {"symlink", "hardlink"}:
        other = tmp_path / "another-marker"
        other.write_bytes(marker.read_bytes())
        marker.unlink()
        if physical == "symlink":
            marker.symlink_to(other)
        else:
            os.link(other, marker)
    elif physical == "short":
        marker.write_text(UUID, encoding="ascii")
    elif physical == "directory":
        marker.unlink()
        marker.mkdir()
    elif physical == "symbolic":
        git.run("symbolic-ref", effect.source_ref, "refs/heads/main")
    authorized: list[str] = []
    observed = local.reconcile(effect)
    if physical in {"exact", "absent"}:
        assert observed.disposition == ("prior" if physical == "exact" else "target")
        assert local.execute(effect, lambda: authorized.append("current")).disposition == "target"
        assert authorized
        assert git.run("for-each-ref", "--format=%(refname)", effect.source_ref).stdout == ""
    else:
        assert observed.disposition in {"third", "unknown"}
        assert local.execute(effect, lambda: authorized.append("wrong-clone")).disposition in {"third", "unknown"}
        assert authorized == []
        assert git.commit(effect.source_ref).sha == effect.before_sha


@pytest.mark.parametrize("absent", [False, True])
def test_imported_ref_uuid_is_reread_after_authorization_including_absent_noop(
    git_repo: Path, tmp_path: Path, absent: bool,
) -> None:
    git, local, effect, marker = _original(git_repo, tmp_path)
    if absent:
        git.run("update-ref", "-d", effect.source_ref, effect.before_sha)
    assert local.reconcile(effect).disposition == ("target" if absent else "prior")
    calls: list[str] = []

    def authorize() -> None:
        calls.append("current")
        marker.write_text("c9" * 16 + "\n", encoding="ascii")

    with pytest.raises(KernelError, match="local_effect_boundary_changed"):
        local.execute(effect, authorize)
    assert calls
    assert git.run("for-each-ref", "--format=%(refname)", effect.source_ref).stdout.strip() == ("" if absent else effect.source_ref)
