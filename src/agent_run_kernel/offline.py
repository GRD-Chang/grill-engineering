"""Executable synthetic-GitHub/Agent adapter, backed by real Git and SQLite.

This entire module is exploration cost, not a production GitHub/Worker adapter.
Its scripts control external outcomes, never internal kernel transitions.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .effects import LocalGit
from .kernel import Kernel
from .model import (AgentCall, Checks, Commit, Delivery, DeliveryKind, Effect, EffectResult,
                    KernelError, Policy, PullRequest, Role, RunSpec, Step, World, wire_json)


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
        self.root = root
        self.git = LocalGit(root / "authority")
        self.database = root / "remote.sqlite3"
        self.lose_response: set[str] = set()
        self.force_read: dict[str, str] = {}

    @classmethod
    def create(cls, root: Path) -> OfflinePort:
        authority = root / "authority"
        authority.mkdir(parents=True, exist_ok=False)
        port = cls(root)
        port.git.run("init", "--initial-branch=main")
        port.git.run("config", "maintenance.auto", "false")
        port.git.run("config", "gc.auto", "0")
        (authority / "README.md").write_text("Synthetic repository, no user data.\n")
        port.git.run("add", "--all")
        port.git.run("commit", "-m", "initial fixture")
        with sqlite3.connect(port.database) as db:
            db.executescript("""
                CREATE TABLE prs(number INTEGER PRIMARY KEY,subject TEXT UNIQUE,body BLOB NOT NULL);
                CREATE TABLE receipts(id TEXT PRIMARY KEY,body BLOB NOT NULL);
                CREATE TABLE writes(position INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,subject TEXT,intent TEXT,facts BLOB);
                CREATE TABLE closed(issue INTEGER PRIMARY KEY,intent TEXT,actor TEXT,merge_sha TEXT);
                CREATE TABLE comments(intent TEXT PRIMARY KEY,issue INTEGER,actor TEXT,body BLOB);
            """)
        return port

    def prepare(self, kind: DeliveryKind, *, policy: Policy | None = None) -> tuple[RunSpec, Delivery]:
        base = self.git.commit("refs/heads/main")
        source = base
        key = {DeliveryKind.TICKET: "ticket:2", DeliveryKind.PARENT_ONLY: "parent:1", DeliveryKind.FINAL_RUN: "final:1"}[kind]
        source_ref = "refs/heads/run" if kind == DeliveryKind.FINAL_RUN else "refs/heads/task"
        target_ref = "refs/heads/run" if kind == DeliveryKind.TICKET else "refs/heads/main"
        if kind == DeliveryKind.FINAL_RUN:
            # This is copied/synthetic completed-Ticket input, not a new Agent result.
            self.git.run("switch", "-c", "fixture-completed-ticket")
            (self.git.root / "ticket.txt").write_text("Completed synthetic Ticket.\n")
            self.git.run("add", "--all")
            self.git.run("commit", "-m", "completed fixture ticket")
            source = self.git.commit("HEAD")
            self.git.run("switch", "main")
        self.git.run("update-ref", source_ref, source.sha)
        self.git.run("update-ref", target_ref, base.sha)
        checkout = self.root / "work" / key.replace(":", "-")
        checkout.parent.mkdir(exist_ok=True)
        self.git.run("worktree", "add", "--detach", str(checkout), source.sha)
        delivery = Delivery(key, kind, 2 if kind == DeliveryKind.TICKET else 1, "subject-v1", source,
                            source_ref, target_ref, completion_revision="no-tickets", checkout=str(checkout))
        spec = RunSpec("offline-run", "example/repository", 1, "parent-v1", "graph-v1", (delivery,), policy or Policy())
        return spec, delivery

    def pull_request(self, subject: str) -> PullRequest | None:
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT body FROM prs WHERE subject=?", (subject,)).fetchone()
        if row is None:
            return None
        pr = _pr(json.loads(row[0]))
        # GitHub's base/head are live remote facts, not frozen intent fields.
        return PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref,
                           self.git.commit(pr.source_ref), self.git.commit(pr.target_ref), pr.title, pr.body,
                           pr.state, pr.actor, pr.merged)

    def world(self, spec: RunSpec, delivery: Delivery, checks_result: str = "none") -> World:
        pr = self.pull_request(delivery.key)
        checks = None
        if pr is not None:
            details = () if checks_result == "none" else (("required", checks_result),)
            checks = Checks(pr.number, pr.head.sha, checks_result, details,
                            "Raw synthetic failed job log" if checks_result == "fail" else "")
        return World(spec.parent_revision, spec.graph_revision, delivery.revision,
                     self.git.commit(delivery.target_ref), self.git.commit(delivery.source_ref), pr, checks,
                     delivery.completion_revision,
                     ((self.git.commit(delivery.target_ref).sha, self.git.commit(delivery.source_ref).sha),)
                     if self.git.is_ancestor(self.git.commit(delivery.target_ref).sha, self.git.commit(delivery.source_ref).sha) else ())

    def _write_pr(self, subject: str, pr: PullRequest) -> None:
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO prs VALUES (?,?,?) ON CONFLICT(number) DO UPDATE SET body=excluded.body",
                       (pr.number, subject, wire_json(asdict(pr))))

    def _receipt(self, effect: Effect, result: EffectResult) -> None:
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?)", (effect.effect_id, wire_json(asdict(result))))

    def execute(self, effect: Effect, authorize: Callable[[], None] | None = None) -> None:
        operation = effect.operation
        payload = effect.payload
        git = LocalGit(self.git.root, authorize)
        commit: Commit | None = None
        pr: PullRequest | None = None
        expected: str | None = None
        raw: dict[str, Any] = {}
        try:
            if operation == "candidate":
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
                            return
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
                git.compare_and_swap(effect.source_ref, effect.before_sha, effect.after_sha)
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
                                 self.git.commit(effect.source_ref), self.git.commit(effect.target_ref),
                                 payload["title"], payload["body"], actor=effect.actor)
                self._write_pr(effect.subject, pr)
            elif operation == "merge":
                pr = self.pull_request(effect.subject)
                if pr is None or pr.head.sha != effect.after_sha or pr.base.sha != effect.before_sha or pr.state != "OPEN":
                    raise KernelError("merge_compare_and_swap")
                commit = git.make_commit(pr.head.tree, payload["parents"], payload["commit_message"])
                git.compare_and_swap(effect.target_ref, effect.before_sha, commit.sha)
                pr = PullRequest(pr.number, pr.repository, pr.source_ref, pr.target_ref, pr.head, pr.base,
                                 pr.title, pr.body, "MERGED", effect.actor, commit)
                self._write_pr(effect.subject, pr)
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
            return
        self._receipt(effect, EffectResult("target", commit, expected, pr, raw))
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO writes(operation,subject,intent,facts) VALUES (?,?,?,?)",
                       (operation, effect.subject, effect.effect_id, wire_json({"before": effect.before_sha, "after": effect.after_sha})))
        if operation in self.lose_response:
            self.lose_response.remove(operation)
            raise LostResponse(f"{operation} took effect; response lost")

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation in self.force_read:
            return EffectResult(self.force_read[effect.operation])
        operation = effect.operation
        if operation in {"push", "run_branch"}:
            actual = self.git.commit(effect.source_ref)
            return EffectResult("target" if actual.sha == effect.after_sha else "prior" if actual.sha == effect.before_sha else "third", commit=actual)
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
            if pr.state == "MERGED" and pr.merged is not None and pr.head.sha == effect.after_sha and pr.actor == effect.actor:
                if effect.before_sha not in self.git.parents(pr.merged.sha) or pr.merged.tree != pr.head.tree:
                    return EffectResult("third", pr=pr)
                return EffectResult("target", commit=pr.merged, pr=pr)
            return EffectResult("prior" if pr.state == "OPEN" and pr.head.sha == effect.after_sha and pr.base.sha == effect.before_sha else "third", pr=pr)
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
        self.git.run("worktree", "remove", str(checkout))
        return candidate


class Scenario:
    def __init__(self, root: Path, kind: DeliveryKind, *, reviews: Sequence[str] = ("pass",),
                 checks: Sequence[str] = ("none",), policy: Policy | None = None,
                 edits: Sequence[Mapping[str, str]] | None = None) -> None:
        self.port = OfflinePort.create(root)
        self.spec, self.delivery = self.port.prepare(kind, policy=policy)
        self.kernel = Kernel.create(root / "kernel.sqlite3", self.spec)
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
            edits = ({f"development-{self.development_count}.txt": f"Change {self.development_count}\n"} if self.edits is None
                     else self.edits[min(self.development_count - 1, len(self.edits) - 1)])
            for name, body in edits.items():
                path = Path(call.context["checkout"]) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            return wire_json({"result_kind": "development", "summary": f"Changed synthetic behavior {self.development_count}", "human_blockers": None})
        if call.role == Role.REVIEW:
            outcome = self.reviews[min(self.review_count, len(self.reviews) - 1)]
            self.review_count += 1
            return wire_json(review_result(outcome))
        self.publication_count += 1
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
                    self.port.execute(effect, lambda: self.kernel.guard_effect(effect, self.execution, self.world()))
                except LostResponse:
                    pass
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
                return step
        raise AssertionError("offline driver failed to reach a stable observable boundary")

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
            commit = self.port.git.commit(ref)
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


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run one isolated replacement-kernel scenario on newly created data")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", required=True, choices=("ticket-normal", "ticket-review-repair", "ticket-fallback",
                        "parent-only-normal", "full-parent-normal", "full-parent-review-repair"))
    parser.add_argument("--output", type=Path, required=True)
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
    scenario = Scenario(args.root, kind, reviews=reviews)
    boundary = scenario.run(approve=kind != DeliveryKind.TICKET, cut=cut)
    result = {"schema": "kernel-observable-trace/1", "implementation": "independent-kernel-exploration",
              "cross_architecture_compared": False, "scenarios": [scenario.observable(args.scenario, boundary)]}
    args.output.write_bytes(wire_json(result) + b"\n")
    print(json.dumps({"scenario": args.scenario, "terminal": boundary.kind, "output": str(args.output)}))


if __name__ == "__main__":
    main()
