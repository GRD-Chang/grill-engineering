"""Executable synthetic-GitHub/Agent adapter, backed by real Git and SQLite.

This entire module is exploration cost, not a production GitHub/Worker adapter.
Its scripts control external outcomes, never internal kernel transitions.
"""

from __future__ import annotations

from support.repository_fixture import create_kernel
from agent_run.core.operation_runner import observe_control

from contextlib import nullcontext
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Callable, Mapping, Sequence, TypeVar

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.contracts.write_authority import WriteAuthority, admit_write
from agent_run.errors import bounded_error
from agent_run.errors import GitHubReadError
from agent_run.config.resources import resolve_resources

from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.local_git import LocalEffects
from agent_run.adapters.remote import EffectReadError, RemoteEffects
from .offline_api import ExternalObjects, SyntheticGitHub
from .offline_git import OfflineGit
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import DeliveryFacts, DeliveryRead, OperationBinding, OperationPorts, ReadUnavailable, requested_ancestry
from agent_run.core.operation_runner import OperationRunner
from agent_run.model import AgentCall, Checks, Commit, CreationInputs, Delivery, DeliveryKind, Effect, EffectResult, Execution, Issue, ParentIssue, Policy, PullRequest, Requirements, ResourceInspection, ResourceWitness, Role, RunSpec, Step, World, wire_json
from agent_run.errors import KernelError


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


_SHARED_REMOTE = frozenset({"pr_write", "merge", "completion_comment", "close", "retire_pr", "reopen_ticket", "recovery_comment", "push", "run_branch", "cleanup_remote_ref"})


class OfflinePort:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise ValueError("offline world root must not redirect paths")
        self.root = root.resolve()
        self.git = LocalGit(self.root / "authority")
        self.git.env.update({"GIT_AUTHOR_NAME": "Offline Publisher", "GIT_AUTHOR_EMAIL": "offline@example.invalid",
                             "GIT_COMMITTER_NAME": "Offline Publisher", "GIT_COMMITTER_EMAIL": "offline@example.invalid",
                             "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"})
        self.database = self.root / "remote.sqlite3"
        self.remote_name = "offline-remote"
        self._objects = ExternalObjects(self.database) if self.database.exists() else None
        self.lose_response: set[str] = set()
        self.read_faults: set[str] = set()

    @property
    def objects(self) -> ExternalObjects:
        if self._objects is None:
            self._objects = ExternalObjects(self.database)
        return self._objects

    @classmethod
    def create(cls, root: Path, initial_git: Mapping[str, Any] | None = None) -> OfflinePort:
        port = cls(root)
        authority = port.root / "authority"
        authority.mkdir(parents=True, exist_ok=False)
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
        port._initialize_external_objects()
        return port

    def _initialize_external_objects(self) -> None:
        with sqlite3.connect(self.database) as db:
            db.executescript("""
                CREATE TABLE writes(position INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT,subject TEXT,intent TEXT,facts BLOB);
                CREATE TABLE links(issue INTEGER PRIMARY KEY,ref TEXT NOT NULL,sha TEXT NOT NULL,intent TEXT NOT NULL);
            """)
            ExternalObjects.create(db)
        self.physical.initialize({"refs/heads/main": self.git.commit("HEAD").sha})

    def prepare(self, kind: DeliveryKind, *, policy: Policy | None = None,
                ticket_number: int = 2, repository: str = "example/repository") -> tuple[RunSpec, Delivery]:
        if kind == DeliveryKind.FINAL_RUN:
            raise KernelError("final_requires_completed_source")
        self.objects.bind(repository, {number: 'open' for number in ([1, ticket_number] if kind == DeliveryKind.TICKET else [1])})
        base = self.git.commit("refs/heads/main")
        source = base
        key = f"ticket:{ticket_number}" if kind == DeliveryKind.TICKET else "parent:1"
        source_ref = "refs/heads/task"
        target_ref = "refs/heads/run" if kind == DeliveryKind.TICKET else "refs/heads/main"
        self.git.run("update-ref", source_ref, source.sha)
        self.git.run("update-ref", target_ref, base.sha)
        if target_ref != "refs/heads/main":
            response = self.physical.push(target_ref, None, base.sha)
            if response.returncode:
                raise KernelError("offline_initial_ref_failed", response.stderr)
        checkout = self.root / "work" / key.replace(":", "-")
        checkout.parent.mkdir(exist_ok=True)
        # The shared Core workspace effect creates and authenticates checkout.
        delivery = Delivery(key, kind, ticket_number if kind == DeliveryKind.TICKET else 1, "subject-v1", source,
                            source_ref, target_ref, completion_revision="no-tickets", checkout=str(checkout),
                            owned_root=str(checkout.parent.resolve()), common_git_dir=str((self.git.root / ".git").resolve()))
        spec = RunSpec("offline-run", repository, 1, "parent-v1", "graph-v1", (delivery,), policy or Policy(),
                       language="zh", repository_root=str(self.git.root.resolve()), inputs=fixture_inputs())
        return spec, delivery


    def source(self, requirements: Requirements, parent_number: int) -> Requirements:
        if requirements.parent.number != parent_number:
            raise KernelError("synthetic_source_binding")
        closed = self.objects.closed_issues()
        return replace(requirements, issues=tuple(
            replace(issue, state="CLOSED" if issue.number in closed else issue.state,
                    blocked_by=tuple(replace(blocker, state="CLOSED") if blocker.number in closed else blocker
                                     for blocker in issue.blocked_by)) for issue in requirements.issues))

    def pr_by_number(self, number: int) -> PullRequest | None:
        return self._remote(self.objects.repository).pull_request(number)

    def delivery(self, request: DeliveryRead, checks_script: Sequence[str], heads: list[str], *,
                 publisher_identity: Mapping[str, Any] | None = None) -> DeliveryFacts:
        """Read the synthetic remote; revisions and World belong to Core."""
        if type(request.read_checks) is not bool or not request.read_checks and request.check_head is not None:
            raise ReadUnavailable("checks_request_invalid", "An exact checks head requires a checks observation")
        requested_ancestry(request, None)
        pr = (None if not request.read_pr else self.pr_by_number(request.pr_number)
              if request.pr_number is not None else self.pull_request_by_ref(request.source_ref, request.target_ref))
        checks = None
        if pr is not None and pr.state == "OPEN" and request.read_checks:
            if request.check_head is not None and request.check_head != pr.head.sha:
                raise ReadUnavailable("github_observation_pending", "Synthetic checks head is not current")
            if pr.head.sha not in heads:
                heads.append(pr.head.sha)
            checks_result = checks_script[min(max(0, len(heads) - 1), len(checks_script) - 1)]
            details = () if checks_result == "none" else (("required", checks_result),)
            checks = Checks(pr.number, pr.head.sha, checks_result, details,
                            "Raw synthetic failed job log" if checks_result == "fail" else "")
        # Source and target belong to one physical ref sample. Re-read on the
        # next observation; object transfer still checks current ownership.
        physical = self.physical
        refs = physical.refs()
        sampled_heads = tuple(dict.fromkeys(refs[ref] for ref in (request.source_ref, request.target_ref)
                                            if ref in refs))
        commits = physical.read_commits(sampled_heads, transfer_missing=True)
        source, target = (commits.get(refs.get(ref)) for ref in (request.source_ref, request.target_ref))
        checked = requested_ancestry(request, source, target, pr)
        ancestry_heads = set(request.anchors) | {head.sha for head in (source, target) if head is not None}
        if request.ancestry_pairs is not None:
            ancestry_heads.update(head for pair in checked or () for head in pair)
            # Even a reflexive or unresolved demand retains anchor object checks.
            physical.require_commits(tuple(sorted(ancestry_heads)))
        pairs = ((before, after) for before in sorted(ancestry_heads) for after in sorted(ancestry_heads)) if request.ancestry_pairs is None else iter(checked or ())
        ancestry = tuple((before, after) for before, after in pairs
                         if before == after or self.git.is_ancestor(before, after))
        return DeliveryFacts(target, source, pr, checks, ancestry, "offline-publisher", publisher_identity, checked,
                             request.read_checks)

    def pull_request_by_ref(self, source_ref: str, target_ref: str) -> PullRequest | None:
        return self._remote(self.objects.repository).find_pull_request(source_ref, target_ref)

    def ancestry(self, source: Commit | None, target: Commit, witnesses: Sequence[str]) -> tuple[tuple[str, str], ...]:
        before_heads = set(witnesses) | {target.sha}
        after_heads = {target.sha} if source is None else {target.sha, source.sha}
        return tuple((before, after) for before in sorted(before_heads) for after in sorted(after_heads)
                     if before == after or self.git.is_ancestor(before, after))

    def ref_commit(self, ref: str) -> Commit | None:
        result = self.git.run("for-each-ref", "--format=%(refname)%09%(objectname)", ref)
        heads = [line.partition("\t")[2] for line in result.stdout.splitlines() if line.partition("\t")[0] == ref]
        return None if not heads else self.git.commit(heads[0])

    @property
    def physical(self) -> OfflineGit:
        return OfflineGit(self.root, environment=self.git.env)


    def remote_refs(self) -> dict[str, str]:
        return self.physical.refs()

    def remote_commit(self, ref: str) -> Commit | None:
        physical = self.physical
        head = physical.refs().get(ref)
        if head is None:
            return None
        physical.fetch(head)
        return self.git.commit(head)


    def external_pr_transition(self, number: int, state: str, actor: str) -> None:
        if state not in {'OPEN', 'CLOSED'} or self.objects.pr(number) is None:
            raise ValueError('Expected an existing PR and explicit OPEN/CLOSED state')
        self.objects.set_state(number, state.lower(), actor)


    @property
    def local(self) -> LocalEffects:
        return LocalEffects(self.git, owned_root=self.root / "work")

    def inspect_resources(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        return self.local.inspect_resources(resources)

    def issue_state(self, issue: int) -> str:
        return self.objects.issue_state(issue).upper()


    def prepare_retirement(self, effect: Effect) -> dict[str, Any]:
        return self._remote(effect.repository).prepare_retirement(effect)


    def _remote(self, repository: str, written: Callable[..., None] | None = None) -> RemoteEffects:
        physical = self.physical
        api = SyntheticGitHub(self.git, self.database, repository, physical=physical,
                              written=written or (lambda *args: None), read_faults=self.read_faults)
        git_writes = physical._with_written(api.record_git_write)
        return RemoteEffects(self.git.root, repository, runner_identity={"source": "host"},
                             remote=self.remote_name, api=api, git_writes=git_writes)

    def execute(self, effect: Effect, authorize: WriteAuthority) -> EffectResult | None:
        if effect.operation in _SHARED_REMOTE:
            def written(position: int, response: Mapping[str, Any], result: subprocess.CompletedProcess[str]) -> None:
                with sqlite3.connect(self.database) as db:
                    db.execute("INSERT INTO writes(operation,subject,intent,facts) VALUES (?,?,?,?)",
                               (effect.operation, effect.subject, effect.effect_id,
                                wire_json({"before": effect.before_sha, "after": effect.after_sha})))
                if result.returncode == 0 and effect.operation in self.lose_response:
                    self.lose_response.remove(effect.operation)
                    raise GitHubReadError('write_response_unavailable', effect.operation + ' took effect; response lost')
            return self._remote(effect.repository, written).execute(effect, authorize)
        operation = effect.operation
        payload = effect.payload
        if operation in LocalEffects.OPERATIONS:
            result = self.local.execute(effect, authorize)
        elif operation == "issue_link":
            admit_write(authorize)
            with sqlite3.connect(self.database) as db:
                db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?)", (payload["issue"], payload["ref"], payload["source_head"], effect.effect_id))
            result = EffectResult("target", raw={"acknowledged_status": "linked"})
        else:
            raise KernelError("unsupported_offline_verb", operation)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO writes(operation,subject,intent,facts) VALUES (?,?,?,?)",
                       (operation, effect.subject, effect.effect_id, wire_json({"before": effect.before_sha, "after": effect.after_sha})))
        if operation in self.lose_response:
            self.lose_response.remove(operation)
            raise LostResponse(f"{operation} took effect; response lost")
        return result


    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation in _SHARED_REMOTE:
            return self._remote(effect.repository).reconcile(effect)
        operation = effect.operation
        if operation in LocalEffects.OPERATIONS:
            return self.local.reconcile(effect)
        if operation == "issue_link":
            with sqlite3.connect(self.database) as db:
                row = db.execute("SELECT ref,sha,intent FROM links WHERE issue=?", (effect.payload["issue"],)).fetchone()
            return EffectResult("target" if row == (effect.payload["ref"], effect.payload["source_head"], effect.effect_id) else "prior" if row is None else "third")
        return EffectResult("unknown")

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
        candidate, _ = self.git.candidate(checkout, before.sha, token, (before.sha,))
        if candidate.sha == before.sha:
            candidate = self.git.make_commit(before.tree, (before.sha,), "external append " + token)
        self.git.compare_and_swap(ref, before.sha, candidate.sha)
        if ref in self.remote_refs():
            response = self.physical.push(ref, before.sha, candidate.sha)
            if response.returncode:
                raise KernelError("external_ref_changed", response.stderr)
        self.git.run("worktree", "remove", str(checkout))
        return candidate

    def external_remote_append(self, ref: str, files: Mapping[str, str], token: str) -> Commit:
        before = self.remote_refs()[ref]
        self.physical.fetch(before)
        temporary = "refs/heads/remote-actor-" + token
        self.git.run("update-ref", temporary, before)
        candidate = self.external_append(temporary, files, token)
        self.git.run("update-ref", "-d", temporary, candidate.sha)
        response = self.physical.push(ref, before, candidate.sha)
        if response.returncode:
            raise KernelError("external_ref_changed", response.stderr)
        return candidate


class _ScenarioPorts:
    """The same physical contract as native, with scripted external outcomes."""

    def __init__(self, scenario: Scenario, binding: OperationBinding) -> None:
        self.scenario, self.binding = scenario, binding
        scenario.port.remote_name = binding.inputs.repository_remote
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

    def execute(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        try:
            result = self.scenario.port.execute(effect, authorize)
        except EffectReadError as error:
            failed = error.write_returncode if error.write_returncode != 0 else None
            raise ReadUnavailable(error.code, error.message, write_returncode=failed,
                                  write_diagnostic=error.write_diagnostic if failed is not None else '') from error
        except GitHubReadError as error:
            raise ReadUnavailable(error.code, error.message) from error
        except LostResponse as error:
            if effect.operation == "issue_link":
                return EffectResult("unknown", raw={"display": "indeterminate", "diagnostic": bounded_error(str(error))})
            raise ReadUnavailable('write_response_unavailable', bounded_error(str(error))) from error
        if effect.operation in _SHARED_REMOTE or effect.operation == "issue_link":
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
                 edits: Sequence[Mapping[str, str]] | None = None, neutral_script: Mapping[str, Any] | None = None,
                 record_checkpoints: bool = False) -> None:
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
            producer = self._completed_source(root, policy, neutral_script, record_checkpoints=record_checkpoints)
            self.port, self.initial, self.spec = producer.port, producer.initial, producer.spec
            self.kernel = Kernel(producer.kernel.storage.path, run_id=producer.kernel.storage.run_id)
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
            self.kernel = create_kernel(root / "kernel.sqlite3", self.spec, requirements=self.requirements)
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
        self.record_checkpoints = record_checkpoints
        self.edits = edits
        self.reviewed_heads: list[str] = []

    @staticmethod
    def _completed_source(root: Path, policy: Policy | None, script: Mapping[str, Any] | None,
                          *, record_checkpoints: bool = False) -> Scenario:
        """Produce real historical Completion; never insert accepted state rows."""
        producer: Scenario
        if script is None:
            producer = _CompletedTicketProducer(root, DeliveryKind.TICKET, policy=policy,
                checks=("pass",), edits=({"ticket.txt": "Completed synthetic Ticket.\n"},),
                record_checkpoints=record_checkpoints)
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
            producer = Scenario(root, DeliveryKind.TICKET, neutral_script=prefix, record_checkpoints=record_checkpoints)
        # The historical Ticket is setup for Final. Keep the standalone
        # scripted/diagnostic path and its checkpoints on native storage.
        if script is None and not record_checkpoints:
            from support.run_scoped_kernel import prepare_domain_history
            preparation = prepare_domain_history(producer)
        else:
            preparation = nullcontext()
        with preparation:
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
        return observe_control(self.kernel, self.delivery.key, self.make_ports)

    def make_ports(self, binding: OperationBinding) -> OperationPorts:
        return _ScenarioPorts(self, binding)

    def runner(self) -> OperationRunner:
        return OperationRunner(self.kernel, self.execution, self.make_ports)

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
        from support.core_operations import prepare_scripted_agent
        step = prepare_scripted_agent(self.kernel, step)
        if step.call is not None:
            runner.authorize_agent(step.call)
            self.kernel.receive(step.call, self.agent(step.call))
        if self.record_checkpoints:
            self.checkpoints.append(self.kernel.status(self.delivery.key))
        return step

    def _progress(self):
        # A fixture stopping condition needs no prompt, budget, history or material
        # projection. Read current facts each time; Core alone chooses the work.
        with self.kernel.storage.transaction(write=False) as tx:
            return tx.one("SELECT s.phase,c.state AS cleanup_state,c.outcome FROM subjects s "
                "LEFT JOIN resource_cleanup c ON c.run_id=s.run_id AND c.subject=s.id "
                "AND c.generation=s.generation WHERE s.run_id=? AND s.id=?",
                (self.kernel.storage.run_id, self.delivery.key))

    def run(self, *, approve: bool = False, max_steps: int = 200, cut: str = "complete") -> Step:
        for _ in range(max_steps):
            progress = self._progress()
            if cut == "publication_pending" and progress["phase"] == "publish":
                return Step("publication_pending", self.delivery.key)
            if progress["phase"] == "complete" and progress["cleanup_state"] in {None, "complete"}:
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
                current = self._progress()
                # The operation above attempted cleanup. Preserve the original
                # run() business result while status retains unresolved assets;
                # cleanup() itself still reports its pending/conflict boundary.
                if (cut == "complete" and current["phase"] == "complete"
                        and current["outcome"] in {"third", "unknown"}):
                    return Step("complete", self.delivery.key)
                return step
        raise AssertionError("offline runner failed to reach the requested observable boundary")

    def cleanup(self, *, max_steps: int = 16) -> Step:
        return self.run(max_steps=max_steps, cut="cleanup")

    def trace(self) -> dict[str, Any]:
        return {"role_calls": self.role_calls, "checkpoints": self.checkpoints,
                "raw_backend_calls": {"development": self.development_count, "review": self.review_count,
                                      "publication": self.publication_count},
                "external_writes": self.writes(), "external_objects": self.port.objects.snapshot(),
                "final": self.kernel.status(self.delivery.key),
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
        if not self.record_checkpoints:
            raise ValueError("Neutral observations require record_checkpoints=True")
        import hashlib
        pr = self.port.pull_request_by_ref(self.delivery.source_ref, self.delivery.target_ref)
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

        def head_identity(head: str | None) -> list[str] | str | None:
            return None if head is None else identities.get(head, head)

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
        comments, closed = self.port.objects.completion_trace()
        with sqlite3.connect(self.port.database) as db:
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
            publication = tx.one("SELECT result_id FROM attempts WHERE run_id=? AND subject=? AND generation=? AND role='publication' AND status='consumed' ORDER BY ordinal DESC LIMIT 1",
                                 (self.kernel.storage.run_id, self.delivery.key, current["generation"]))
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
                "raw_effects": self.writes(), "external_objects": self.port.objects.snapshot(),
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
                 reviews: Sequence[str] = ("pass",), initial_git: Mapping[str, Any] | None = None,
                 record_checkpoints: bool = False) -> None:
        self.requirements = requirements
        self.script = None
        self.port = OfflinePort.create(root, initial_git)
        self.port.objects.bind("example/project", {requirements.parent.number: 'open',
            **{i.number: i.state.lower() for i in requirements.issues}})
        base = self.port.git.commit("refs/heads/main")
        self.initial = base
        run_ref = "refs/heads/run"
        if requirements.parent.sub_issue_numbers:
            self.port.git.run("update-ref", run_ref, base.sha)
            response = self.port.physical.push(run_ref, None, base.sha)
            if response.returncode:
                raise KernelError("offline_initial_ref_failed", response.stderr)
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
        self.kernel = create_kernel(root / "kernel.sqlite3", self.spec, requirements=requirements)
        self.execution = self.kernel.claim_executor("offline-executor")
        self.delivery = self.kernel.delivery(deliveries[0].key)
        self.reviews, self.checks = list(reviews), ["none"]
        self.review_count = self.development_count = self.publication_count = 0
        self.heads: list[str] = []
        self.check_heads: dict[str, list[str]] = {}
        self.role_calls: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.record_checkpoints = record_checkpoints
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
    scenario = Scenario(args.root, kind, reviews=reviews, neutral_script=script, record_checkpoints=True)
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
