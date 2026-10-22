"""Executable synthetic-GitHub/Agent adapter, backed by real Git and SQLite.

This entire module is exploration cost, not a production GitHub/Worker adapter.
Its scripts control external outcomes, never internal kernel transitions.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence, TypeVar

from agent_run.agent_profiles import resolve_profiles
from agent_run.error_safety import bounded_error
from agent_run.github import GitHubReadError
from agent_run.prompt_resources import resolve_resources

from .effects import LocalGit
from .local_effects import LocalEffects
from .remote_effects import EffectReadError, RemoteEffects
from .kernel import Kernel
from .operation_runner import DeliveryFacts, DeliveryRead, OperationBinding, OperationPorts, OperationRunner, ReadUnavailable
from .model import (AgentCall, Checks, Commit, CreationInputs, Delivery, DeliveryKind, Effect, EffectResult, Execution,
                    Issue, KernelError, ParentIssue, Policy, PullRequest, Requirements, ResourceInspection, ResourceWitness, Role, RunSpec, Step, World, wire_json)


class LostResponse(OSError):
    pass


_ReadValue = TypeVar('_ReadValue')


def _physical_read(read: Callable[[], _ReadValue]) -> _ReadValue:
    try:
        return read()
    except GitHubReadError as error:
        raise ReadUnavailable(error.code, error.message) from error
    except (OSError, sqlite3.Error) as error:
        raise ReadUnavailable('physical_read_unavailable', bounded_error(str(error))) from error


def fixture_inputs() -> CreationInputs:
    """Explicit fixture configuration; never an implicit production fallback."""
    return CreationInputs(resolve_profiles(preset="economy"), resolve_resources("zh"),
                          {"source": "host"}, {"source": "host"}, {"enabled": False},
                          {"source": "explicit-synthetic-fixture", "profile_preset": "economy"},
                          repository_remote="offline-remote")


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
        self.git.env.update({"GIT_AUTHOR_NAME": "Offline Publisher", "GIT_AUTHOR_EMAIL": "offline@example.invalid",
                             "GIT_COMMITTER_NAME": "Offline Publisher", "GIT_COMMITTER_EMAIL": "offline@example.invalid",
                             "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"})
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
                CREATE TABLE prs(number INTEGER PRIMARY KEY,subject TEXT NOT NULL,body BLOB NOT NULL);
                CREATE TABLE pr_events(id INTEGER PRIMARY KEY AUTOINCREMENT,pr INTEGER NOT NULL,
                    event TEXT NOT NULL,actor TEXT NOT NULL,intent TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                CREATE TABLE writes(position INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,subject TEXT,intent TEXT,facts BLOB);
                CREATE TABLE closed(issue INTEGER PRIMARY KEY,intent TEXT,actor TEXT,merge_sha TEXT);
                CREATE TABLE issue_events(id INTEGER PRIMARY KEY AUTOINCREMENT,issue INTEGER NOT NULL,
                    event TEXT NOT NULL,actor TEXT NOT NULL,intent TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                CREATE TABLE comments(intent TEXT PRIMARY KEY,issue INTEGER,actor TEXT,body BLOB,
                    baseline_event_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                CREATE TABLE refs(ref TEXT PRIMARY KEY,sha TEXT NOT NULL);
                CREATE TABLE links(issue INTEGER PRIMARY KEY,ref TEXT NOT NULL,sha TEXT NOT NULL,intent TEXT NOT NULL);
            """)
            db.execute("INSERT INTO refs VALUES (?,?)", ("refs/heads/main", port.git.commit("HEAD").sha))
        return port

    def prepare(self, kind: DeliveryKind, *, policy: Policy | None = None,
                ticket_number: int = 2, repository: str = "example/repository") -> tuple[RunSpec, Delivery]:
        if kind == DeliveryKind.FINAL_RUN:
            raise KernelError("final_requires_completed_source")
        base = self.git.commit("refs/heads/main")
        source = base
        key = f"ticket:{ticket_number}" if kind == DeliveryKind.TICKET else "parent:1"
        source_ref = "refs/heads/task"
        target_ref = "refs/heads/run" if kind == DeliveryKind.TICKET else "refs/heads/main"
        self.git.run("update-ref", source_ref, source.sha)
        self.git.run("update-ref", target_ref, base.sha)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT OR IGNORE INTO refs VALUES (?,?)", (target_ref, base.sha))
        checkout = self.root / "work" / key.replace(":", "-")
        checkout.parent.mkdir(exist_ok=True)
        # The shared Core workspace effect creates and authenticates checkout.
        delivery = Delivery(key, kind, ticket_number if kind == DeliveryKind.TICKET else 1, "subject-v1", source,
                            source_ref, target_ref, completion_revision="no-tickets", checkout=str(checkout),
                            owned_root=str(checkout.parent.resolve()), common_git_dir=str((self.git.root / ".git").resolve()))
        spec = RunSpec("offline-run", repository, 1, "parent-v1", "graph-v1", (delivery,), policy or Policy(),
                       language="zh", repository_root=str(self.git.root.resolve()), inputs=fixture_inputs())
        return spec, delivery

    def pull_request(self, subject: str, source_ref: str | None = None) -> PullRequest | None:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("SELECT body FROM prs WHERE subject=? ORDER BY number DESC", (subject,)).fetchall()
        for row in rows:
            pr = _pr(json.loads(row[0]))
            if source_ref is None or pr.source_ref == source_ref:
                return self.pr_by_number(pr.number)
        return None

    def source(self, requirements: Requirements, parent_number: int) -> Requirements:
        if requirements.parent.number != parent_number:
            raise KernelError("synthetic_source_binding")
        with sqlite3.connect(self.database) as db:
            closed = {row[0] for row in db.execute("SELECT issue FROM closed")}
        return replace(requirements, issues=tuple(
            replace(issue, state="CLOSED" if issue.number in closed else issue.state,
                    blocked_by=tuple(replace(blocker, state="CLOSED") if blocker.number in closed else blocker
                                     for blocker in issue.blocked_by)) for issue in requirements.issues))

    def pr_by_number(self, number: int) -> PullRequest | None:
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT body FROM prs WHERE number=?", (number,)).fetchone()
        if row is None:
            return None
        pr = _pr(json.loads(row[0]))
        if pr.state == "MERGED":
            return pr
        # GitHub's base/head are live remote facts, not frozen intent fields.
        return PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref,
                           self.remote_commit(pr.source_ref) or pr.head, self.remote_commit(pr.target_ref) or pr.base, pr.title, pr.body,
                           pr.state, pr.actor, pr.merged, pr.head_repository)

    def delivery(self, request: DeliveryRead, checks_script: Sequence[str], heads: list[str], *,
                 publisher_identity: Mapping[str, Any] | None = None) -> DeliveryFacts:
        """Read the synthetic remote; revisions and World belong to Core."""
        pr = (None if not request.read_pr else self.pr_by_number(request.pr_number)
              if request.pr_number is not None else self.pull_request_by_ref(request.source_ref))
        checks = None
        if pr is not None and pr.state == "OPEN":
            if request.check_head is not None and request.check_head != pr.head.sha:
                raise ReadUnavailable("github_observation_pending", "Synthetic checks head is not current")
            if pr.head.sha not in heads:
                heads.append(pr.head.sha)
            checks_result = checks_script[min(max(0, len(heads) - 1), len(checks_script) - 1)]
            details = () if checks_result == "none" else (("required", checks_result),)
            checks = Checks(pr.number, pr.head.sha, checks_result, details,
                            "Raw synthetic failed job log" if checks_result == "fail" else "")
        source, target = self.remote_commit(request.source_ref), self.remote_commit(request.target_ref)
        ancestry_heads = set(request.anchors) | {head.sha for head in (source, target) if head is not None}
        ancestry = tuple((before, after) for before in sorted(ancestry_heads) for after in sorted(ancestry_heads)
                         if before == after or self.git.is_ancestor(before, after))
        return DeliveryFacts(target, source, pr, checks, ancestry, "offline-publisher", publisher_identity)

    def pull_request_by_ref(self, source_ref: str) -> PullRequest | None:
        with sqlite3.connect(self.database) as db:
            numbers = [row[0] for row in db.execute("SELECT number FROM prs ORDER BY number DESC")]
        return next((pr for number in numbers if (pr := self.pr_by_number(number)) is not None
                     and pr.source_ref == source_ref), None)

    def ancestry(self, source: Commit | None, target: Commit, witnesses: Sequence[str]) -> tuple[tuple[str, str], ...]:
        before_heads = set(witnesses) | {target.sha}
        after_heads = {target.sha} if source is None else {target.sha, source.sha}
        return tuple((before, after) for before in sorted(before_heads) for after in sorted(after_heads)
                     if before == after or self.git.is_ancestor(before, after))

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

    def external_pr_transition(self, number: int, state: str, actor: str) -> None:
        """An independent fixture actor changes the exact PR lifecycle."""
        if state not in {"OPEN", "CLOSED"}:
            raise ValueError("Expected an explicit PR lifecycle state")
        pr = self.pr_by_number(number)
        if pr is None:
            raise ValueError("Unknown PR")
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT subject FROM prs WHERE number=?", (number,)).fetchone()
            self._write_pr_in(db, row[0], replace(pr, state=state))
            db.execute("INSERT INTO pr_events(pr,event,actor,intent) VALUES (?,?,?,'external')",
                       (number, "reopened" if state == "OPEN" else "closed", actor))

    def _write_pr_in(self, db: sqlite3.Connection, subject: str, pr: PullRequest) -> None:
        db.execute("INSERT INTO prs VALUES (?,?,?) ON CONFLICT(number) DO UPDATE SET body=excluded.body",
                   (pr.number, subject, wire_json(asdict(pr))))

    @property
    def local(self) -> LocalEffects:
        return LocalEffects(self.git, owned_root=self.root / "work")

    def inspect_resources(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        return self.local.inspect_resources(resources)

    def issue_state(self, issue: int) -> str:
        with sqlite3.connect(self.database) as db:
            return "CLOSED" if db.execute("SELECT issue FROM closed WHERE issue=?", (issue,)).fetchone() else "OPEN"

    def _inspect_reopen(self, effect: Effect) -> EffectResult:
        original = effect.payload["close_receipt"].get("raw", {}).get("event")
        if not isinstance(original, dict):
            return EffectResult("unknown")
        issue = effect.payload["issue"]
        state = self.issue_state(issue)
        with sqlite3.connect(self.database) as db:
            event = db.execute("SELECT id,event,actor,intent,created_at FROM issue_events WHERE issue=? ORDER BY id DESC LIMIT 1", (issue,)).fetchone()
        if state == "OPEN":
            if event is not None and event[1:4] == ("reopened", effect.actor, effect.effect_id):
                return EffectResult("target", raw={"issue": issue, "state": state, "prior_close": original,
                    "reopened_event": {"id": event[0], "event": event[1], "actor": event[2], "created_at": event[4]}})
            actual = None if event is None else {"id": event[0], "event": event[1], "actor": event[2], "created_at": event[4]}
            return EffectResult("third", raw={"issue": issue, "state": state, "prior_close": original,
                                               "latest_reopen": actual, "reason": "not_owned"})
        actual = None if event is None else {"id": event[0], "event": event[1], "actor": event[2], "created_at": event[4]}
        if actual != original:
            return EffectResult("third", raw={"issue": issue, "state": state, "prior_close": original,
                                               "latest_close": actual, "reason": "not_owned"})
        return EffectResult("prior", raw={"issue": issue, "state": state, "latest_close": actual})

    def _inspect_recovery_comment(self, effect: Effect) -> EffectResult:
        proof = effect.payload["reopen_receipt"]["raw"]
        raw = {"issue": effect.payload["issue"], "reopen_effect": effect.payload["reopen_effect"],
               "prior_close": proof["prior_close"], "reopened_event": proof["reopened_event"]}
        body = (RemoteEffects._marker(effect) + "\n" + effect.payload["comment_body"]).encode()
        with sqlite3.connect(self.database) as db:
            comment = db.execute("SELECT rowid,issue,actor,body,created_at FROM comments WHERE intent=?",
                                 (effect.effect_id,)).fetchone()
        if comment is not None:
            if comment[1:4] != (effect.payload["issue"], effect.actor, body):
                return EffectResult("third", raw=raw)
            return EffectResult("target", raw={**raw, "comment": {"id": comment[0], "body": body.decode(),
                "actor": effect.actor, "created_at": comment[4]}})
        reopened = self._inspect_reopen(replace(effect, effect_id=effect.payload["reopen_effect"]))
        if reopened.disposition != "target":
            return reopened
        if reopened.raw["reopened_event"] != proof["reopened_event"]:
            return EffectResult("unknown", raw=raw)
        return EffectResult("prior", raw=raw)

    def prepare_retirement(self, effect: Effect) -> dict[str, Any]:
        prior = effect.payload["prior_pr"]
        pr = self.pr_by_number(prior["number"])
        if pr is None or pr.state != "OPEN" or asdict(pr) != prior:
            raise KernelError("retirement_pr_changed")
        with sqlite3.connect(self.database) as db:
            baseline = db.execute("SELECT coalesce(max(id),0) FROM pr_events WHERE pr=?", (pr.number,)).fetchone()[0]
        return {"repository": pr.repository, "pr_number": pr.number, "source_ref": pr.source_ref,
                "target_ref": pr.target_ref, "head_sha": pr.head.sha, "base_sha": pr.base.sha,
                "author": pr.actor, "baseline_event_id": baseline}

    def _inspect_retirement(self, effect: Effect) -> EffectResult:
        intent = effect.payload["retirement_intent"]
        pr = self.pr_by_number(intent["pr_number"])
        if pr is None:
            return EffectResult("unknown")
        prior = effect.payload["prior_pr"]
        original_base = Commit(**prior["base"])
        if (asdict(replace(pr, state="OPEN", base=original_base)) != prior
                or not self.git.is_ancestor(original_base.sha, pr.base.sha)):
            return EffectResult("third", pr=pr)
        if pr.state == "OPEN":
            return EffectResult("prior", pr=pr)
        with sqlite3.connect(self.database) as db:
            comment = db.execute("SELECT rowid,actor,body,created_at FROM comments WHERE intent=?", (effect.effect_id,)).fetchone()
            event = db.execute("SELECT id,event,actor,intent,created_at FROM pr_events WHERE pr=? ORDER BY id DESC LIMIT 1", (pr.number,)).fetchone()
        if (pr.state != "CLOSED" or comment is None or event is None or comment[1] != effect.actor
                or comment[2] != wire_json(intent) or event[0] <= intent["baseline_event_id"]
                or event[1:4] != ("closed", effect.actor, effect.effect_id)):
            return EffectResult("third", pr=pr)
        return EffectResult("target", pr=pr, raw={
            "close_intent": {**intent, "comment_id": comment[0], "comment_created_at": comment[3]},
            "base_ancestry": [original_base.sha, pr.base.sha],
            "event": {"id": event[0], "event": event[1], "actor": event[2], "created_at": event[4]}})

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
        if operation in LocalEffects.OPERATIONS:
            outcome = self.local.execute(effect, authorize or (lambda: None))
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
                if operation == "run_branch":
                    self._sync_mirror(git, effect.source_ref, effect.before_sha)
                    git.compare_and_swap(effect.source_ref, effect.before_sha, effect.after_sha)
                elif authorize is not None:
                    # Publishing a detached SHA does not move the local
                    # workspace marker created by the original workspace.
                    authorize()
                self._remote_cas_in(db, effect.source_ref, effect.before_sha, effect.after_sha)
            commit = git.commit(effect.after_sha)
        elif operation == "pr_write":
            if authorize is not None:
                authorize()
            prior = self.pull_request(effect.subject, effect.source_ref)
            if prior is not None and prior.state != "OPEN":
                raise KernelError("closed_pull_request")
            with sqlite3.connect(self.database) as db:
                number = prior.number if prior else db.execute("SELECT coalesce(max(number),0)+1 FROM prs").fetchone()[0]
            pr = PullRequest(number, effect.repository, effect.source_ref, effect.target_ref,
                             self.git.commit(str(self.remote_refs()[effect.source_ref])), self.git.commit(str(self.remote_refs()[effect.target_ref])),
                             payload["title"], payload["body"], actor=effect.actor, head_repository=effect.repository)
            self._write_pr(effect.subject, pr)
        elif operation == "retire_pr":
            observed = self._inspect_retirement(effect)
            if observed.disposition != "prior":
                return observed
            intent = payload["retirement_intent"]
            if authorize is not None:
                authorize()
            with sqlite3.connect(self.database) as db:
                db.execute("INSERT OR IGNORE INTO comments(intent,issue,actor,body,baseline_event_id) VALUES (?,?,?,?,?)",
                           (effect.effect_id, intent["pr_number"], effect.actor, wire_json(intent), intent["baseline_event_id"]))
            if authorize is not None:
                authorize()
            observed = self._inspect_retirement(effect)
            if observed.disposition != "prior" or observed.pr is None:
                return observed
            with sqlite3.connect(self.database) as db:
                self._write_pr_in(db, effect.subject, replace(observed.pr, state="CLOSED"))
                db.execute("INSERT INTO pr_events(pr,event,actor,intent) VALUES (?,'closed',?,?)",
                           (intent["pr_number"], effect.actor, effect.effect_id))
            outcome = self._inspect_retirement(effect)
        elif operation == "merge":
            pr = self.pull_request(effect.subject, effect.source_ref)
            if pr is None or pr.head.sha != effect.after_sha or pr.base.sha != effect.before_sha or pr.state != "OPEN":
                raise KernelError("merge_compare_and_swap")
            commit = git.make_commit(pr.head.tree, payload["parents"], payload["commit_message"])
            pr = PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref, pr.head, pr.base,
                             pr.title, pr.body, "MERGED", effect.actor, commit, pr.head_repository)
            with sqlite3.connect(self.database) as db:
                db.execute("BEGIN IMMEDIATE")
                source = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.source_ref,)).fetchone()
                target = db.execute("SELECT sha FROM refs WHERE ref=?", (effect.target_ref,)).fetchone()
                if source != (effect.after_sha,) or target != (effect.before_sha,):
                    raise KernelError("merge_compare_and_swap")
                self._sync_mirror(git, effect.target_ref, effect.before_sha)
                git.compare_and_swap(effect.target_ref, effect.before_sha, commit.sha)
                self._remote_cas_in(db, effect.target_ref, effect.before_sha, commit.sha)
                self._write_pr_in(db, effect.subject, pr)
        elif operation == "completion_comment":
            with sqlite3.connect(self.database) as db:
                closed = db.execute("SELECT rowid FROM closed WHERE issue=?", (payload["issue"],)).fetchone()
            if closed is not None:
                return EffectResult("third")
            if authorize is not None:
                authorize()
            with sqlite3.connect(self.database) as db:
                db.execute("INSERT OR IGNORE INTO comments(intent,issue,actor,body,baseline_event_id) VALUES (?,?,?,?,0)",
                           (effect.effect_id, payload["issue"], effect.actor, wire_json(dict(payload))))
        elif operation == "close":
            observed = self.reconcile(effect)
            if observed.disposition != "prior":
                return observed
            if authorize is not None:
                authorize()
            with sqlite3.connect(self.database) as db:
                db.execute("INSERT OR IGNORE INTO closed VALUES (?,?,?,?)",
                           (payload["issue"], effect.effect_id, effect.actor, payload["merge_sha"]))
                db.execute("INSERT INTO issue_events(issue,event,actor,intent) VALUES (?,'closed',?,?)",
                           (payload["issue"], effect.actor, effect.effect_id))
        elif operation == "reopen_ticket":
            if authorize is not None:
                authorize()
            outcome = self._inspect_reopen(effect)
            if outcome.disposition != "prior":
                return outcome
            if authorize is not None:
                authorize()
            with sqlite3.connect(self.database) as db:
                db.execute("DELETE FROM closed WHERE issue=?", (payload["issue"],))
                db.execute("INSERT INTO issue_events(issue,event,actor,intent) VALUES (?,'reopened',?,?)",
                           (payload["issue"], effect.actor, effect.effect_id))
            outcome = self._inspect_reopen(effect)
        elif operation == "recovery_comment":
            outcome = self._inspect_recovery_comment(effect)
            if outcome.disposition != "prior":
                return outcome
            if authorize is not None:
                authorize()
            if self._inspect_recovery_comment(effect) != outcome:
                raise KernelError("ticket_recovery_boundary_changed")
            body = (RemoteEffects._marker(effect) + "\n" + payload["comment_body"]).encode()
            with sqlite3.connect(self.database) as db:
                db.execute("INSERT INTO comments(intent,issue,actor,body,baseline_event_id) VALUES (?,?,?,?,0)",
                           (effect.effect_id, payload["issue"], effect.actor, body))
            outcome = self._inspect_recovery_comment(effect)
        else:
            raise KernelError("unsupported_offline_verb", operation)
        result = outcome or EffectResult("target", commit, expected, pr, raw)
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

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation in self.force_read:
            return EffectResult(self.force_read[effect.operation])
        operation = effect.operation
        if operation in LocalEffects.OPERATIONS:
            return self.local.reconcile(effect)
        if operation == "cleanup_remote_ref":
            return self._inspect_remote_cleanup(effect)
        if operation == "retire_pr":
            return self._inspect_retirement(effect)
        if operation == "reopen_ticket":
            return self._inspect_reopen(effect)
        if operation == "recovery_comment":
            return self._inspect_recovery_comment(effect)
        if operation == "issue_link":
            with sqlite3.connect(self.database) as db:
                row = db.execute("SELECT ref,sha,intent FROM links WHERE issue=?", (effect.payload["issue"],)).fetchone()
            return EffectResult("target" if row == (effect.payload["ref"], effect.payload["source_head"], effect.effect_id) else "prior" if row is None else "third")
        if operation in {"push", "run_branch"}:
            remote = self.remote_refs().get(effect.source_ref)
            if operation == "push":
                return EffectResult("target" if remote == effect.after_sha else "prior" if remote in {None, effect.before_sha} else "third",
                                    commit=None if remote is None else self.git.commit(remote))
            actual = self.git.commit(effect.source_ref)
            return EffectResult("target" if actual.sha == remote == effect.after_sha else "prior" if actual.sha == effect.before_sha and remote in {None, effect.before_sha} else "third", commit=actual)
        if operation == "pr_write":
            pr = self.pull_request(effect.subject, effect.source_ref)
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
            pr = self.pull_request(effect.subject, effect.source_ref)
            if pr is None:
                return EffectResult("unknown")
            if (pr.number != effect.pr_number or pr.repository != effect.repository or pr.source_ref != effect.source_ref
                    or pr.target_ref != effect.target_ref or pr.head.sha != effect.after_sha or pr.actor != effect.actor):
                return EffectResult("third", pr=pr)
            if pr.state == "MERGED" and pr.merged is not None and pr.head.sha == effect.after_sha and pr.actor == effect.actor:
                parents = list(self.git.parents(pr.merged.sha))
                if parents != list(effect.payload["parents"]) or pr.merged.tree != pr.head.tree:
                    return EffectResult("third", pr=pr)
                target = self.remote_commit(effect.target_ref)
                if target is None:
                    return EffectResult("unknown", pr=pr)
                if not self.git.is_ancestor(pr.merged.sha, target.sha):
                    return EffectResult("third", pr=pr)
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
                event = db.execute("SELECT id,event,actor,created_at FROM issue_events WHERE issue=? ORDER BY id DESC LIMIT 1", (effect.payload["issue"],)).fetchone()
                raw = {} if event is None else {"event": {"id": event[0], "event": event[1], "actor": event[2], "created_at": event[3]}}
                return EffectResult("target" if closed == (effect.effect_id, effect.actor, effect.payload["merge_sha"]) else "third", raw=raw)
            if operation == "completion_comment":
                comment = db.execute("SELECT actor,body,rowid,baseline_event_id,created_at FROM comments WHERE intent=?", (effect.effect_id,)).fetchone()
                if comment is None:
                    return EffectResult("prior")
                if comment[0] != effect.actor or comment[1] != wire_json(dict(effect.payload)):
                    return EffectResult("third")
                intent = {"run_id": effect.execution.run_id, "subject": effect.subject, "issue": effect.payload["issue"],
                          "merge_sha": effect.payload["merge_sha"], "actor": effect.actor, "effect_id": effect.effect_id,
                          "pr_number": effect.pr_number, "kind": effect.payload["kind"], "comment_id": comment[2],
                          "baseline_event_id": comment[3], "comment_created_at": comment[4], "comment_body": comment[1].decode()}
                return EffectResult("target", raw={"close_intent": intent})
        return EffectResult("unknown")

    def writes(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database) as db:
            rows = db.execute("SELECT operation,subject,intent,facts FROM writes ORDER BY position").fetchall()
        return [{"operation": operation, "subject": subject, "intent": intent, "facts": json.loads(facts)}
                for operation, subject, intent, facts in rows]

    def _sync_mirror(self, git: LocalGit, ref: str, remote_before: str) -> None:
        """Advance a stale local mirror only to the intent's exact remote parent."""
        local = self.ref_commit(ref)
        if local is None:
            raise KernelError("write_mirror_missing")
        if local.sha != remote_before:
            if not git.is_ancestor(local.sha, remote_before):
                raise KernelError("write_mirror_changed")
            git.compare_and_swap(ref, local.sha, remote_before)

    def external_append(self, ref: str, files: Mapping[str, str], token: str) -> Commit:
        """A separate fixture actor advances an exact ref; never used by the driver."""
        before = self.git.commit(ref)
        checkout = self.root / ("external-" + token)
        self.git.run("worktree", "add", "--detach", str(checkout), before.sha)
        for name, body in files.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        candidate, _ = self.git.candidate(checkout, before.sha, token, (before.sha,))
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


class _ScenarioPorts:
    """The same physical contract as native, with scripted external outcomes."""

    def __init__(self, scenario: Scenario, binding: OperationBinding) -> None:
        self.scenario, self.binding = scenario, binding
        if (binding.repository_root.resolve() != scenario.port.git.root.resolve()
                or binding.repository != scenario.spec.repository
                or binding.parent_number != scenario.spec.parent_issue):
            raise KernelError("synthetic_source_binding")

    def source(self, parent_number: int) -> Requirements:
        source = self.scenario.requirements
        if source is None or source.parent.number != parent_number:
            raise KernelError("synthetic_source_not_configured")
        return _physical_read(lambda: self.scenario.port.source(source, parent_number))

    def delivery(self, request: DeliveryRead) -> DeliveryFacts:
        scenario = self.scenario
        heads = scenario.check_heads.setdefault(request.source_ref, [])
        facts = _physical_read(lambda: scenario.port.delivery(request, scenario.checks, heads,
                                       publisher_identity=self.binding.inputs.runner_identity))
        if request.source_ref == scenario.delivery.source_ref:
            scenario.heads = heads
        return facts

    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        return _physical_read(lambda: self.scenario.port.inspect_resources(resources))

    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        try:
            result = self.scenario.port.execute(effect, authorize)
        except EffectReadError as error:
            failed = error.write_returncode if error.write_returncode != 0 else None
            raise ReadUnavailable(error.code, error.message, write_returncode=failed,
                                  write_diagnostic=error.write_diagnostic if failed is not None else '') from error
        except GitHubReadError as error:
            raise ReadUnavailable(error.code, error.message) from error
        except LostResponse as error:
            raise ReadUnavailable('write_response_unavailable', bounded_error(str(error))) from error
        if effect.operation == "issue_link":
            return result if result is not None else EffectResult("unknown")
        try:
            return self.reconcile(effect)
        except ReadUnavailable as error:
            code = None if result is None else result.raw.get('write_returncode')
            if result is not None and type(code) is int and code != 0:
                raise ReadUnavailable(error.code, error.message, write_returncode=code,
                    write_diagnostic=str(result.raw.get('write_diagnostic', ''))) from error
            raise

    def reconcile(self, effect: Effect) -> EffectResult:
        return _physical_read(lambda: self.scenario.port.reconcile(effect))

    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]:
        return _physical_read(lambda: self.scenario.port.local.prepare_publication(effect))

    def prepare_retirement(self, effect: Effect) -> Mapping[str, Any]:
        return _physical_read(lambda: self.scenario.port.prepare_retirement(effect))


class Scenario:
    port: OfflinePort
    initial: Commit
    spec: RunSpec
    kernel: Kernel
    requirements: Requirements
    delivery: Delivery
    execution: Execution
    checks: list[str]

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
                            ticket_ci_developments=frozen["ticket_ci_development_limit"], parent_ci_developments=frozen["parent_ci_development_limit"],
                            resync_limit=frozen["integration_resync_limit"], ticket_ci_review=frozen["ticket_ci_review"],
                            concurrency=frozen["ticket_development_workers"],
                            development_deadline_seconds=frozen["invocation_deadlines"]["development"],
                            review_deadline_seconds=frozen["invocation_deadlines"]["review"],
                            publication_deadline_seconds=frozen["invocation_deadlines"]["publication"])
            checks = neutral_script["external"]["required_checks"]
        self.historical_trace: dict[str, Any] | None = None
        self.observation_start = 0
        if kind == DeliveryKind.FINAL_RUN:
            producer = self._completed_source(root, policy, neutral_script)
            self.port, self.initial, self.spec = producer.port, producer.initial, producer.spec
            self.kernel = Kernel(producer.kernel.storage.path)
            self.requirements = producer.requirements
            self.historical_trace = producer.trace()
            self.historical_trace['completion'] = next(row for row in self.kernel.source_status()['completions']
                                                        if row['subject'] == producer.delivery.key)
            self.observation_start = len(self.port.writes())
            self.kernel.control("stop", "historical-source-stop")
            self.kernel.confirm_executor_exit(producer.execution)
            self.kernel.control("resume", "historical-final-resume")
            self.execution = self.kernel.claim_executor("offline-final-executor")
            final = self.kernel.source_status()["final_subject"]
            self.delivery = self.kernel.delivery(final)
        else:
            self.port = OfflinePort.create(root, None if neutral_script is None else neutral_script["git"])
            self.initial = self.port.git.commit("refs/heads/main")
            self.spec, self.delivery = self.port.prepare(kind, policy=policy,
                ticket_number=2 if neutral_script is None else neutral_script["ticket"]["number"] if neutral_script.get("ticket") else 1,
                repository="example/repository" if neutral_script is None else neutral_script["repository"])
            if neutral_script is None:
                parent = ParentIssue(1, "Parent spec", "Deliver the declared synthetic behavior.",
                                     (self.delivery.issue,) if kind == DeliveryKind.TICKET else ())
                issues = (() if kind == DeliveryKind.PARENT_ONLY else
                          (Issue(self.delivery.issue, "Deliver one ticket", "Implement the declared synthetic behavior.",
                                 "OPEN", frozenset({"ready-for-agent"})),))
                self.requirements = Requirements(parent, issues)
            else:
                self.requirements = Requirements.from_wire({"parent": neutral_script["parent"],
                    "issues": [] if kind == DeliveryKind.PARENT_ONLY else [neutral_script["ticket"]]})
            if kind == DeliveryKind.TICKET:
                final = Delivery("final:1", DeliveryKind.FINAL_RUN, self.requirements.parent.number, "untrusted", self.initial,
                    self.delivery.target_ref, "refs/heads/main", checkout=str(self.port.root / "work" / "final-1"),
                    owned_root=self.delivery.owned_root, common_git_dir=self.delivery.common_git_dir)
                self.spec = replace(self.spec, deliveries=(*self.spec.deliveries, final))
            self.kernel = Kernel.create(root / "kernel.sqlite3", self.spec, requirements=self.requirements)
            self.delivery = self.kernel.delivery(self.delivery.key)
            self.execution = self.kernel.claim_executor("offline-executor")
        self.reviews = list(reviews)
        self.checks = list(checks)
        self.review_count = 0
        self.development_count = 0
        self.publication_count = 0
        self.heads: list[str] = []
        self.check_heads: dict[str, list[str]] = {}
        self.role_calls: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.edits = edits
        self.reviewed_heads: list[str] = []

    @staticmethod
    def _completed_source(root: Path, policy: Policy | None, script: Mapping[str, Any] | None) -> Scenario:
        """Produce real historical Completion; never insert accepted state rows."""
        producer: Scenario
        if script is None:
            producer = _CompletedTicketProducer(root, DeliveryKind.TICKET, policy=policy,
                checks=("pass",), edits=({"ticket.txt": "Completed synthetic Ticket.\n"},))
        else:
            historical = script["completed_ticket_precondition"]
            prefix = {**script, "id": script["id"] + "-historical-source", "delivery_kind": "ticket",
                "ticket": {**script["ticket"], "state": "OPEN", "labels": ["ready-for-agent"]},
                "external": {**script["external"], "required_checks": ["pass"]},
                "agents": {
                    "development": [{"edits": {}, "wire": {"result_kind": "development",
                        "summary": "Verify the declared historical tree.", "human_blockers": None}}],
                    "review": [historical["review_artifact"]],
                    "publication": [{"result_kind": "publication", "commit_message": historical["commit_message"],
                        "pr_title": "feat(ticket): accepted historical fixture",
                        "pr_body_markdown": "Accepted historical Ticket with recorded review, checks and exact Git evidence.",
                        "human_blockers": None}]}}
            producer = Scenario(root, DeliveryKind.TICKET, neutral_script=prefix)
        if producer.run().kind != "complete":
            raise KernelError("historical_source_incomplete")
        if script is not None:
            producer.requirements = Requirements.from_wire({"parent": script["parent"], "issues": [script["ticket"]]})
            head = producer.port.remote_commit(producer.delivery.target_ref)
            if head is None:
                raise KernelError("historical_source_incomplete")
            files = {name: producer.port.git.run("show", f"{head.sha}:{name}").stdout for name in
                     producer.port.git.run("ls-tree", "-r", "--name-only", head.sha).stdout.splitlines()}
            budget = producer.kernel.status(producer.delivery.key)["budgets"]["requirements"]
            expected = script["completed_ticket_precondition"]
            if (files != expected["tree_files"] or producer.port.git.run("show", "-s", "--format=%B", head.sha).stdout.strip()
                    != expected["commit_message"].strip() or
                    (budget["developments_used"], budget["reviews_used"], budget["window"]) !=
                    tuple(expected["historical_budget"][key] for key in ("development_attempts", "reviewer_invocations", "window"))):
                raise KernelError("historical_fixture_input_changed")
        for _ in range(10):
            if producer.kernel.source_status()["final_subject"] is not None:
                return producer
            producer.step()
        raise KernelError("historical_final_not_activated")

    def writes(self) -> list[dict[str, Any]]:
        """This observer's new calls; the historical producer retains its journal."""
        return self.port.writes()[getattr(self, "observation_start", 0):]

    def world(self) -> World:
        return self.kernel.observe_control(self.delivery.key, self.make_ports)

    def make_ports(self, binding: OperationBinding) -> OperationPorts:
        return _ScenarioPorts(self, binding)

    def runner(self) -> OperationRunner:
        return self.kernel.operation_runner(self.execution, self.make_ports)

    def agent(self, call: AgentCall) -> bytes:
        delivery = self.kernel.delivery(call.subject)
        self.role_calls.append({"role": call.role.value, "purpose": call.purpose.value,
                                "owner_kind": delivery.kind.value, "attempt": call.attempt_id,
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
            assert Path(call.context["checkout"]) != Path(delivery.checkout)
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
        runner = self.runner()
        step = runner.run_once()
        if step.call is not None:
            runner.authorize_agent(step.call)
            self.kernel.receive(step.call, self.agent(step.call))
        self.checkpoints.append(self.kernel.status(self.delivery.key))
        return step

    def run(self, *, approve: bool = False, max_steps: int = 200, cut: str = "complete") -> Step:
        for _ in range(max_steps):
            state = self.kernel.status(self.delivery.key)
            if cut == "publication_pending" and state["phase"] == "publish":
                return Step("publication_pending", self.delivery.key)
            cleanup = state["resource_cleanup"]
            if state["phase"] == "complete" and (cleanup is None or cleanup["state"] == "complete"):
                return Step("complete", self.delivery.key)
            step = self.step()
            if step.subject and step.subject != self.delivery.key:
                continue  # The assertion focus never chooses Core's work.
            if not step.subject:
                if step.kind == "complete":
                    return Step("complete", self.delivery.key, step.reason)
                state = self.kernel.status(self.delivery.key)
                blocked = self.kernel.source_status()["blocked"]
                if state["phase"] == "approval":
                    step = Step("approval", self.delivery.key)
                elif state["gates"] or state["stopped"] or blocked:
                    reason = state["gates"][0] if state["gates"] else blocked or "operator_stopped"
                    step = Step("paused", self.delivery.key, reason)
                elif step.kind == "quiescent":
                    step = Step("waiting", self.delivery.key, "quiescent", retry_at=step.retry_at)
            if step.kind == "approval" and approve:
                self.kernel.control("approve", f"approval-{self.publication_count}", subject=self.delivery.key, world=self.world())
            elif step.kind in {"paused", "approval", "waiting"}:
                return step
        raise AssertionError("offline runner failed to reach the requested observable boundary")

    def cleanup(self, *, max_steps: int = 16) -> Step:
        return self.run(max_steps=max_steps, cut="cleanup")

    def trace(self) -> dict[str, Any]:
        return {"role_calls": self.role_calls, "checkpoints": self.checkpoints,
                "raw_backend_calls": {"development": self.development_count, "review": self.review_count,
                                      "publication": self.publication_count},
                "external_writes": self.writes(), "final": self.kernel.status(self.delivery.key),
                **({"historical_source": self.historical_trace} if getattr(self, "historical_trace", None) else {})}

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
                "checkpoints": self.checkpoints, "external_writes": self.writes(),
                "final": self.kernel.status(self.delivery.key), "terminal": boundary.kind,
                "world": asdict(world), "git_evidence": refs}

    def neutral_observation(self, script: Mapping[str, Any], input_digest: str) -> dict[str, Any]:
        """Actual same-input facts; cleanup differences are deliberately retained."""
        import hashlib
        pr = self.port.pull_request(self.delivery.key, self.delivery.source_ref)
        if pr is None or pr.merged is None or not self.reviewed_heads:
            raise KernelError("neutral_cut_not_reached")
        heads = {"initial": self.initial.sha, "candidate": self.reviewed_heads[-1],
                 "published": pr.head.sha, "integrated": pr.merged.sha}
        source_label = {DeliveryKind.TICKET: "ticket", DeliveryKind.PARENT_ONLY: "parent", DeliveryKind.FINAL_RUN: "run"}[self.delivery.kind]
        target_label = "run" if self.delivery.kind == DeliveryKind.TICKET else "main"
        labels = {self.delivery.source_ref: source_label, self.delivery.target_ref: target_label, "refs/heads/main": "main"}
        delivery_type = {DeliveryKind.TICKET: "Ticket", DeliveryKind.PARENT_ONLY: "Parent-only", DeliveryKind.FINAL_RUN: "Final Run"}[self.delivery.kind]
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
            name = labels.get(ref, ref)
            refs[name] = head_identity(head)
        with sqlite3.connect(self.port.database) as db:
            closed = db.execute("SELECT issue,intent,actor,merge_sha FROM closed ORDER BY issue").fetchall()
            comments = db.execute("SELECT intent,issue,actor,body FROM comments ORDER BY rowid").fetchall()
            linked = db.execute("SELECT issue,ref,sha,intent FROM links ORDER BY issue").fetchall()
        if getattr(self, "observation_start", 0):
            intents = {write["intent"] for write in self.writes()}
            closed = [row for row in closed if row[1] in intents]
            comments = [row for row in comments if row[0] in intents]
            linked = [row for row in linked if row[3] in intents]
        mutations: list[dict[str, Any]] = []
        for _, issue, _, raw in comments:
            body = json.loads(raw)
            mutation = {"action": "completion_comment" if self.delivery.kind == DeliveryKind.TICKET else "parent_completion_comment",
                        "ticket_number" if self.delivery.kind == DeliveryKind.TICKET else "parent_number": issue,
                        "pr_number": pr.number, "integrated_sha": head_identity(body["merge_sha"])}
            if self.delivery.kind != DeliveryKind.TICKET:
                mutation["delivery_type"] = delivery_type
            mutations.append(mutation)
        for issue, _, _, merge_sha in closed:
            mutation = {"action": "close_issue" if self.delivery.kind == DeliveryKind.TICKET else "close_parent_issue",
                        "ticket_number" if self.delivery.kind == DeliveryKind.TICKET else "parent_number": issue,
                        "pr_number": pr.number, "integrated_sha": head_identity(merge_sha)}
            if self.delivery.kind != DeliveryKind.TICKET:
                mutation["delivery_type"] = delivery_type
            mutations.append(mutation)
        for write in self.writes():
            if write["operation"] == "cleanup_remote_ref" and self.delivery.source_ref not in self.port.remote_refs():
                mutations.append({"action": "delete_managed_branch", "branch": source_label})
        current = self.kernel.status(self.delivery.key)
        projected = self.observable(script["id"], Step("complete", self.delivery.key))
        with self.kernel.storage.transaction(write=False) as tx:
            publication = tx.one("SELECT result_id FROM attempts WHERE subject=? AND generation=? AND role='publication' AND status='consumed' ORDER BY ordinal DESC LIMIT 1",
                                 (self.delivery.key, current["generation"]))
            publication_wire = json.loads(tx.get(publication[0], self.delivery.key))
        approval = any(checkpoint["phase"] == "approval" for checkpoint in self.checkpoints)
        historical = script.get("completed_ticket_precondition")
        history = getattr(self, "historical_trace", None)
        display_attempts = [{"issue_number": issue, "branch": labels.get(ref, ref),
                             "head_sha": head_identity(head)} for issue, ref, head, _ in linked]
        context_paths = {"review": current["reviewed_checkout"], "publication": self.delivery.checkout}
        if self.development_count:
            context_paths = {"development": self.delivery.checkout, **context_paths}
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
                               "branch": None if branch is None else labels.get(branch, branch),
                               "detached": "detached" in fields, "prunable": values.get("prunable")})
        return {"schema": "kernel-neutral-ticket-observation/1", "input_sha256": input_digest,
                "implementation": "independent-kernel-exploration", "cut": script["cut"],
                "roles": projected["role_calls"], "raw_backend_calls": {"development": self.development_count,
                    "review": self.review_count, "publication": self.publication_count},
                "status": ("ticket_completed" if self.delivery.kind == DeliveryKind.TICKET else "completed") if current["phase"] == "complete" else current["phase"],
                "approval_required": current["phase"] == "approval", "budget": {"initial_development": current["budgets"]["integration" if self.delivery.kind == DeliveryKind.FINAL_RUN else "requirements"]["developments_used"],
                    "initial_review": current["budgets"]["integration" if self.delivery.kind == DeliveryKind.FINAL_RUN else "requirements"]["reviews_used"]},
                "commits": {name: actual_commit(head) for name, head in heads.items()}, "local_refs": refs,
                "published_refs": {labels.get(ref, ref): head_identity(head)
                                   for ref, head in self.port.remote_refs().items() if ref in {self.delivery.source_ref, self.delivery.target_ref}},
                "pull_request": {"title": pr.title, "body": pr.body, "state": pr.state,
                                 "primary_ticket": self.delivery.issue if self.delivery.kind == DeliveryKind.TICKET else None},
                "closed_issues": [row[0] for row in closed], "mutations": mutations,
                "native_branch_display": {"observed": True, "calls": [{**attempt, "result": "linked"} for attempt in display_attempts],
                                          "fixture_attempts": display_attempts, "linked_branches": {str(issue): labels.get(ref, ref) for issue, ref, _, _ in linked},
                                          "durable_outcome": {"display_attempted": current["issue_link"] is not None,
                                                              "status": None if current["issue_link"] is None else current["issue_link"]["state"]}},
                "post_close_worktrees": {"observed": True, "registrations": registered,
                                         "role_checkouts": {role: {"observed": True, "path": path, "exists": Path(path).exists(), "registered": f"worktree {path}" in registry} for role, path in context_paths.items()}},
                "publication_wire": publication_wire, "cross_architecture_compared": False,
                "delivery_kind": "full_parent" if self.delivery.kind == DeliveryKind.FINAL_RUN else self.delivery.kind.value,
                "approval_boundary": ("run_approval_pending" if self.delivery.kind == DeliveryKind.FINAL_RUN else "parent_approval_pending") if approval else None,
                "historical_input": None if historical is None or history is None else {
                    "kind": "completed Ticket from an authenticated historical producer",
                    "budget": {"development_attempts": history["final"]["budgets"]["requirements"]["developments_used"],
                               "reviewer_invocations": history["final"]["budgets"]["requirements"]["reviews_used"]},
                    "head": history["completion"]["merge_sha"]},
                "source_ref_identity": self.delivery.source_ref.removeprefix("refs/heads/"),
                "known_gaps": ["GitHub Issue branch linking uses a persistent synthetic remote, not the native online API"],
                "raw_effects": self.writes(),
                **({"historical_source": self.historical_trace} if getattr(self, "historical_trace", None) else {})}


class _CompletedTicketProducer(Scenario):
    def agent(self, call: AgentCall) -> bytes:
        raw = super().agent(call)
        if call.role == Role.PUBLICATION:
            value = json.loads(raw)
            value["commit_message"] = "completed fixture ticket"
            return wire_json(value)
        return raw


class GraphScenario(Scenario):
    """Native source reader plus the same one-request driver, using real Git."""

    def __init__(self, root: Path, requirements: Requirements, *, policy: Policy | None = None,
                 reviews: Sequence[str] = ("pass",), initial_git: Mapping[str, Any] | None = None) -> None:
        self.requirements = requirements
        self.script = None
        self.port = OfflinePort.create(root, initial_git)
        base = self.port.git.commit("refs/heads/main")
        self.initial = base
        run_ref = "refs/heads/run"
        if requirements.parent.sub_issue_numbers:
            self.port.git.run("update-ref", run_ref, base.sha)
            self.port._remote_cas(run_ref, base.sha, base.sha)
        work = (root / "work").resolve()
        work.mkdir()
        deliveries = []
        common = str((self.port.git.root / ".git").resolve())
        for number in requirements.parent.sub_issue_numbers:
            source_ref = f"refs/heads/ticket-{number}"
            checkout = work / f"ticket-{number}"
            # Name the resource only. The first workspace effect creates its
            # ref and checkout at the Core's allocated dependency-visible head.
            deliveries.append(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "untrusted", base, source_ref, run_ref, checkout=str(checkout), owned_root=str(work), common_git_dir=common))
        if deliveries:
            deliveries.append(Delivery("final:1", DeliveryKind.FINAL_RUN, requirements.parent.number, "untrusted", base,
                              run_ref, "refs/heads/main", checkout=str(work / "final-1"), owned_root=str(work), common_git_dir=common))
        else:
            deliveries.append(Delivery("parent:1", DeliveryKind.PARENT_ONLY, requirements.parent.number, "untrusted", base,
                              "refs/heads/parent", "refs/heads/main", checkout=str(work / "parent-1"), owned_root=str(work), common_git_dir=common))
        self.spec = RunSpec("offline-run", "example/project", requirements.parent.number, "untrusted", "untrusted", tuple(deliveries), policy or Policy(),
                           language="zh", repository_root=str(self.port.git.root.resolve()), inputs=fixture_inputs())
        self.kernel = Kernel.create(root / "kernel.sqlite3", self.spec, requirements=requirements)
        self.execution = self.kernel.claim_executor("offline-executor")
        self.delivery = self.kernel.delivery(deliveries[0].key)
        self.reviews, self.checks = list(reviews), ["none"]
        self.review_count = self.development_count = self.publication_count = 0
        self.heads: list[str] = []
        self.check_heads: dict[str, list[str]] = {}
        self.role_calls: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.edits = None
        self.reviewed_heads: list[str] = []

    def select(self, subject: str) -> None:
        self.delivery = self.kernel.delivery(subject)
        self.heads = []

    def refresh(self) -> dict[str, Any]:
        self.world()
        return self.kernel.source_status()

    def agent(self, call: AgentCall) -> bytes:
        raw = super().agent(call)
        self.role_calls[-1]["subject"] = call.subject
        self.role_calls[-1]["context"] = dict(call.context)
        return raw



def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run one isolated replacement-kernel scenario on newly created data")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", required=True, choices=("ticket-normal", "ticket-review-repair", "ticket-fallback",
                        "parent-only-normal", "full-parent-normal", "full-parent-review-repair"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="Exact neutral delivery script, shared with the unchanged baseline adapter")
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
