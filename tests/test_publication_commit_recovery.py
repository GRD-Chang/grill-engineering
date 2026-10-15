"""真实 publication commit 已安装、业务结果未保存时的恢复保障。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from support.delivery_agents import PassAgents, issue
from support.seeding import write_fixture
from support.state_commits import observe_run_commits


class CrashBeforePublicationSave(StateStore):
    """只拒绝首次 publication 结果落盘，先前事实仍由 StateStore 保存。"""

    def __init__(self, root: Path, subject: str, *, crash: bool = True) -> None:
        super().__init__(root)
        self.subject = subject
        self.publication_sha: str | None = None
        self.crash = crash

    def before_save(self, run_id: str, state: dict[str, Any]) -> None:
        job = state.get(self.subject)
        if (
            self.crash and self.publication_sha is None
            and isinstance(job, dict)
            and job.get("phase") == "publishing"
            and isinstance(job.get("publication_sha"), str)
        ):
            self.publication_sha = job["publication_sha"]
            raise SimulatedProcessCrash("publication commit installed before result save")


@pytest.mark.parametrize(
    ("parent_only", "window"),
    [(False, "result_save"), (True, "result_save"),
     (False, "object_response"), (True, "install")],
    ids=["ticket-save", "parent-save", "ticket-object-response", "parent-install"],
)
def test_installed_publication_commit_resume_preserves_accepted_work(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, parent_only: bool, window: str,
) -> None:
    subject = "parent_job" if parent_only else "active_ticket_job"
    fixture = write_fixture(
        git_repo / "github.json", issues={} if parent_only else {"3": issue(3)},
    )
    states = CrashBeforePublicationSave(
        git_repo / ".agent-run", subject, crash=window == "result_save",
    )
    observe_run_commits(monkeypatch, states, before=states.before_save)
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states,
    ).start(1)
    run_id = str(state["run_id"])
    checkout = states.root / "worktrees" / run_id / ("parent" if parent_only else "ticket-3")
    agents = PassAgents(checkout)
    engine_type = ParentDeliveryEngine if parent_only else TicketDeliveryEngine

    # Count actual Git object writes, rather than calls to the idempotent wrapper.
    publication_commits: list[str] = []
    original_run = GitRepository._run_in

    def observe_git(
        self: GitRepository, path: Path, *args: str,
        input: str | None = None, timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if window == "install" and args[:2] == ("reset", "--hard") and publication_commits:
            if args[2] == publication_commits[0] and not states.publication_sha:
                states.publication_sha = publication_commits[0]
                raise SimulatedProcessCrash("publication commit before result save: install lost")
        result = original_run(self, path, *args, input=input, timeout=timeout)
        if args[:2] == ("hash-object", "-w") and agents.publication_requests:
            publication_commits.append(result.stdout.strip())
            if window == "object_response" and not states.publication_sha:
                states.publication_sha = publication_commits[0]
                raise SimulatedProcessCrash("publication commit before result save: response lost")
        return result

    monkeypatch.setattr(GitRepository, "_run_in", observe_git)
    git = GitRepository(git_repo)
    with pytest.raises(SimulatedProcessCrash, match="before result save"):
        engine_type(
            git=git, states=states, github=FixtureGitHubPublisher(fixture, git), agents=agents,
        ).deliver(run_id)

    interrupted = StateStore(states.root).load_current_run(run_id)
    assert interrupted is not None
    before = deepcopy(interrupted[subject])
    assert before["phase"] == "accepted"
    assert "publication_sha" not in before
    assert before["pending_publication_result"]
    attempt = deepcopy(before["pending_semantic_attempt"])
    assert attempt["role"] == "publication"
    assert publication_commits == [states.publication_sha]
    checkout_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True,
    ).stdout.strip()
    written = str(states.publication_sha)
    assert checkout_head == (written if window == "result_save" else before["candidate_sha"])
    assert git.commit_parents(written) == [before["base_sha"]]
    assert git.resolve(f"{written}^{{tree}}") == git.resolve(f"{before['candidate_sha']}^{{tree}}")
    intent = before["publication_commit_intent"]
    assert intent["expected_sha"] == written
    assert subprocess.run(
        ["git", "cat-file", "commit", written], cwd=checkout,
        capture_output=True, text=True, check=True,
    ).stdout == intent["raw_commit"]

    # Fresh readers and engines discard every in-memory post-write result.
    restored = StateStore(states.root)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+0000")
    Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), restored).resume(
        run_id,
    )
    restored_git = GitRepository(git_repo)
    completed = engine_type(
        git=restored_git, states=restored,
        github=FixtureGitHubPublisher(fixture, restored_git), agents=agents,
    ).deliver(run_id)
    assert completed["status"] == ("parent_approval_pending" if parent_only else "ticket_completed")
    after = completed[subject]
    assert after["publication_sha"] == written
    assert publication_commits == [written]
    assert "publication_commit_intent" not in after
    assert len(agents.publication_requests) == 1
    assert len(agents.development_requests) == 1
    assert agents.review_count == 1
    assert after["candidate_sha"] == before["candidate_sha"]
    assert after["acceptance_record"] == before["acceptance_record"]
    assert after["publication_attempts"] == before["publication_attempts"] == 1
    closed_attempts = [
        item for item in after["semantic_attempt_history"]
        if item["role"] == "publication"
    ]
    assert len(closed_attempts) == 1
    assert closed_attempts[0]["attempt_id"] == attempt["attempt_id"]
    assert closed_attempts[0]["currentness_boundary_fingerprint"] == attempt["currentness_boundary_fingerprint"]
    assert closed_attempts[0]["outcome"] == "publication_artifact"
    assert [
        item for item in after["semantic_attempt_history"] if item["role"] != "publication"
    ] == before["semantic_attempt_history"]
    assert after["publication"] == before["pending_publication_result"]
    persisted = restored.load_current_run(run_id)
    assert persisted is not None
    assert persisted[subject]["publication_sha"] == written
    assert persisted[subject]["acceptance_record"] == before["acceptance_record"]


@pytest.mark.parametrize("changed", ["tree", "parents", "message", "raw_bytes", "sha"])
def test_publication_intent_mismatch_rejects_before_any_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    intent: dict[str, Any] = {}
    saved: list[dict[str, Any]] = []
    sha = git.create_publication_commit(
        git_repo, candidate_sha=base, base_sha=base, message="delivered",
        publication_intent=intent, save_intent=lambda: saved.append(deepcopy(intent)),
    )
    assert saved[0]["expected_sha"] == sha
    if changed == "tree":
        intent["tree_sha"] = "invalid-tree"
    elif changed == "parents":
        intent["parents"] = [sha]
    elif changed == "raw_bytes":
        intent["raw_commit"] = intent["raw_commit"].replace("delivered", "foreign")
    elif changed == "sha":
        intent["expected_sha"] = base
    writes: list[tuple[str, ...]] = []
    original = GitRepository._run_in

    def observe(
        self: GitRepository, path: Path, *args: str,
        input: str | None = None, timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] in {"reset", "commit-tree"} or args[:2] == ("hash-object", "-w"):
            writes.append(args)
        return original(self, path, *args, input=input, timeout=timeout)

    monkeypatch.setattr(GitRepository, "_run_in", observe)
    with pytest.raises(GitError, match="intent"):
        git.create_publication_commit(
            git_repo, candidate_sha=base, base_sha=base,
            message="changed" if changed == "message" else "delivered",
            publication_intent=intent, save_intent=lambda: saved.append(deepcopy(intent)),
        )
    assert writes == []
    assert len(saved) == 1
    assert git.checkout_head(git_repo) == sha


def test_diverged_publication_response_loss_reuses_exact_two_parent_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    default = git._run_in(
        git_repo, "commit-tree", f"{base}^{{tree}}", "-m", "default",
    ).stdout.strip()
    candidate = git._run_in(
        git_repo, "commit-tree", f"{base}^{{tree}}", "-p", base, "-p", default,
        "-m", "accepted merge resolution",
    ).stdout.strip()
    intent: dict[str, Any] = {}
    persisted: list[dict[str, Any]] = []
    writes: list[str] = []
    original = GitRepository._run_in

    def lose_response(
        self: GitRepository, path: Path, *args: str,
        input: str | None = None, timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = original(self, path, *args, input=input, timeout=timeout)
        if args[:2] == ("hash-object", "-w"):
            writes.append(result.stdout.strip())
            assert persisted[0]["expected_sha"] == writes[0]
            if len(writes) == 1:
                raise SimulatedProcessCrash("commit object response lost")
        return result

    monkeypatch.setattr(GitRepository, "_run_in", lose_response)
    with pytest.raises(SimulatedProcessCrash, match="object response"):
        git.create_publication_commit(
            git_repo, candidate_sha=candidate, published_sha=base,
            base_sha=default, message="published merge resolution\n\n",
            publication_intent=intent,
            save_intent=lambda: persisted.append(deepcopy(intent)),
        )
    restored = GitRepository(git_repo)
    publication = restored.create_publication_commit(
        git_repo, candidate_sha=candidate, published_sha=base,
        base_sha=default, message="published merge resolution\n\n",
        publication_intent=deepcopy(persisted[0]),
        save_intent=lambda: pytest.fail("persisted intent must be reused"),
    )
    assert writes == [publication]
    assert restored.commit_parents(publication) == [base, default]
    assert restored.resolve(f"{publication}^{{tree}}") == restored.resolve(f"{candidate}^{{tree}}")
    assert restored.checkout_head(git_repo) == publication


@pytest.mark.parametrize("encoding", ["UTF-8", "ISO-8859-1"])
def test_publication_intent_bytes_match_native_commit_tree_format(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, encoding: str,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+0000")
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    configured = git._run_in(git_repo, "config", "i18n.commitEncoding", encoding)
    assert configured.returncode == 0
    message = "feat: 交付\n\n原始正文与末尾空行\n\n"
    native = git.create_publication_commit(
        git_repo, candidate_sha=base, base_sha=base, message=message,
    )
    reset = git._run_in(git_repo, "reset", "--hard", base)
    assert reset.returncode == 0
    intent: dict[str, Any] = {}
    persisted: list[dict[str, Any]] = []
    recovered = git.create_publication_commit(
        git_repo, candidate_sha=base, base_sha=base, message=message,
        publication_intent=intent, save_intent=lambda: persisted.append(deepcopy(intent)),
    )
    assert recovered == native == persisted[0]["expected_sha"]
