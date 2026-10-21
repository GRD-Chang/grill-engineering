"""Executable synthetic-GitHub/Agent adapter, backed by real Git and SQLite.

This entire module is exploration cost, not a production GitHub/Worker adapter.
Its scripts control external outcomes, never internal kernel transitions.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .effects import LocalGit
from .cleanup_adapter import cleanup_local_ref, cleanup_worktree, inspect_local_ref, inspect_worktree
from .kernel import Kernel
from .model import (AgentCall, Checks, Commit, Delivery, DeliveryKind, Effect, EffectResult,
                    KernelError, Policy, PullRequest, Requirements, Role, RunSpec, Step, World, wire_json)


class LostResponse(OSError):
    pass


def review_result(outcome: str) -> dict[str, Any]:
    return {"checks": {name: {"status": "fail" if name == "spec" and outcome == "fail" else
                             "blocked" if name == "spec" and outcome == "blocked" else "pass",
                             "evidence": "Known synthetic acceptance evidence",
                             "findings": ["The required behavior is missing; implement it and rerun the example."]
                             if name == "spec" and outcome == "fail" else []}
                       for name in ("e2e", "standards", "spec")}}


def _pr(data: dict[str, Any]) -> PullRequest:
    return PullRequest(**{**data, "head": Commit(**data["head"]), "base": Commit(**data["base"]),
                          "merged": None if data["merged"] is None else Commit(**data["merged"])})


class OfflinePort:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.git = LocalGit(self.root / "authority")
        self.database = self.root / "remote.sqlite3"
        self.lose_response: set[str] = set()
        self.force_read: dict[str, str] = {}

    @classmethod
    def create(cls, root: Path, initial_git: Mapping[str, Any] | None = None) -> OfflinePort:
        authority = root / "authority"
        authority.mkdir(parents=True, exist_ok=False)
        port = cls(root)
        if initial_git is not None:
            port.git.env.update({"GIT_AUTHOR_NAME": initial_git["author_name"], "GIT_AUTHOR_EMAIL": initial_git["author_email"],
                                 "GIT_COMMITTER_NAME": initial_git["author_name"], "GIT_COMMITTER_EMAIL": initial_git["author_email"],
                                 "GIT_AUTHOR_DATE": initial_git["date"], "GIT_COMMITTER_DATE": initial_git["date"]})
        port.git.run("init", "--initial-branch=main")
        port.git.run("config", "maintenance.auto", "false")
        port.git.run("config", "gc.auto", "0")
        initial_files = {"README.md": "Synthetic repository, no user data.\n"} if initial_git is None else initial_git["initial_files"]
        for name, body in initial_files.items():
            path = authority / name
            if not path.resolve().is_relative_to(authority.resolve()) or ".git" in path.parts:
                raise KernelError("invalid_fixture_path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        port.git.run("add", "--all")
        port.git.run("commit", "-m", "initial fixture" if initial_git is None else initial_git["initial_message"])
        with sqlite3.connect(port.database) as db:
            db.executescript("""
                CREATE TABLE prs(number INTEGER PRIMARY KEY,subject TEXT UNIQUE,body BLOB NOT NULL);
                CREATE TABLE receipts(id TEXT PRIMARY KEY,body BLOB NOT NULL);
                CREATE TABLE writes(position INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,subject TEXT,intent TEXT,facts BLOB);
                CREATE TABLE closed(issue INTEGER PRIMARY KEY,intent TEXT,actor TEXT,merge_sha TEXT);
                CREATE TABLE comments(intent TEXT PRIMARY KEY,issue INTEGER,actor TEXT,body BLOB);
                CREATE TABLE refs(ref TEXT PRIMARY KEY,sha TEXT NOT NULL);
                CREATE TABLE links(issue INTEGER PRIMARY KEY,ref TEXT NOT NULL,sha TEXT NOT NULL,intent TEXT NOT NULL);
            """)
            db.execute("INSERT INTO refs VALUES (?,?)", ("refs/heads/main", port.git.commit("HEAD").sha))
        return port

    def prepare(self, kind: DeliveryKind, *, policy: Policy | None = None,
                ticket_number: int = 2, repository: str = "example/repository",
                completed_ticket: Mapping[str, Any] | None = None) -> tuple[RunSpec, Delivery]:
        base = self.git.commit("refs/heads/main")
        source = base
        key = {DeliveryKind.TICKET: f"ticket:{ticket_number}", DeliveryKind.PARENT_ONLY: "parent:1", DeliveryKind.FINAL_RUN: "final:1"}[kind]
        source_ref = "refs/heads/run" if kind == DeliveryKind.FINAL_RUN else "refs/heads/task"
        target_ref = "refs/heads/run" if kind == DeliveryKind.TICKET else "refs/heads/main"
        if kind == DeliveryKind.FINAL_RUN:
            # This is copied/synthetic completed-Ticket input, not a new Agent result.
            if completed_ticket is not None:
                source = self.git.make_commit(base.tree, (base.sha,), completed_ticket["commit_message"])
            else:
                self.git.run("switch", "-c", "fixture-completed-ticket")
                (self.git.root / "ticket.txt").write_text("Completed synthetic Ticket.\n")
                self.git.run("add", "--all")
                self.git.run("commit", "-m", "completed fixture ticket")
                source = self.git.commit("HEAD")
                self.git.run("switch", "main")
        self.git.run("update-ref", source_ref, source.sha)
        self.git.run("update-ref", target_ref, base.sha)
        with sqlite3.connect(self.database) as db:
            if kind == DeliveryKind.FINAL_RUN:
                db.execute("INSERT OR IGNORE INTO refs VALUES (?,?)", (source_ref, source.sha))
            db.execute("INSERT OR IGNORE INTO refs VALUES (?,?)", (target_ref, base.sha))
        checkout = self.root / "work" / key.replace(":", "-")
        checkout.parent.mkdir(exist_ok=True)
        self.git.run("worktree", "add", "--detach", str(checkout), source.sha)
        delivery = Delivery(key, kind, ticket_number if kind == DeliveryKind.TICKET else 1, "subject-v1", source,
                            source_ref, target_ref, completion_revision="no-tickets", checkout=str(checkout),
                            owned_root=str(checkout.parent.resolve()), common_git_dir=str((self.git.root / ".git").resolve()))
        spec = RunSpec("offline-run", repository, 1, "parent-v1", "graph-v1", (delivery,), policy or Policy())
        return spec, delivery

    def pull_request(self, subject: str) -> PullRequest | None:
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT body FROM prs WHERE subject=?", (subject,)).fetchone()
        if row is None:
            return None
        pr = _pr(json.loads(row[0]))
        if pr.state == "MERGED":
            return pr
        # GitHub's base/head are live remote facts, not frozen intent fields.
        return PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref,
                           self.remote_commit(pr.source_ref) or pr.head, self.remote_commit(pr.target_ref) or pr.base, pr.title, pr.body,
                           pr.state, pr.actor, pr.merged)

    def world(self, spec: RunSpec, delivery: Delivery, checks_result: str = "none") -> World:
        pr = self.pull_request(delivery.key)
        checks = None
        if pr is not None:
            details = () if checks_result == "none" else (("required", checks_result),)
            checks = Checks(pr.number, pr.head.sha, checks_result, details,
                            "Raw synthetic failed job log" if checks_result == "fail" else "")
        source = self.remote_commit(delivery.source_ref)
        target = self.remote_commit(delivery.target_ref)
        if target is None:
            raise KernelError("remote_target_missing")
        return World(spec.parent_revision, spec.graph_revision, delivery.revision,
                     target, source, pr, checks,
                     delivery.completion_revision,
                     ((target.sha, source.sha),) if source is not None and self.git.is_ancestor(target.sha, source.sha) else ())

    def ref_commit(self, ref: str) -> Commit | None:
        result = self.git.run("for-each-ref", "--format=%(refname)%09%(objectname)", ref)
        heads = [line.partition("\t")[2] for line in result.stdout.splitlines() if line.partition("\t")[0] == ref]
        return None if not heads else self.git.commit(heads[0])

    def remote_refs(self) -> dict[str, str]:
        with sqlite3.connect(self.database) as db:
            return dict(db.execute("SELECT ref,sha FROM refs ORDER BY ref"))

    def remote_commit(self, ref: str) -> Commit | None:
        head = self.remote_refs().get(ref)
        return None if head is None else self.git.commit(head)

    def _remote_cas(self, ref: str, before: str, after: str) -> None:
        with sqlite3.connect(self.database) as db:
            self._remote_cas_in(db, ref, before, after)

    def _remote_cas_in(self, db: sqlite3.Connection, ref: str, before: str, after: str) -> None:
        row = db.execute("SELECT sha FROM refs WHERE ref=?", (ref,)).fetchone()
        if row is not None and row[0] not in {before, after}:
            raise KernelError("remote_compare_and_swap")
        db.execute("INSERT INTO refs VALUES (?,?) ON CONFLICT(ref) DO UPDATE SET sha=excluded.sha", (ref, after))

    def _write_pr(self, subject: str, pr: PullRequest) -> None:
        with sqlite3.connect(self.database) as db:
            self._write_pr_in(db, subject, pr)

    def _write_pr_in(self, db: sqlite3.Connection, subject: str, pr: PullRequest) -> None:
        db.execute("INSERT INTO prs VALUES (?,?,?) ON CONFLICT(number) DO UPDATE SET body=excluded.body",
                   (pr.number, subject, wire_json(asdict(pr))))

    def _receipt(self, effect: Effect, result: EffectResult) -> None:
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?)", (effect.effect_id, wire_json(asdict(result))))

    def execute(self, effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        operation = effect.operation
        payload = effect.payload
        git = LocalGit(self.git.root, authorize)
        git.env = self.git.env.copy()
        commit: Commit | None = None
        pr: PullRequest | None = None
        expected: str | None = None
        raw: dict[str, Any] = {}
        outcome: EffectResult | None = None
        try:
            if operation == "cleanup_worktree":
                outcome = cleanup_worktree(git, effect)
            elif operation == "cleanup_local_ref":
                outcome = cleanup_local_ref(git, effect)
            elif operation == "cleanup_remote_ref":
                outcome = self._inspect_remote_cleanup(effect)
                if outcome.disposition == "prior":
                    if authorize is not None:
                        authorize()
                    with sqlite3.connect(self.database) as db:
                        db.execute("DELETE FROM refs WHERE ref=? AND sha=?", (payload["branch_ref"], payload["expected_head"]))
                    outcome = self._inspect_remote_cleanup(effect)
            elif operation == "issue_link":
                if authorize is not None:
                    authorize()
                with sqlite3.connect(self.database) as db:
                    db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?)", (payload["issue"], payload["ref"], payload["source_head"], effect.effect_id))
                outcome = EffectResult("target", raw={"acknowledged_status": "linked"})
            elif operation == "workspace":
                checkout = Path(payload["checkout"])
                if payload["create_ref"]:
                    existing = self.ref_commit(effect.source_ref)
                    if existing is None:
                        git.run("update-ref", effect.source_ref, effect.before_sha, "0" * len(effect.before_sha))
                    elif existing.sha != effect.before_sha:
                        raise KernelError("workspace_ref_changed")
                if not checkout.exists():
                    git.run("worktree", "add", "--detach", str(checkout), effect.before_sha)
                common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout).stdout.strip()
                commit = git.commit(git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip())
                if Path(common).resolve() != (git.root / ".git").resolve() or commit.sha != effect.before_sha:
                    raise KernelError("foreign_workspace")
            elif operation == "validation_checkout":
                checkout = Path(payload["checkout"])
                if not checkout.exists():
                    git.run("worktree", "add", "--detach", str(checkout), payload["reviewed_commit"])
                self._validation_identity(git, checkout, payload["reviewed_commit"])
                commit = git.commit(payload["reviewed_commit"])
            elif operation == "validation_cleanup":
                checkout = Path(payload["checkout"])
                if checkout.exists():
                    self._validation_identity(git, checkout)
                    git.run("worktree", "remove", "--force", str(checkout))
            elif operation == "candidate":
                commit, no_change = git.candidate(Path(payload["checkout"]), effect.before_sha, effect.effect_id)
                raw = {"parents": git.parents(commit.sha), "no_change": no_change}
            elif operation == "integrate":
                if effect.candidate is None:
                    raise KernelError("missing_candidate")
                commit = effect.candidate
                for suffix, target in (("source", payload["source_sha"]), ("target", effect.base.sha)):
                    if target:
                        try:
                            commit = git.combine(commit.sha, target, effect.effect_id + "-" + suffix)
                        except KernelError as error:
                            if error.code != "integration_failed":
                                raise
                            raw = git.conflict_checkout(Path(payload["checkout"]), target)
                            self._receipt(effect, EffectResult("failure", commit=self.git.commit(str(raw["head"])), raw=raw))
                            return None
                        git.install_checkout(Path(payload["checkout"]), commit.sha)
                expected = commit.tree
                raw = {"parents": git.parents(commit.sha)}
            elif operation == "publication_commit":
                if payload["reuse_sha"]:
                    commit = git.commit(payload["reuse_sha"])
                else:
                    commit = git.make_commit(payload["integrated_tree"], payload["parents"], payload["commit_message"])
                raw = {"parents": git.parents(commit.sha)}
            elif operation in {"push", "run_branch"}:
                if effect.after_sha is None:
                    raise KernelError("missing_write_target")
                # One simulated remote request: independently stored remote
                # facts commit atomically. Git objects/refs are its local mirror.
                # A crash across those two storage systems remains unknown,
                # never a receipt-only proof that the request succeeded.
                with sqlite3.connect(self.database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    prior = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.source_ref,)).fetchone()
                    if prior is not None and prior[0] not in {effect.before_sha, effect.after_sha}:
                        raise KernelError("remote_compare_and_swap")
                    git.compare_and_swap(effect.source_ref, effect.before_sha, effect.after_sha)
                    self._remote_cas_in(db, effect.source_ref, effect.before_sha, effect.after_sha)
                commit = git.commit(effect.after_sha)
            elif operation == "pr_write":
                if authorize is not None:
                    authorize()
                prior = self.pull_request(effect.subject)
                if prior is not None and prior.state != "OPEN":
                    raise KernelError("closed_pull_request")
                with sqlite3.connect(self.database) as db:
                    number = prior.number if prior else db.execute("SELECT coalesce(max(number),0)+1 FROM prs").fetchone()[0]
                pr = PullRequest(number, effect.repository, effect.source_ref, effect.target_ref,
                                 self.git.commit(str(self.remote_refs()[effect.source_ref])), self.git.commit(str(self.remote_refs()[effect.target_ref])),
                                 payload["title"], payload["body"], actor=effect.actor)
                self._write_pr(effect.subject, pr)
            elif operation == "merge":
                pr = self.pull_request(effect.subject)
                if pr is None or pr.head.sha != effect.after_sha or pr.base.sha != effect.before_sha or pr.state != "OPEN":
                    raise KernelError("merge_compare_and_swap")
                commit = git.make_commit(pr.head.tree, payload["parents"], payload["commit_message"])
                pr = PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref, pr.head, pr.base,
                                 pr.title, pr.body, "MERGED", effect.actor, commit)
                with sqlite3.connect(self.database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    source = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.source_ref,)).fetchone()
                    target = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.target_ref,)).fetchone()
                    if source != (effect.after_sha,) or target != (effect.before_sha,):
                        raise KernelError("merge_compare_and_swap")
                    local = self.ref_commit(effect.target_ref)
                    if local is None:
                        raise KernelError("merge_mirror_missing")
                    if local.sha != effect.before_sha:
                        if not git.is_ancestor(local.sha, effect.before_sha):
                            raise KernelError("merge_mirror_changed")
                        git.compare_and_swap(effect.target_ref, local.sha, effect.before_sha)
                    git.compare_and_swap(effect.target_ref, effect.before_sha, commit.sha)
                    self._remote_cas_in(db, effect.target_ref, effect.before_sha, commit.sha)
                    self._write_pr_in(db, effect.subject, pr)
            elif operation == "completion_comment":
                if authorize is not None:
                    authorize()
                with sqlite3.connect(self.database) as db:
                    db.execute("INSERT OR IGNORE INTO comments VALUES (?,?,?,?)",
                               (effect.effect_id, payload["issue"], effect.actor, wire_json(dict(payload))))
            elif operation == "close":
                if authorize is not None:
                    authorize()
                with sqlite3.connect(self.database) as db:
                    db.execute("INSERT OR IGNORE INTO closed VALUES (?,?,?,?)",
                               (payload["issue"], effect.effect_id, effect.actor, payload["merge_sha"]))
            else:
                raise KernelError("unsupported_offline_verb", operation)
        except KernelError as error:
            if error.code in {"operation_cancelled", "operation_boundary_changed", "executor_fenced"}:
                raise
            if operation not in {"candidate", "integrate"}:
                raise
            self._receipt(effect, EffectResult("failure", raw={"code": error.code, "evidence": str(error)}))
            return None
        result = outcome or EffectResult("target", commit, expected, pr, raw)
        self._receipt(effect, result)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO writes(operation,subject,intent,facts) VALUES (?,?,?,?)",
                       (operation, effect.subject, effect.effect_id, wire_json({"before": effect.before_sha, "after": effect.after_sha})))
        if operation in self.lose_response:
            self.lose_response.remove(operation)
            raise LostResponse(f"{operation} took effect; response lost")
        return result

    def _inspect_remote_cleanup(self, effect: Effect) -> EffectResult:
        if effect.operation != "cleanup_remote_ref" or effect.payload["branch_ref"] != effect.source_ref:
            return EffectResult("third")
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.source_ref,)).fetchone()
        return EffectResult("target" if row is None else "prior" if row[0] == effect.payload["expected_head"] else "third",
                            raw={"observed_head": None if row is None else row[0]})

    def _validation_identity(self, git: LocalGit, checkout: Path, expected: str | None = None) -> None:
        if checkout.parent != self.root / "work" or not checkout.name.startswith("review-"):
            raise KernelError("foreign_validation_checkout")
        common = Path(git.run("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout).stdout.strip()).resolve()
        head = git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        if common != (git.root / ".git").resolve() or (expected is not None and head != expected):
            raise KernelError("foreign_validation_checkout")

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation in self.force_read:
            return EffectResult(self.force_read[effect.operation])
        operation = effect.operation
        if operation == "cleanup_worktree":
            return inspect_worktree(self.git, effect)
        if operation == "cleanup_local_ref":
            return inspect_local_ref(self.git, effect)
        if operation == "cleanup_remote_ref":
            return self._inspect_remote_cleanup(effect)
        if operation == "issue_link":
            with sqlite3.connect(self.database) as db:
                row = db.execute("SELECT ref,sha,intent FROM links WHERE issue=?", (effect.payload["issue"],)).fetchone()
            return EffectResult("target" if row == (effect.payload["ref"], effect.payload["source_head"], effect.effect_id) else "prior" if row is None else "third")
        if operation in {"push", "run_branch"}:
            actual = self.git.commit(effect.source_ref)
            remote = self.remote_refs().get(effect.source_ref)
            return EffectResult("target" if actual.sha == remote == effect.after_sha else "prior" if actual.sha == effect.before_sha and remote in {None, effect.before_sha} else "third", commit=actual)
        if operation == "pr_write":
            pr = self.pull_request(effect.subject)
            if pr is None:
                return EffectResult("prior" if effect.pr_number is None else "third")
            if (pr.repository != effect.repository or pr.source_ref != effect.source_ref or pr.target_ref != effect.target_ref
                    or pr.head.sha != effect.after_sha or pr.actor != effect.actor or pr.state != "OPEN"
                    or (effect.pr_number is not None and pr.number != effect.pr_number)):
                return EffectResult("third", pr=pr)
            if pr.title == effect.payload["title"] and pr.body == effect.payload["body"]:
                return EffectResult("target", pr=pr)
            prior = effect.payload["prior"]
            if prior is not None and pr.title == prior["title"] and pr.body == prior["body"]:
                return EffectResult("prior", pr=pr)
            return EffectResult("third", pr=pr)
        if operation == "merge":
            pr = self.pull_request(effect.subject)
            if pr is None:
                return EffectResult("unknown")
            if (pr.number != effect.pr_number or pr.repository != effect.repository or pr.source_ref != effect.source_ref
                    or pr.target_ref != effect.target_ref or pr.head.sha != effect.after_sha or pr.actor != effect.actor):
                return EffectResult("third", pr=pr)
            if pr.state == "MERGED" and pr.merged is not None and pr.head.sha == effect.after_sha and pr.actor == effect.actor:
                parents = list(self.git.parents(pr.merged.sha))
                if parents != list(effect.payload["parents"]) or pr.merged.tree != pr.head.tree:
                    return EffectResult("third", pr=pr)
                if self.remote_refs().get(effect.target_ref) != pr.merged.sha or self.git.commit(effect.target_ref) != pr.merged:
                    return EffectResult("unknown", pr=pr)
                return EffectResult("target", commit=pr.merged, pr=pr, raw={"parents": parents})
            prior = (pr.state == "OPEN" and pr.head.sha == effect.after_sha and pr.base.sha == effect.before_sha
                     and self.remote_refs().get(effect.target_ref) == effect.before_sha
                     and self.git.commit(effect.target_ref).sha == effect.before_sha)
            return EffectResult("prior" if prior else "unknown" if pr.state == "OPEN" else "third", pr=pr)
        with sqlite3.connect(self.database) as db:
            if operation == "close":
                closed = db.execute("SELECT intent,actor,merge_sha FROM closed WHERE issue=?", (effect.payload["issue"],)).fetchone()
                if closed is None:
                    return EffectResult("prior")
                return EffectResult("target" if closed == (effect.effect_id, effect.actor, effect.payload["merge_sha"]) else "third")
            if operation == "completion_comment":
                comment = db.execute("SELECT actor,body FROM comments WHERE intent=?", (effect.effect_id,)).fetchone()
                return EffectResult("prior" if comment is None else "target" if comment[0] == effect.actor and comment[1] == wire_json(dict(effect.payload)) else "third")
            receipt = db.execute("SELECT body FROM receipts WHERE id=?", (effect.effect_id,)).fetchone()
        if receipt is None:
            return EffectResult("prior")
        data = json.loads(receipt[0])
        commit = None if data["commit"] is None else Commit(**data["commit"])
        if commit is not None and self.git.commit(commit.sha) != commit:
            return EffectResult("third")
        return EffectResult(data["disposition"], commit, data["expected_tree"],
                            None if data["pr"] is None else _pr(data["pr"]), data["raw"])

    def writes(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("SELECT operation,subject,intent,facts FROM writes ORDER BY position").fetchall()
        return [{"operation": operation, "subject": subject, "intent": intent, "facts": json.loads(facts)}
                for operation, subject, intent, facts in rows]

    def external_append(self, ref: str, files: Mapping[str, str], token: str) -> Commit:
        """A separate fixture actor advances an exact ref; never used by the driver."""
        before = self.git.commit(ref)
        checkout = self.root / ("external-" + token)
        self.git.run("worktree", "add", "--detach", str(checkout), before.sha)
        for name, body in files.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        candidate, _ = self.git.candidate(checkout, before.sha, token)
        if candidate.sha == before.sha:
            candidate = self.git.make_commit(before.tree, (before.sha,), "external append " + token)
        self.git.compare_and_swap(ref, before.sha, candidate.sha)
        if ref in self.remote_refs():
            self._remote_cas(ref, before.sha, candidate.sha)
        self.git.run("worktree", "remove", str(checkout))
        return candidate

    def external_remote_append(self, ref: str, files: Mapping[str, str], token: str) -> Commit:
        before = self.remote_refs()[ref]
        temporary = "refs/heads/remote-actor-" + token
        self.git.run("update-ref", temporary, before)
        candidate = self.external_append(temporary, files, token)
        self.git.run("update-ref", "-d", temporary, candidate.sha)
        self._remote_cas(ref, before, candidate.sha)
        return candidate


class Scenario:
    def __init__(self, root: Path, kind: DeliveryKind, *, reviews: Sequence[str] = ("pass",),
                 checks: Sequence[str] = ("none",), policy: Policy | None = None,
                 edits: Sequence[Mapping[str, str]] | None = None, neutral_script: Mapping[str, Any] | None = None) -> None:
        self.script = neutral_script
        if neutral_script is not None:
            declared = {"ticket": DeliveryKind.TICKET, "parent_only": DeliveryKind.PARENT_ONLY, "full_parent": DeliveryKind.FINAL_RUN}[neutral_script.get("delivery_kind", "ticket")]
            if neutral_script["schema"] != "kernel-neutral-ticket/1" or kind != declared:
                raise KernelError("unsupported_neutral_script")
            frozen = neutral_script["policy"]
            policy = Policy(ticket_rounds=frozen["ticket_review_rounds"], parent_rounds=frozen["parent_only_paired_rounds"],
                            run_repairs=frozen["run_repair_rounds"], integration_developments=frozen["ticket_integration_development_limit"],
                            ci_developments=frozen["ticket_ci_development_limit"], ticket_ci_review=frozen["ticket_ci_review"],
                            concurrency=frozen["ticket_development_workers"])
            checks = neutral_script["external"]["required_checks"]
        self.port = OfflinePort.create(root, None if neutral_script is None else neutral_script["git"])
        self.spec, self.delivery = self.port.prepare(kind, policy=policy,
            ticket_number=2 if neutral_script is None else neutral_script["ticket"]["number"] if neutral_script.get("ticket") else 1,
            repository="example/repository" if neutral_script is None else neutral_script["repository"],
            completed_ticket=None if neutral_script is None else neutral_script.get("completed_ticket_precondition"))
        evidence = None if neutral_script is None else {"parent": neutral_script["parent"], "ticket": neutral_script.get("ticket"),
            "historical_completion": neutral_script.get("completed_ticket_precondition"), "source": "explicit immutable neutral fixture; not a live Completion import"}
        self.kernel = Kernel.create(root / "kernel.sqlite3", self.spec, fixture_evidence=evidence)
        self.execution = self.kernel.claim_executor("offline-executor")
        self.reviews = list(reviews)
        self.checks = list(checks)
        self.review_count = 0
        self.development_count = 0
        self.publication_count = 0
        self.heads: list[str] = []
        self.role_calls: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.edits = edits
        self.reviewed_heads: list[str] = []

    def world(self) -> World:
        pr = self.port.pull_request(self.delivery.key)
        if pr is not None and pr.head.sha not in self.heads:
            self.heads.append(pr.head.sha)
        index = max(0, len(self.heads) - 1)
        return self.port.world(self.spec, self.delivery, self.checks[min(index, len(self.checks) - 1)])

    def agent(self, call: AgentCall) -> bytes:
        self.role_calls.append({"role": call.role.value, "purpose": call.purpose.value,
                                "owner_kind": self.delivery.kind.value, "attempt": call.attempt_id,
                                "repair_source": call.context.get("repair_source")})
        self.kernel.bind_thread(call, call.reported_thread or "offline-" + call.thread_id)
        if call.role == Role.DEVELOPMENT:
            self.development_count += 1
            script_step = None if self.script is None else self.script["agents"]["development"][self.development_count - 1]
            edits = (script_step["edits"] if script_step is not None else
                     {f"development-{self.development_count}.txt": f"Change {self.development_count}\n"} if self.edits is None
                     else self.edits[min(self.development_count - 1, len(self.edits) - 1)])
            for name, body in edits.items():
                path = Path(call.context["checkout"]) / name
                if not path.resolve().is_relative_to(Path(call.context["checkout"]).resolve()) or ".git" in Path(name).parts:
                    raise KernelError("invalid_fixture_path")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            return wire_json(script_step["wire"] if script_step is not None else
                             {"result_kind": "development", "summary": f"Changed synthetic behavior {self.development_count}", "human_blockers": None})
        if call.role == Role.REVIEW:
            # A Reviewer may produce temporary test files in its own checkout.
            assert Path(call.context["checkout"]) != Path(self.delivery.checkout)
            assert self.port.git.run("rev-parse", "HEAD", cwd=Path(call.context["checkout"])).stdout.strip() == call.context["reviewed_commit"]
            (Path(call.context["checkout"]) / "validation.tmp").write_text("Reviewer temporary file\n")
            self.reviewed_heads.append(call.context["candidate_sha"])
            outcome = self.reviews[min(self.review_count, len(self.reviews) - 1)]
            self.review_count += 1
            if self.script is not None:
                return wire_json(self.script["agents"]["review"][self.review_count - 1])
            return wire_json(review_result(outcome))
        self.publication_count += 1
        if self.script is not None:
            return wire_json(self.script["agents"]["publication"][self.publication_count - 1])
        return wire_json({"result_kind": "publication", "commit_message": f"feat: synthetic behavior {self.publication_count}\n\nValidated change.",
                          "pr_title": f"feat: synthetic behavior {self.publication_count}",
                          "pr_body_markdown": f"Synthetic scenario with recorded evidence {self.publication_count}.", "human_blockers": None})

    def step(self) -> Step:
        world = self.world()
        step = self.kernel.advance(self.delivery.key, self.execution, world)
        return self.handle(step)

    def handle(self, step: Step) -> Step:
        if step.kind == "agent" and step.call is not None:
            self.kernel.receive(step.call, self.agent(step.call))
        elif step.kind == "effect" and step.effect is not None:
            effect = step.effect
            if self.kernel.dispatch(effect, self.execution, self.world()):
                try:
                    acknowledgement = self.port.execute(effect, lambda: self.kernel.guard_effect(effect, self.execution, self.world()))
                except LostResponse:
                    pass
                else:
                    if effect.operation == "issue_link":
                        if acknowledgement is not None:
                            self.kernel.acknowledge_link(effect, acknowledgement)
                    else:
                        self.kernel.reconcile(effect, self.port.reconcile(effect), self.execution, self.world())
        elif step.kind == "reconcile" and step.effect is not None:
            self.kernel.reconcile(step.effect, self.port.reconcile(step.effect), self.execution, self.world())
        self.checkpoints.append(self.kernel.status(self.delivery.key))
        return step

    def run(self, *, approve: bool = False, max_steps: int = 200, cut: str = "complete") -> Step:
        for _ in range(max_steps):
            if cut == "publication_pending" and self.kernel.status(self.delivery.key)["phase"] == "publish":
                return Step("publication_pending", self.delivery.key)
            step = self.step()
            if step.kind == "approval" and approve:
                self.kernel.control("approve", f"approval-{self.publication_count}", subject=self.delivery.key, world=self.world())
            elif step.kind in {"complete", "paused", "approval", "waiting"}:
                if step.kind == "complete":
                    self.cleanup()
                return step
        raise AssertionError("offline driver failed to reach a stable observable boundary")

    def cleanup(self, *, max_steps: int = 16) -> Step:
        for _ in range(max_steps):
            step = self.kernel.advance_cleanup(self.delivery.key, self.execution, self.world())
            self.handle(step)
            current = self.kernel.status(self.delivery.key)["resource_cleanup"]
            if step.kind in {"complete", "paused", "waiting"} or (current and current["outcome"] in {"third", "unknown"}):
                return step
        return Step("waiting", self.delivery.key, "cleanup_pending")

    def trace(self) -> dict[str, Any]:
        return {"role_calls": self.role_calls, "checkpoints": self.checkpoints,
                "raw_backend_calls": {"development": self.development_count, "review": self.review_count,
                                      "publication": self.publication_count},
                "external_writes": self.port.writes(), "final": self.kernel.status(self.delivery.key)}

    def observable(self, scenario_id: str, boundary: Step) -> dict[str, Any]:
        """A declared read-side projection, not an expected-value generator."""
        calls: list[dict[str, str]] = []
        # Exact frozen call contexts are read through the same public history,
        # captured by the Agent adapter at actual invocation time below.
        for call in self.role_calls:
            role, purpose = call["role"], call["purpose"]
            if role == "publication":
                purpose = "publication"
            elif purpose == "ci":
                purpose = "ci_repair"
            elif self.delivery.kind == DeliveryKind.FINAL_RUN:
                purpose = "acceptance_repair" if call.get("repair_source") == "acceptance" else "full_review" if role == "review" else "integration"
            elif purpose == "requirements":
                purpose = "acceptance_repair" if role == "development" and call.get("repair_source") == "acceptance" else "initial"
            calls.append({"role": "reviewer" if role == "review" else role, "purpose": purpose,
                          "owner_kind": "full_parent" if self.delivery.kind == DeliveryKind.FINAL_RUN else self.delivery.kind.value})
        world = self.world()
        refs = {}
        import hashlib
        for name, ref in (("source", self.delivery.source_ref), ("target", self.delivery.target_ref)):
            commit = self.port.ref_commit(ref)
            if commit is None:
                continue
            paths = self.port.git.run("ls-tree", "-r", "--name-only", commit.sha).stdout.splitlines()
            refs[name] = {"sha": commit.sha, "tree": commit.tree, "parents": list(self.port.git.parents(commit.sha)),
                          "files": {path: hashlib.sha256(self.port.git.run("show", f"{commit.sha}:{path}").stdout.encode()).hexdigest() for path in paths}}
        return {"scenario_id": scenario_id, "parameters": {"kind": self.delivery.kind.value, "cut": boundary.kind,
                "policy": asdict(self.spec.policy)}, "role_calls": calls,
                "raw_backend_calls": {"development": self.development_count, "reviewer": self.review_count,
                                      "publication": self.publication_count},
                "checkpoints": self.checkpoints, "external_writes": self.port.writes(),
                "final": self.kernel.status(self.delivery.key), "terminal": boundary.kind,
                "world": asdict(world), "git_evidence": refs}

    def neutral_observation(self, script: Mapping[str, Any], input_digest: str) -> dict[str, Any]:
        """Actual same-input facts; cleanup differences are deliberately retained."""
        import hashlib
        pr = self.port.pull_request(self.delivery.key)
        if pr is None or pr.merged is None or not self.reviewed_heads:
            raise KernelError("neutral_cut_not_reached")
        heads = {"initial": self.delivery.base.sha, "candidate": self.reviewed_heads[-1],
                 "published": pr.head.sha, "integrated": pr.merged.sha}
        identities: dict[str, list[str]] = {}
        for name, head in heads.items():
            identities.setdefault(head, []).append(name)

        def head_identity(head: str) -> list[str] | str:
            return identities.get(head, head)

        def actual_commit(head: str) -> dict[str, Any]:
            files: dict[str, Any] = {}
            for path in self.port.git.run("ls-tree", "-r", "--name-only", head).stdout.splitlines():
                content = self.port.git.run("show", f"{head}:{path}").stdout
                files[path] = {"utf8": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
            return {"sha": head, "tree": self.port.git.commit(head).tree,
                    "parents": [head_identity(parent) for parent in self.port.git.parents(head)],
                    "message": self.port.git.run("show", "-s", "--format=%B", head).stdout.strip(), "files": files}

        refs: dict[str, Any] = {}
        for line in self.port.git.run("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout.splitlines():
            ref, head = line.split()
            name = {self.delivery.source_ref: "ticket", self.delivery.target_ref: "run", "refs/heads/main": "main"}.get(ref, ref)
            refs[name] = head_identity(head)
        with sqlite3.connect(self.port.database) as db:
            closed = db.execute("SELECT issue,intent,actor,merge_sha FROM closed ORDER BY issue").fetchall()
            comments = db.execute("SELECT intent,issue,actor,body FROM comments ORDER BY rowid").fetchall()
            linked = db.execute("SELECT issue,ref,sha,intent FROM links ORDER BY issue").fetchall()
        mutations: list[dict[str, Any]] = []
        for _, issue, _, raw in comments:
            body = json.loads(raw)
            mutations.append({"action": "completion_comment", "ticket_number": issue,
                              "pr_number": pr.number, "integrated_sha": head_identity(body["merge_sha"])})
        for issue, _, _, merge_sha in closed:
            mutations.append({"action": "close_issue", "ticket_number": issue,
                              "pr_number": pr.number, "integrated_sha": head_identity(merge_sha)})
        for write in self.port.writes():
            if write["operation"] == "cleanup_remote_ref" and self.delivery.source_ref not in self.port.remote_refs():
                mutations.append({"action": "delete_managed_branch", "branch": "ticket"})
        current = self.kernel.status(self.delivery.key)
        projected = self.observable(script["id"], Step("complete", self.delivery.key))
        display_attempts = [{"issue_number": issue, "branch": "ticket" if ref == self.delivery.source_ref else ref,
                             "head_sha": head_identity(head)} for issue, ref, head, _ in linked]
        context_paths = {"development": self.delivery.checkout, "review": current["reviewed_checkout"], "publication": self.delivery.checkout}
        listing = self.port.git.run("worktree", "list", "--porcelain", "-z").stdout
        registry = listing.split("\0")
        registered = []
        for block in listing.split("\0\0"):
            fields = block.split("\0")
            if not fields[0].startswith("worktree "):
                continue
            path = fields[0][len("worktree "):]
            values = dict(field.split(" ", 1) for field in fields[1:] if " " in field)
            branch = values.get("branch")
            registered.append({"path": path, "repository": Path(path) == self.port.git.root,
                               "checkout_roles": [role for role, checkout in context_paths.items() if checkout == path],
                               "exists": Path(path).exists(), "head": head_identity(values["HEAD"]),
                               "branch": None if branch is None else {self.delivery.source_ref: "ticket", self.delivery.target_ref: "run", "refs/heads/main": "main"}.get(branch, branch),
                               "detached": "detached" in fields, "prunable": values.get("prunable")})
        return {"schema": "kernel-neutral-ticket-observation/1", "input_sha256": input_digest,
                "implementation": "independent-kernel-exploration", "cut": script["cut"],
                "roles": projected["role_calls"], "raw_backend_calls": {"development": self.development_count,
                    "review": self.review_count, "publication": self.publication_count},
                "status": "ticket_completed" if current["phase"] == "complete" else current["phase"],
                "approval_required": False, "budget": {"initial_development": current["budgets"]["requirements"]["developments_used"],
                    "initial_review": current["budgets"]["requirements"]["reviews_used"]},
                "commits": {name: actual_commit(head) for name, head in heads.items()}, "local_refs": refs,
                "published_refs": {{self.delivery.source_ref: "ticket", self.delivery.target_ref: "run"}.get(ref, ref): head_identity(head)
                                   for ref, head in self.port.remote_refs().items() if ref in {self.delivery.source_ref, self.delivery.target_ref}},
                "pull_request": {"title": pr.title, "body": pr.body, "state": pr.state,
                                 "primary_ticket": self.delivery.issue},
                "closed_issues": [row[0] for row in closed], "mutations": mutations,
                "native_branch_display": {"observed": True, "calls": [{**attempt, "result": "linked"} for attempt in display_attempts],
                                          "fixture_attempts": display_attempts, "linked_branches": {str(issue): "ticket" if ref == self.delivery.source_ref else ref for issue, ref, _, _ in linked},
                                          "durable_outcome": {"display_attempted": current["issue_link"] is not None,
                                                              "status": None if current["issue_link"] is None else current["issue_link"]["state"]}},
                "post_close_worktrees": {"observed": True, "registrations": registered,
                                         "role_checkouts": {role: {"observed": True, "path": path, "exists": Path(path).exists(), "registered": f"worktree {path}" in registry} for role, path in context_paths.items()}},
                "publication_wire": script["agents"]["publication"][0], "cross_architecture_compared": False,
                "known_gaps": ["GitHub Issue branch linking uses a persistent synthetic remote, not the native online API"],
                "raw_effects": self.port.writes()}


class GraphScenario(Scenario):
    """Native source reader plus the same one-request driver, using real Git."""

    def __init__(self, root: Path, requirements: Requirements, *, policy: Policy | None = None,
                 reviews: Sequence[str] = ("pass",), initial_git: Mapping[str, Any] | None = None) -> None:
        self.requirements = requirements
        self.script = None
        self.port = OfflinePort.create(root, initial_git)
        base = self.port.git.commit("refs/heads/main")
        run_ref = "refs/heads/run"
        self.port.git.run("update-ref", run_ref, base.sha)
        self.port._remote_cas(run_ref, base.sha, base.sha)
        work = (root / "work").resolve()
        work.mkdir()
        deliveries = []
        common = str((self.port.git.root / ".git").resolve())
        for number in requirements.parent.sub_issue_numbers:
            source_ref = f"refs/heads/ticket-{number}"
            checkout = work / f"ticket-{number}"
            self.port.git.run("update-ref", source_ref, base.sha)
            self.port.git.run("worktree", "add", "--detach", str(checkout), base.sha)
            deliveries.append(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "untrusted", base, source_ref, run_ref, checkout=str(checkout), owned_root=str(work), common_git_dir=common))
        final = Delivery("final:1", DeliveryKind.FINAL_RUN, requirements.parent.number, "untrusted", base,
                         run_ref, "refs/heads/main", checkout=str(work / "final-1"), owned_root=str(work), common_git_dir=common)
        deliveries.append(final)
        self.spec = RunSpec("offline-run", "example/project", requirements.parent.number, "untrusted", "untrusted", tuple(deliveries), policy or Policy())
        self.kernel = Kernel.create(root / "kernel.sqlite3", self.spec, requirements=requirements)
        self.execution = self.kernel.claim_executor("offline-executor")
        self.delivery = self.kernel.delivery(deliveries[0].key)
        self.reviews, self.checks = list(reviews), ["none"]
        self.review_count = self.development_count = self.publication_count = 0
        self.heads: list[str] = []
        self.role_calls: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.edits = None
        self.reviewed_heads: list[str] = []

    def select(self, subject: str) -> None:
        self.delivery = self.kernel.delivery(subject)
        self.heads = []

    def refresh(self) -> dict[str, Any]:
        with sqlite3.connect(self.port.database) as db:
            closed = {row[0] for row in db.execute("SELECT issue FROM closed")}
        source = replace(self.requirements, issues=tuple(
            replace(issue, state="CLOSED" if issue.number in closed else issue.state,
                    blocked_by=tuple(replace(blocker, state="CLOSED") if blocker.number in closed else blocker for blocker in issue.blocked_by))
            for issue in self.requirements.issues))
        return self.kernel.refresh(source)

    def world(self) -> World:
        self.refresh()
        self.delivery = self.kernel.delivery(self.delivery.key)
        status = self.kernel.status(self.delivery.key)
        source = self.port.remote_commit(self.delivery.source_ref)
        target = self.port.remote_commit(self.delivery.target_ref)
        if target is None:
            raise KernelError("remote_target_missing")
        pr = self.port.pull_request(self.delivery.key)
        checks = None if pr is None else Checks(pr.number, pr.head.sha, "none")
        return World(status["source_parent_revision"], self.kernel.source_status()["observed_graph_revision"], status["source_revision"],
                     target, source, pr, checks, status["completion_revision"],
                     ((target.sha, source.sha),) if source is not None and self.port.git.is_ancestor(target.sha, source.sha) else ())

    def agent(self, call: AgentCall) -> bytes:
        raw = super().agent(call)
        self.role_calls[-1]["subject"] = call.subject
        self.role_calls[-1]["context"] = dict(call.context)
        return raw

    def activate_final(self) -> str:
        self.refresh()
        head = self.port.remote_commit("refs/heads/run")
        default = self.port.remote_commit("refs/heads/main")
        if head is None or default is None:
            raise KernelError("final_source_missing")
        records = self.kernel.source_status()["completions"]
        ancestry = tuple((record["merge_sha"], head.sha) for record in records if self.port.git.is_ancestor(record["merge_sha"], head.sha))
        return self.kernel.activate_final(self.execution, head, default, ancestry=ancestry)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run one isolated replacement-kernel scenario on newly created data")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", required=True, choices=("ticket-normal", "ticket-review-repair", "ticket-fallback",
                        "parent-only-normal", "full-parent-normal", "full-parent-review-repair"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="Exact neutral Ticket script, shared with the unchanged baseline adapter")
    args = parser.parse_args()
    configurations = {
        "ticket-normal": (DeliveryKind.TICKET, ("pass",), "complete"),
        "ticket-review-repair": (DeliveryKind.TICKET, ("fail", "pass"), "complete"),
        "ticket-fallback": (DeliveryKind.TICKET, ("fail",), "complete"),
        "parent-only-normal": (DeliveryKind.PARENT_ONLY, ("pass",), "complete"),
        "full-parent-normal": (DeliveryKind.FINAL_RUN, ("pass",), "complete"),
        "full-parent-review-repair": (DeliveryKind.FINAL_RUN, ("fail", "pass"), "publication_pending"),
    }
    kind, reviews, cut = configurations[args.scenario]
    script = None if args.input is None else json.loads(args.input.read_text(encoding="utf-8"))
    scenario = Scenario(args.root, kind, reviews=reviews, neutral_script=script)
    boundary = scenario.run(approve=kind != DeliveryKind.TICKET, cut=cut)
    result = {"schema": "kernel-observable-trace/1", "implementation": "independent-kernel-exploration",
              "cross_architecture_compared": False, "scenarios": [scenario.observable(args.scenario, boundary)]}
    if script is not None:
        import hashlib
        result["neutral_observation"] = scenario.neutral_observation(script, hashlib.sha256(args.input.read_bytes()).hexdigest())
    args.output.write_bytes(wire_json(result) + b"\n")
    print(json.dumps({"scenario": args.scenario, "terminal": boundary.kind, "output": str(args.output)}))


if __name__ == "__main__":
    main()
