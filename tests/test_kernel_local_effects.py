"""Real local Git effects, including durable object intent and exact readback."""
from __future__ import annotations

from dataclasses import replace
import base64
import json
import os
import shutil
import sys
from pathlib import Path
import time

import pytest

from agent_run_kernel import Effect, Execution, KernelError
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects


def publication(git_repo: Path, tmp_path: Path) -> tuple[LocalGit, LocalEffects, Effect]:
    git = LocalGit(git_repo)
    git.run("config", "user.name", "Frozen Repository Author")
    git.run("config", "user.email", "frozen@example.invalid")
    base = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    checkout = owned / "delivery"
    git.run("worktree", "add", "--detach", str(checkout), base.sha)
    effect = Effect(
        "publication-original", "ticket:3", "publication_commit", "example/repository",
        "refs/heads/task", "refs/heads/run", base.sha, None, base, base, None,
        None, {"checkout": str(checkout), "owned_root": str(owned),
               "common_git_dir": str(git_repo / ".git"), "integrated_sha": base.sha,
               "integrated_tree": base.tree, "parents": [base.sha], "reuse_sha": None,
               "commit_message": "Publish the reviewed change", "intent_token": "publication-original"},
        Execution("run", "executor", 1),
    )
    return git, LocalEffects(git, owned_root=owned), effect


@pytest.mark.parametrize("crash_boundary", ["prepared", "object_written", "checkout_reset"])
def test_publication_replays_original_object_bytes_after_reopen(
    git_repo: Path, tmp_path: Path, crash_boundary: str,
) -> None:
    git, local, effect = publication(git_repo, tmp_path)
    started = int(time.time())
    witness = local.prepare_publication(effect)
    # This seam's durable caller is represented by an actual file/reopen; the
    # Kernel once-attachment is covered separately through its public interface.
    saved = tmp_path / "original-intent.json"
    saved.write_text(json.dumps(witness))
    assert git.run("cat-file", "--batch-check", input_text=witness["expected_sha"] + "\n").stdout.endswith(" missing\n")
    assert "author Frozen Repository Author <frozen@example.invalid>" in witness["raw_commit"]
    assert "committer Frozen Repository Author <frozen@example.invalid>" in witness["raw_commit"]
    timestamp = int(next(line for line in witness["raw_commit"].splitlines() if line.startswith("author ")).rsplit(" ", 2)[1])
    assert started <= timestamp <= int(time.time())
    if crash_boundary != "prepared":
        git.run("hash-object", "-w", "-t", "commit", "--stdin", input_text=witness["raw_commit"])
    if crash_boundary == "checkout_reset":
        git.run("reset", "--hard", witness["expected_sha"], cwd=Path(effect.payload["checkout"]))
    # Different current author/date must not regenerate the already saved object.
    git.env.update({"GIT_AUTHOR_NAME": "Later", "GIT_COMMITTER_NAME": "Later",
                    "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"})
    effect = replace(effect, payload={**effect.payload, "publication_commit_intent": json.loads(saved.read_text())})
    reopened = LocalEffects(git, owned_root=tmp_path / "work")
    assert reopened.prepare_publication(effect) == witness
    observed = reopened.reconcile(effect)
    assert observed.disposition == ("target" if crash_boundary == "checkout_reset" else "prior")
    calls: list[str] = []
    result = reopened.execute(effect, lambda: calls.append("authorized"))
    assert result.disposition == "target" and result.commit is not None
    assert result.commit.sha == witness["expected_sha"]
    assert git.parents(result.commit.sha) == tuple(effect.payload["parents"])
    assert git.run("cat-file", "commit", result.commit.sha).stdout == witness["raw_commit"]
    assert git.run("rev-parse", "HEAD", cwd=Path(effect.payload["checkout"])).stdout.strip() == result.commit.sha
    assert calls or crash_boundary == "checkout_reset"
    assert reopened.reconcile(effect).disposition == "target"
    assert git.run("for-each-ref", "--format=%(refname)").stdout.splitlines() == ["refs/heads/main"]


def test_local_delivery_resumes_exact_integration_and_retires_owned_resources(
    git_repo: Path, tmp_path: Path,
) -> None:
    git, local, seed = publication(git_repo, tmp_path)
    base = git.commit("HEAD")
    git.run("worktree", "remove", seed.payload["checkout"])
    checkout = Path(seed.payload["checkout"])

    def intent(operation: str, **payload: object) -> Effect:
        return replace(seed, effect_id="original-" + operation, operation=operation,
                       payload={**seed.payload, "intent_token": "original-" + operation, **payload})

    workspace = intent("workspace", create_ref=True)
    guarded: list[str] = []
    assert local.reconcile(workspace).disposition == "prior"
    assert local.execute(workspace, lambda: guarded.append("workspace")).disposition == "target"
    assert git.commit(workspace.source_ref) == base and checkout.is_dir()
    (checkout / "development.txt").write_text("accepted work\n")
    candidate_effect = intent("candidate", parents=[base.sha])
    candidate = local.execute(candidate_effect, lambda: guarded.append("candidate")).commit
    assert candidate is not None and git.parents(candidate.sha) == (base.sha,)
    assert local.reconcile(candidate_effect).commit == candidate
    # A token is not authority for additional or reordered parents.
    unrelated = git.make_commit(base.tree, (), "unrelated root")
    foreign = git.make_commit(candidate.tree, (unrelated.sha, base.sha), "candidate " + candidate_effect.effect_id)
    git.run("reset", "--hard", foreign.sha, cwd=checkout)
    assert local.reconcile(candidate_effect).disposition == "third"
    git.run("reset", "--hard", candidate.sha, cwd=checkout)
    review = intent("validation_checkout", checkout=str(checkout.parent / "review-original"), reviewed_commit=candidate.sha)
    assert local.execute(review, lambda: guarded.append("review")).commit == candidate
    assert git.run("status", "--porcelain", cwd=Path(review.payload["checkout"])).stdout == ""
    (Path(review.payload["checkout"]) / "validation.tmp").write_text("temporary validation output\n")
    review_cleanup = replace(review, operation="validation_cleanup", before_sha=candidate.sha)
    assert local.execute(review_cleanup, lambda: guarded.append("review_cleanup")).disposition == "target"
    assert not Path(review.payload["checkout"]).exists()
    # Independent source and target edits require the original two ordered merges.
    (git_repo / "source.txt").write_text("source work\n")
    git.run("add", "--all")
    git.run("commit", "-m", "source append")
    source = git.commit("HEAD")
    git.run("reset", "--hard", base.sha)
    (git_repo / "target.txt").write_text("target work\n")
    git.run("add", "--all")
    git.run("commit", "-m", "target append")
    target = git.commit("HEAD")
    integration = replace(intent("integrate", source_sha=source.sha), candidate=candidate, base=target)
    middle = git.combine(candidate.sha, source.sha, integration.effect_id + "-source")
    git.install_checkout(checkout, middle.sha)  # actual first merge completed before response loss

    def snapshot() -> dict[str, bytes]:
        return {str(path.relative_to(git_repo)): path.read_bytes()
                for path in (git_repo / ".git").rglob("*") if path.is_file()}

    before = snapshot()
    observed = local.reconcile(integration)
    assert observed.disposition == "prior" and observed.commit == middle
    assert snapshot() == before
    integrated = local.execute(integration, lambda: guarded.append("integration"))
    assert integrated.disposition == "target" and integrated.commit is not None
    assert git.parents(integrated.commit.sha) == (middle.sha, target.sha)
    assert git.parents(middle.sha) == (candidate.sha, source.sha)
    assert integrated.expected_tree == integrated.commit.tree
    assert all((checkout / name).exists() for name in ("development.txt", "source.txt", "target.txt"))
    before = snapshot()
    assert local.reconcile(integration) == integrated
    assert snapshot() == before
    foreign_identity = replace(integration, payload={**integration.payload, "common_git_dir": str(tmp_path)})
    assert local.execute(foreign_identity, lambda: pytest.fail("foreign checkout must not dispatch")).disposition == "third"
    cleanup = intent("cleanup_worktree", expected_head=integrated.commit.sha, branch_ref=seed.source_ref)
    (checkout / "keep-my-work.txt").write_text("not disposable\n")
    assert local.execute(cleanup, lambda: pytest.fail("dirty delivery must survive")).disposition == "third"
    (checkout / "keep-my-work.txt").unlink()
    assert local.execute(cleanup, lambda: guarded.append("cleanup")).disposition == "target"
    assert not checkout.exists()
    ref_cleanup = intent("cleanup_local_ref", expected_head=base.sha, branch_ref=seed.source_ref)
    assert local.execute(ref_cleanup, lambda: guarded.append("ref_cleanup")).disposition == "target"
    assert git.run("for-each-ref", "--format=%(refname)", seed.source_ref).stdout == ""
    assert set(guarded) == {"workspace", "candidate", "review", "review_cleanup", "integration", "cleanup", "ref_cleanup"}


def test_publication_distinguishes_unreadable_object_from_conflict_and_revoked_write(
    git_repo: Path, tmp_path: Path,
) -> None:
    git, local, effect = publication(git_repo, tmp_path)
    witness = local.prepare_publication(effect)
    effect = replace(effect, payload={**effect.payload, "publication_commit_intent": witness})
    wrong = replace(effect, payload={**effect.payload, "publication_commit_intent": {
        **witness, "raw_commit": witness["raw_commit"].replace("Publish the reviewed change", "Unrelated publication")}})
    assert local.execute(wrong, lambda: pytest.fail("conflicting bytes cannot dispatch")).disposition == "third"

    def revoked() -> None:
        raise KernelError("executor_fenced")

    with pytest.raises(KernelError, match="executor_fenced"):
        local.execute(effect, revoked)
    assert git.run("cat-file", "--batch-check", input_text=witness["expected_sha"] + "\n").stdout.endswith(" missing\n")
    assert git.run("rev-parse", "HEAD", cwd=Path(effect.payload["checkout"])).stdout.strip() == effect.before_sha
    # Fail the actual Git command at its process boundary; all earlier checkout
    # and witness reads still run real Git. No internal storage object is mocked.
    executable = shutil.which("git")
    assert executable is not None
    commands = tmp_path / "commands"
    commands.mkdir()
    wrapper = commands / "git"
    wrapper.write_text(f"#!{sys.executable}\nimport os, sys\n"
                       "if 'cat-file' in sys.argv:\n sys.stderr.write('object storage unavailable\\n'); sys.exit(73)\n"
                       f"os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n")
    wrapper.chmod(0o700)
    git.env["PATH"] = str(commands) + os.pathsep + git.env["PATH"]
    unavailable = LocalEffects(git, owned_root=tmp_path / "work")
    assert unavailable.reconcile(effect).disposition == "unknown"
    assert unavailable.execute(effect, lambda: pytest.fail("unknown read cannot authorize replay")).disposition == "unknown"


def test_local_git_uses_bounded_capture_and_its_explicit_environment(tmp_path: Path) -> None:
    commands = tmp_path / "bounded-commands"
    commands.mkdir()
    executable = commands / "git"
    executable.write_text(f"#!{sys.executable}\nimport os, sys\n"
                          "if 'show' in sys.argv:\n sys.stdout.write('x' * (2 * 1024 * 1024))\n"
                          "else:\n sys.stdout.write(os.environ['LOCAL_GIT_ENVIRONMENT'])\n")
    executable.chmod(0o700)
    git = LocalGit(tmp_path)
    git.env.update({"PATH": str(commands), "LOCAL_GIT_ENVIRONMENT": "frozen-local-environment"})
    assert git.run("status").stdout == "frozen-local-environment"
    with pytest.raises(KernelError, match="git_operation_failed|git_output_limit"):
        git.run("show", "HEAD")


@pytest.mark.parametrize("outcome", ["output", "overflow", "timeout"])
def test_selected_remote_environment_redacts_git_process_diagnostics(tmp_path: Path, outcome: str) -> None:
    from agent_run.git_errors import GitError
    from agent_run.git_output import run_git

    commands = tmp_path / "selected-remote-commands"
    commands.mkdir()
    executable = commands / "git"
    executable.write_text(f"#!{sys.executable}\nimport base64, os, sys, time\n"
                          "token = os.environ['GH_TOKEN']\n"
                          "diagnostic = token + ' ' + base64.b64encode(('x-access-token:' + token).encode()).decode()\n"
                          "sys.stderr.write(diagnostic); sys.stderr.flush()\n"
                          "sys.stdout.write(diagnostic); sys.stdout.flush()\n"
                          "if os.environ['GIT_PROBE_OUTCOME'] == 'overflow':\n sys.stdout.write('x' * (2 * 1024 * 1024))\n"
                          "if os.environ['GIT_PROBE_OUTCOME'] == 'timeout':\n time.sleep(10)\n")
    executable.chmod(0o700)
    token = "synthetic-selected-runner-secret"
    environment = {"PATH": str(commands), "GH_TOKEN": token, "GIT_PROBE_OUTCOME": outcome}
    arguments = ["git", "-C", str(tmp_path), "push", "origin", ":refs/heads/owned"]
    if outcome == "output":
        result = run_git(arguments, cwd=tmp_path, environment=environment, timeout=1)
        assert result.returncode == 0
        diagnostic = result.stdout + result.stderr
    else:
        with pytest.raises(GitError, match="transfer limit|timed out") as caught:
            run_git(arguments, cwd=tmp_path, environment=environment, timeout=1)
        diagnostic = str(caught.value) + json.dumps(caught.value.evidence)
    assert "[REDACTED]" in diagnostic
    assert token not in diagnostic
    assert base64.b64encode(f"x-access-token:{token}".encode()).decode() not in diagnostic
