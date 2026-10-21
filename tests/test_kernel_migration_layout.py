"""Original checkout layout changes retain exact branch, index and work bytes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agent_run_kernel.cleanup_adapter import _hold_ref
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import MigrationRefused
from agent_run_kernel.migration_layout import detach_layout, restore_layout, verify_layout


def _layout(git_repo, tmp_path):
    git = LocalGit(git_repo)
    owned = tmp_path / "owned"
    owned.mkdir()
    checkout = owned / "ticket"
    ref, sha, uuid = "refs/heads/original", git.commit("HEAD").sha, "a" * 32
    git.run("update-ref", ref, sha)
    git.run("worktree", "add", str(checkout), ref.removeprefix("refs/heads/"))
    assert git.run("symbolic-ref", "HEAD", cwd=checkout).stdout == ref + "\n"
    (git_repo / ".git" / "agent-run-checkout-id").write_text(uuid + "\n")
    layout = {"version": 1, "nonce": "b" * 32, "source_sha256": "c" * 64,
              "source_uuid": uuid, "repository_root": str(git_repo), "checkout": str(checkout),
              "owned_root": str(owned), "common_git_dir": str(git_repo / ".git"),
              "source_ref": ref, "expected_oid": sha, "before_head": "ref: " + ref + "\n",
              "checkout_archive_sha256": "d" * 64}
    return git, checkout, layout


def _observe_layout_children(monkeypatch, checkout):
    owned, reaped, signals = set(), set(), []
    original_popen, original_killpg = subprocess.Popen, os.killpg

    class ObservedPopen(original_popen):
        def __init__(self, arguments, *args, **kwargs):
            super().__init__(arguments, *args, **kwargs)
            self.layout_child = (arguments == ["git", "update-ref", "--no-deref", "--stdin", "-z"]
                                 and kwargs.get("cwd") == checkout)
            if self.layout_child:
                owned.add(self.pid)

        def wait(self, *args, **kwargs):
            result = super().wait(*args, **kwargs)
            if getattr(self, "layout_child", False):
                reaped.add(self.pid)
            return result

    def observed_killpg(pid, signum):
        if pid in owned:
            assert pid not in reaped, "a reaped layout child no longer owns this process group"
            signals.append((pid, signum))
        return original_killpg(pid, signum)

    monkeypatch.setattr(subprocess, "Popen", ObservedPopen)
    monkeypatch.setattr(os, "killpg", observed_killpg)
    return owned, reaped, signals


def test_layout_roundtrip_and_reentry_preserve_refs_index_and_dirty_bytes(git_repo, tmp_path, monkeypatch):
    git, checkout, layout = _layout(git_repo, tmp_path)
    owned, reaped, signals = _observe_layout_children(monkeypatch, checkout)
    (checkout / "staged").write_bytes(b"original staged\0bytes\n")
    git.run("add", "staged", cwd=checkout)
    (checkout / "staged").write_bytes(b"later unstaged bytes\n")
    (checkout / "untracked").write_bytes(b"untracked\0bytes\n")
    index = Path(git.run("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=checkout).stdout.strip())
    before_index = index.read_bytes()
    before_refs = git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout
    saved = tmp_path / "durable-layout.json"
    saved.write_text(json.dumps(layout))
    assert verify_layout(layout, state="attached") == "attached"
    with _hold_ref(git, git.root, layout["source_ref"], layout["expected_oid"]):
        detach_layout(layout)
        assert verify_layout(layout, state="detached") == "detached"
        detach_layout(json.loads(saved.read_text()))  # Reentry after physical commit, before pointer publication.
        restore_layout(json.loads(saved.read_text()))
        assert verify_layout(layout, state="attached") == "attached"
        restore_layout(layout)  # Reentry after inverse commit, before old protocol release.
    assert index.read_bytes() == before_index
    assert git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout == before_refs
    assert (checkout / "staged").read_bytes() == b"later unstaged bytes\n"
    assert (checkout / "untracked").read_bytes() == b"untracked\0bytes\n"
    assert len(owned) == 2 and owned == reaped and signals == []


@pytest.mark.parametrize("race", [False, True], ids=["already_foreign", "before_head_prepare"])
def test_layout_rejects_foreign_same_oid_head_without_rewriting_it(git_repo, tmp_path, monkeypatch, race):
    git, checkout, layout = _layout(git_repo, tmp_path)
    owned, reaped, signals = _observe_layout_children(monkeypatch, checkout)
    foreign = "refs/heads/foreign"
    git.run("update-ref", foreign, layout["expected_oid"])
    if race:
        real_git = shutil.which("git")
        binary = tmp_path / "bin"
        binary.mkdir()
        wrapper = binary / "git"
        wrapper.write_text("#!" + sys.executable + "\nimport os,subprocess,sys\n"
            + f"if os.getcwd()=={str(checkout)!r} and sys.argv[1:]==['update-ref','--no-deref','--stdin','-z']:\n"
            + f" subprocess.run([{real_git!r},'symbolic-ref','HEAD',{foreign!r}],check=True)\n"
            + f"os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    else:
        git.run("symbolic-ref", "HEAD", foreign, cwd=checkout)
    with _hold_ref(git, git.root, layout["source_ref"], layout["expected_oid"]):
        with pytest.raises(MigrationRefused):
            detach_layout(layout)
    assert git.run("symbolic-ref", "HEAD", cwd=checkout).stdout.strip() == foreign
    assert git.commit(foreign).sha == git.commit(layout["source_ref"]).sha == layout["expected_oid"]
    head = Path(git.run("rev-parse", "--path-format=absolute", "--git-path", "HEAD", cwd=checkout).stdout.strip())
    assert not head.with_name("HEAD.lock").exists()
    assert len(owned) == int(race) and owned == reaped and signals == []


@pytest.mark.parametrize("damage", ["source_uuid", "source_ref", "expected_oid", "common_git_dir", "before_head"])
def test_layout_refuses_changed_original_identity(git_repo, tmp_path, damage):
    git, checkout, layout = _layout(git_repo, tmp_path)
    original = git.run("symbolic-ref", "HEAD", cwd=checkout).stdout
    changed = dict(layout)
    changed[damage] = {"source_uuid": "e" * 32, "source_ref": "refs/heads/foreign",
                       "expected_oid": "e" * 40, "common_git_dir": str(tmp_path),
                       "before_head": "ref: refs/heads/foreign\n"}[damage]
    with pytest.raises(MigrationRefused):
        detach_layout(changed)
    assert git.run("symbolic-ref", "HEAD", cwd=checkout).stdout == original
