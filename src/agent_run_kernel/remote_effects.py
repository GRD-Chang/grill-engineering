"""Bounded Git/GitHub effects and exact readback of the original effect intent.

Kernel owns preparation, dispatch, authorization, retries and receipt persistence.
This adapter has no local journal, delivery phase or writable Run representation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, TYPE_CHECKING
from urllib.parse import urlencode

from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_identity import validate_identity_snapshot
from agent_run.github_retry import run_read_command, run_write_command
from agent_run.runner_transport import runner_git_environment

from .cleanup_adapter import cleanup_remote_ref, inspect_remote_ref

from .effects import LocalGit
from .model import Commit, Effect, EffectResult, KernelError, PullRequest, World
from .source_reading import (read_remote_heads, read_requirements, read_required_check_facts,
                             normalize_required_checks)

if TYPE_CHECKING:
    from .kernel import Kernel
from .storage import MATERIAL_LIMIT, Transaction


class ObservationSuperseded(KernelError):
    """This read lost to an independently committed local authority change."""


class EffectReadError(GitHubReadError):
    """A typed read failure after an actual command's response was observed."""

    def __init__(self, error: GitHubReadError, response: subprocess.CompletedProcess[str]) -> None:
        super().__init__(error.code, error.message)
        self.write_returncode = response.returncode
        self.write_diagnostic = response.stderr[:4096]


class RemoteEffects:
    """A concrete execute/reconcile port; authorize is required for every write.

    The caller supplies the Run's frozen Runner reference and selected repository.
    Writes are never retried here. Exact target readback needs no live write grant.
    Failed reads are unknown, never absence, prior, success or permission to retry.
    """
    def __init__(self, root: Path, repository: str, *, runner_identity: Mapping[str, Any],
                 remote: str = "origin") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected an exact GitHub owner/repository")
        if not remote or remote.startswith("-") or "\0" in remote:
            raise ValueError("Expected a remote name or URL")
        self.root, self.repository, self.remote = root, repository, remote
        self.identity = validate_identity_snapshot(runner_identity)
        self.git = LocalGit(root)

    def actor(self) -> str:
        """Resolve the selected Runner's actual login, not a fixture identity."""
        if self.identity["source"] == "app":
            from agent_run.github_auth import resolve_app_login
            from agent_run.github_auth_profile import GitHubAppProfile
            profile = self.identity["app_profile"]
            return resolve_app_login(GitHubAppProfile(profile["app_id"], profile["installation_id"],
                                                     Path(profile["private_key_path"])))
        return _text(self._api("user")["login"])

    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        """One original operation, after exact readback and current authorization."""
        self._binding(effect)
        if effect.operation == "issue_link":
            return self._link_issue(effect, authorize)
        if effect.operation == "cleanup_remote_ref":
            return cleanup_remote_ref(self._cleanup_git(authorize), effect)
        if effect.operation == "retire_pr":
            return self._retire_pr(effect, authorize)
        if effect.operation == "reopen_ticket":
            return self._reopen_ticket(effect, authorize)
        observed = self.reconcile(effect)
        prior = effect.payload.get("prior")
        if (effect.operation == "pr_write" and effect.pr_number is not None and isinstance(prior, Mapping)
                and prior.get("title") == effect.payload.get("title")
                and prior.get("body") == effect.payload.get("body")):
            observed = self._current_pr_edit(effect, observed)
            if observed.disposition != "target":
                return observed
            if self.actor() != effect.actor:
                raise KernelError("remote_actor_changed")
            authorize()
            fresh = self._current_pr_edit(effect, self.reconcile(effect))
            return replace(fresh, disposition="third") if fresh.disposition == "target" and fresh != observed else fresh
        if observed.disposition != "prior":
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        operation = effect.operation
        if operation in {"push", "run_branch"}:
            after = _text(effect.after_sha)
            if not self.git.is_ancestor(effect.before_sha, after):
                raise KernelError("non_append_write")
            expected = self._expected_remote(effect)
            command = ["git", "push", self.remote, f"{after}:{effect.source_ref}",
                       f"--force-with-lease={effect.source_ref}:{expected or ''}"]
            authorize()
            response = run_write_command(command, cwd=self.root, runner_identity=self.identity)
        elif operation == "pr_write":
            body = {"title": _text(effect.payload["title"]), "body": _text(effect.payload["body"])}
            if effect.pr_number is None:
                endpoint, method = f"repos/{self.repository}/pulls", "POST"
                body.update(head=effect.source_ref.removeprefix("refs/heads/"),
                            base=effect.target_ref.removeprefix("refs/heads/"))
            else:
                endpoint, method = f"repos/{self.repository}/pulls/{effect.pr_number}", "PATCH"
            response = self._write(endpoint, method, body, authorize)
        elif operation == "merge":
            message = _text(effect.payload["commit_message"])
            title, _, message_body = message.partition("\n")
            response = self._write(f"repos/{self.repository}/pulls/{effect.pr_number}/merge", "PUT",
                {"sha": _text(effect.after_sha), "merge_method": effect.payload["method"],
                 "commit_title": title, "commit_message": message_body}, authorize)
        elif operation == "completion_comment":
            issue = self._issue(effect)
            if issue["state"] != "open":
                return EffectResult("third", raw={"issue": issue})
            events = self._events(effect)
            baseline = max((event["id"] for event in events), default=0)
            intent = self._comment_intent(effect, baseline)
            response = self._write(self._issue_path(effect) + "/comments", "POST",
                                   {"body": self._comment_body(effect, intent)}, authorize)
        elif operation == "close":
            response = self._write(self._issue_path(effect), "PATCH", {"state": "closed"}, authorize)
        else:
            raise KernelError("unsupported_remote_effect", operation)
        return self._readback(effect, response)

    def _readback(self, effect: Effect, response: subprocess.CompletedProcess[str]) -> EffectResult:
        try:
            result = self.reconcile(effect)
        except GitHubReadError as error:
            raise EffectReadError(error, response) from error
        return replace(result, raw={**result.raw, "write_returncode": response.returncode,
                                    "write_diagnostic": response.stderr[:4096]})

    def reconcile(self, effect: Effect) -> EffectResult:
        """Only inspect this intent: target, unchanged prior, third or unknown."""
        self._binding(effect)
        try:
            if effect.operation == "issue_link":
                return EffectResult("unknown", raw={"display": "acknowledgement_not_received"})
            if effect.operation == "cleanup_remote_ref":
                return inspect_remote_ref(self._cleanup_git(), effect)
            if effect.operation == "retire_pr":
                return self._reconcile_retirement(effect)
            if effect.operation == "reopen_ticket":
                return self._reconcile_ticket_recovery(effect)
            if effect.operation in {"push", "run_branch"}:
                head = self._remote_head(effect.source_ref)
                if head is not None and head == effect.after_sha:
                    commit = self.git.commit(head)
                    return EffectResult("target", commit=commit, raw={"remote_head": head})
                return EffectResult("prior" if head == self._expected_remote(effect) else "third",
                                    raw={"remote_head": head})
            if effect.operation in {"pr_write", "merge"}:
                return self._reconcile_pr(effect)
            if effect.operation == "completion_comment":
                return self._reconcile_comment(effect)
            if effect.operation == "close":
                return self._reconcile_close(effect)
            raise KernelError("unsupported_remote_effect", effect.operation)
        except GitHubReadError:
            # The Host must supervise the original read, retaining any sent
            # intent. An unavailable read is not a physical write outcome.
            raise
        except (KernelError, OSError, ValueError, KeyError, TypeError) as error:
            return EffectResult("unknown", raw={"diagnostic": str(error)[:4096]})

    def pull_request(self, number: int) -> PullRequest:
        """Original PR identity and actual commit trees, for reads and receipts."""
        return self._pr(self._api(f"repos/{self.repository}/pulls/{number}"))

    def prepare_retirement(self, effect: Effect) -> dict[str, Any]:
        """Read the original OPEN PR and its event watermark before Core attachment."""
        self._binding(effect)
        pr = self.pull_request(_number(effect.pr_number))
        if pr.state != "OPEN" or self._retirement_match(effect, pr) is None:
            raise KernelError("retirement_pr_changed")
        events = self._events_at(self._retirement_path(effect))
        baseline = max((event["id"] for event in events), default=0)
        fresh = self.pull_request(pr.number)
        if fresh != pr:
            raise KernelError("retirement_pr_changed")
        return self._retirement_witness(effect, baseline)

    def _retirement_witness(self, effect: Effect, baseline: int) -> dict[str, Any]:
        prior = effect.payload["prior_pr"]
        if (effect.operation != "retire_pr" or prior["state"] != "OPEN"
                or prior["number"] != effect.pr_number or prior["repository"].casefold() != self.repository.casefold()
                or prior["source_ref"] != effect.source_ref or prior["target_ref"] != effect.target_ref
                or prior["head"]["sha"] != effect.before_sha
                or type(baseline) is not int or baseline < 0):
            raise ValueError("invalid original retirement PR")
        return {"repository": effect.repository, "pr_number": _number(effect.pr_number),
                "source_ref": effect.source_ref, "target_ref": effect.target_ref,
                "head_sha": _sha(prior["head"]["sha"]), "base_sha": _sha(prior["base"]["sha"]),
                "author": _text(prior["actor"]), "baseline_event_id": baseline}

    def _retirement_match(self, effect: Effect, pr: PullRequest) -> list[str] | None:
        witness = self._retirement_witness(effect, 0)
        if pr.actor is None or pr.head_repository is None:
            raise ValueError("retirement PR identity is not observable")
        prior = effect.payload["prior_pr"]
        if (pr.repository.casefold() != self.repository.casefold()
                or pr.head_repository.casefold() != self.repository.casefold()
                or pr.number != witness["pr_number"] or pr.source_ref != effect.source_ref
                or pr.target_ref != effect.target_ref or pr.actor != witness["author"]
                or pr.head.sha != witness["head_sha"] or pr.head.tree != prior["head"]["tree"]):
            return None
        if pr.base.sha == witness["base_sha"]:
            if pr.base.tree != prior["base"]["tree"]:
                return None
        elif not self._ancestor(witness["base_sha"], pr.base.sha):
            return None
        return [witness["base_sha"], pr.base.sha]

    def _retirement_path(self, effect: Effect) -> str:
        return f"repos/{self.repository}/issues/{_number(effect.pr_number)}"

    def _retirement_intent(self, effect: Effect) -> dict[str, Any]:
        intent = effect.payload.get("retirement_intent")
        if not isinstance(intent, Mapping):
            raise ValueError("missing persisted original retirement intent")
        witness = self._retirement_witness(effect, intent["baseline_event_id"])
        binding = {**witness, "effect_id": effect.effect_id, "control_action": effect.payload["control_action"],
                   "control_kind": effect.payload["control_kind"], "run_id": effect.execution.run_id,
                   "subject": effect.subject, "actor": effect.actor}
        body = intent.get("comment_body")
        marker = "<!-- agent-run-retirement:" + effect.effect_id + " -->"
        if (any(intent.get(key) != value for key, value in binding.items())
                or intent["control_kind"] not in {"requeue", "abandon"}
                or type(intent.get("generation")) is not int or intent["generation"] < 1
                or not isinstance(body, str) or body.count(marker) != 1):
            raise ValueError("foreign retirement intent")
        return dict(intent)

    def _reconcile_retirement(self, effect: Effect) -> EffectResult:
        intent = self._retirement_intent(effect)
        pr = self.pull_request(_number(effect.pr_number))
        ancestry = self._retirement_match(effect, pr)
        if ancestry is None or pr.state not in {"OPEN", "CLOSED"}:
            return EffectResult("third", pr=pr)
        path = self._retirement_path(effect)
        events = [event for event in self._events_at(path) if event["id"] > intent["baseline_event_id"]]
        marker = "<!-- agent-run-retirement:" + effect.effect_id + " -->"
        comments = [comment for comment in self._pages(path + "/comments?per_page=100")
                    if marker in _string(comment["body"])]
        if not comments:
            return EffectResult("prior" if pr.state == "OPEN" and not events else "third", pr=pr,
                                raw={"base_ancestry": ancestry})
        if len(comments) != 1:
            return EffectResult("unknown", pr=pr, raw={"diagnostic": "ambiguous retirement marker"})
        comment = comments[0]
        author = _login(comment.get("user"))
        if author is None:
            return EffectResult("unknown", pr=pr)
        if author != effect.actor or comment["body"] != intent["comment_body"]:
            return EffectResult("third", pr=pr)
        _time(comment["created_at"])
        receipt = {**intent, "comment_id": _number(comment["id"]), "comment_created_at": comment["created_at"]}
        raw: dict[str, Any] = {"close_intent": receipt, "base_ancestry": ancestry}
        if pr.state == "OPEN":
            return EffectResult("third" if events else "prior", pr=pr, raw=raw)
        if len(events) != 1 or events[0]["event"] != "closed":
            return EffectResult("unknown", pr=pr, raw=raw)
        event = events[0]
        actor = _login(event.get("actor"))
        if actor is None:
            return EffectResult("unknown", pr=pr, raw=raw)
        if actor != effect.actor or _time(event["created_at"]) < _time(comment["created_at"]):
            return EffectResult("third", pr=pr, raw=raw)
        raw["event"] = {"id": event["id"], "event": "closed", "actor": actor, "created_at": event["created_at"]}
        return EffectResult("target", pr=pr, raw=raw)

    def _retire_pr(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        observed = self.reconcile(effect)
        if observed.disposition != "prior":
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        intent = self._retirement_intent(effect)

        def guard() -> None:
            authorize()
            fresh = self.reconcile(effect)
            # Reconciliation already proves the original identity and any
            # append-only base movement. The original marker must stay exact.
            if (fresh.disposition != "prior"
                    or fresh.raw.get("close_intent") != observed.raw.get("close_intent")):
                raise KernelError("retirement_boundary_changed")

        if observed.raw.get("close_intent") is None:
            self._write(self._retirement_path(effect) + "/comments", "POST", {"body": intent["comment_body"]}, guard)
            observed = self.reconcile(effect)
            if observed.disposition != "prior":
                return observed
            if observed.raw.get("close_intent") is None:
                return EffectResult("unknown", pr=observed.pr, raw={"diagnostic": "retirement marker unconfirmed"})
        response = self._write(f"repos/{self.repository}/pulls/{_number(effect.pr_number)}", "PATCH", {"state": "closed"}, guard)
        return self._readback(effect, response)

    @staticmethod
    def _event_actor(event: Mapping[str, Any]) -> str | None:
        actor = event.get("actor")
        return _login(actor) if isinstance(actor, Mapping) else actor if isinstance(actor, str) and actor else None

    def _ticket_recovery_proof(self, effect: Effect) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        completion = effect.payload["completion"]
        receipt = effect.payload["close_receipt"]
        intent = effect.payload["close_intent"]
        original = receipt["raw"]["event"]
        if not all(isinstance(value, Mapping) for value in (completion, receipt, intent, original)):
            raise ValueError("missing original Ticket close proof")
        _text(completion["close_effect"])
        binding = {"run_id": effect.execution.run_id, "subject": effect.subject,
                   "issue": _number(effect.payload["issue"]), "merge_sha": effect.before_sha, "kind": "ticket"}
        if (receipt["disposition"] != "target" or completion["subject"] != effect.subject
                or completion["merge_sha"] != effect.before_sha
                or any(intent.get(key) != value for key, value in binding.items())
                or original["event"] != "closed" or self._event_actor(original) != _text(intent["actor"])
                or type(intent["baseline_event_id"]) is not int or intent["baseline_event_id"] < 0
                or _number(original["id"]) <= intent["baseline_event_id"]
                or _time(original["created_at"]) < _time(intent["comment_created_at"])):
            raise ValueError("foreign original Ticket close proof")
        _number(intent["comment_id"])
        _text(intent["comment_body"])
        _sha(effect.before_sha)
        return intent, original

    def _reconcile_ticket_recovery(self, effect: Effect) -> EffectResult:
        intent, original = self._ticket_recovery_proof(effect)
        issue = self._issue(effect)
        raw: dict[str, Any] = {"issue": effect.payload["issue"], "state": issue["state"].upper(),
                               "prior_close": dict(original)}
        comments = [comment for comment in self._pages(self._issue_path(effect) + "/comments?per_page=100")
                    if comment["id"] == intent["comment_id"]]
        if (len(comments) != 1 or comments[0]["body"] != intent["comment_body"]
                or _login(comments[0].get("user")) != intent["actor"]
                or _time(comments[0]["created_at"]) != _time(intent["comment_created_at"])):
            return EffectResult("unknown", raw={**raw, "diagnostic": "original close comment unproven"})
        events = self._events(effect)
        matches = [event for event in events if event["id"] == original["id"]]
        if (len(matches) != 1 or matches[0]["event"] != "closed"
                or self._event_actor(matches[0]) != self._event_actor(original)
                or _time(matches[0]["created_at"]) != _time(original["created_at"])):
            return EffectResult("unknown", raw={**raw, "diagnostic": "original close event unproven"})
        latest_close = next(event for event in reversed(events) if event["event"] == "closed")
        if latest_close["id"] != original["id"]:
            return EffectResult("third" if self._event_actor(latest_close) else "unknown",
                                raw={**raw, "latest_close": latest_close})
        later = [event for event in events if event["id"] > original["id"]]
        if issue["state"] == "closed":
            return EffectResult("unknown" if later else "prior", raw=raw)
        if (later and later[-1]["event"] == "reopened" and self._event_actor(later[-1]) == effect.actor
                and _time(later[-1]["created_at"]) >= _time(original["created_at"])):
            return EffectResult("target", raw={**raw, "reopened_event": later[-1]})
        return EffectResult("target", raw={**raw, "already_open": True})

    def _reopen_ticket(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        observed = self.reconcile(effect)
        if observed.disposition not in {"prior", "target"}:
            return observed

        def guard() -> None:
            authorize()
            if self.reconcile(effect) != observed:
                raise KernelError("ticket_recovery_boundary_changed")

        if observed.disposition == "target":
            # A verified already-OPEN cleanup outcome still needs first-dispatch
            # authority. Reconciliation remains a pure physical observation.
            guard()
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        response = self._write(self._issue_path(effect), "PATCH", {"state": "open"}, guard)
        return self._readback(effect, response)

    def observe(self, kernel: Kernel, subject: str) -> World:
        """Read external facts using this input's exact refresh receipt.

        Storage is used only for the fixed input/resource identities and exact
        current Git anchors. Kernel.refresh alone accepts requirements; no
        eligibility, currentness, completion or allowance is computed here.
        """
        with kernel.storage.transaction(write=False) as tx:
            parent = self._source_parent(tx)
            job = dict(tx.one("SELECT * FROM subjects WHERE id=?", (subject,)))
            anchors = {job[key] for key in ("base_sha", "source_sha", "candidate_sha", "integrated_sha") if job[key] is not None}
            snapshots = tx.rows("SELECT source_sha,target_sha FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
            if snapshots:
                anchors.update(snapshots[0])
            if job["review_attempt"] is not None:
                review = tx.one("SELECT context_id FROM attempts WHERE id=? AND subject=?", (job["review_attempt"], subject))
                context = json.loads(tx.get(review["context_id"], subject))
                if "review_context" in context:
                    anchors.add(context["review_context"]["full_range"]["candidate_sha"])
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity)
        receipt = self._refresh_requirements(kernel, reader, parent)
        completions = [row for row in receipt["completion_revisions"] if row["subject"] == subject]
        if len(completions) != 1 or completions[0]["generation"] != job["generation"]:
            with kernel.storage.transaction(write=False) as tx:
                current = tx.one("SELECT generation FROM subjects WHERE id=?", (subject,))[0]
            if current != job["generation"]:
                raise ObservationSuperseded("source_subject_changed")
            raise KernelError("source_subject_changed")
        revision = receipt["observed_parent_revision"]
        if job["kind"] == "ticket":
            issues = [row for row in receipt["observed_issues"] if row["issue"] == job["issue"]]
            if len(issues) != 1:
                raise KernelError("source_issue_missing")
            revision = issues[0]["revision"]
        source_sha, target_sha = read_remote_heads(self.root, job["source_ref"], job["target_ref"],
                                                  runner_identity=self.identity, remote=self.remote)
        target = None if target_sha is None else self._commit(target_sha)[0]
        source = None if source_sha is None else self._commit(source_sha)[0]
        raw_pr = self._find_pr(job["source_ref"], job["target_ref"], job["pr_number"])
        pr = None if raw_pr is None else self._pr(raw_pr)
        checks = None
        if pr is not None and pr.state == "OPEN":
            if source is None or target is None or pr.head.sha != source.sha or pr.base.sha != target.sha:
                raise GitHubReadError("github_observation_pending", "PR and remote ref observations have not converged")
            checks = normalize_required_checks(read_required_check_facts(reader, pr.number, expected_head_sha=source.sha))
        if target is not None:
            anchors.add(target.sha)
        after = {sha for sha in (target_sha, source_sha, job["candidate_sha"], job["integrated_sha"]) if sha is not None}
        ancestry = tuple((before, head) for before in sorted(anchors) for head in sorted(after)
                         if self._ancestor(_sha(before), _sha(head)))
        return World(_text(receipt["observed_parent_revision"]), _text(receipt["observed_graph_revision"]),
                     _text(revision), target, source, pr, checks,
                     _text(completions[0]["completion_revision"]), ancestry, self.actor(), dict(self.identity))

    def final_observation(self, kernel: Kernel) -> tuple[Commit, Commit, tuple[tuple[str, str], ...]]:
        """Facts for activate_final; Kernel alone admits the Completion barrier.

        Ordinary subject World reads never enumerate sibling ancestry. This
        explicit consumer proves current-generation Completions into the actual
        remote Run head using this refresh's receipt, not a later global read.
        """
        with kernel.storage.transaction(write=False) as tx:
            parent = self._source_parent(tx)
            target = tx.one("SELECT source_ref,target_ref FROM final_target")
            source_ref, target_ref = target["source_ref"], target["target_ref"]
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity)
        receipt = self._refresh_requirements(kernel, reader, parent)
        run_sha, default_sha = read_remote_heads(self.root, source_ref, target_ref,
                                                 runner_identity=self.identity, remote=self.remote)
        if run_sha is None:
            raise GitHubReadError("final_source_missing", "The original remote Run ref is absent")
        if default_sha is None:
            raise GitHubReadError("missing_target_branch", "The exact remote default ref is absent")
        run_head, default_head = self._commit(run_sha)[0], self._commit(default_sha)[0]
        generations = {row["subject"]: row["generation"] for row in receipt["completion_revisions"]}
        anchors = {row["merge_sha"] for row in receipt["completions"]
                   if generations.get(row["subject"]) == row["generation"]}
        proof = tuple((before, run_sha) for before in sorted(anchors) if self._ancestor(_sha(before), run_sha))
        return run_head, default_head, proof

    def _refresh_requirements(self, kernel: Kernel, reader: GhGitHubReader, parent: int) -> dict[str, Any]:
        with kernel.storage.transaction(write=False) as tx:
            sequence = tx.one("SELECT source_sequence FROM runs")[0]
        try:
            return kernel.refresh(lambda: read_requirements(reader, parent))
        except KernelError as error:
            if error.code != "source_observation_changed":
                raise
            with kernel.storage.transaction(write=False) as tx:
                current = tx.one("SELECT source_sequence FROM runs")[0]
            if current == sequence:
                raise
            raise ObservationSuperseded(error.code) from error

    def _source_parent(self, tx: Transaction) -> int:
        run = tx.one("SELECT * FROM runs")
        inputs = tx.one("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
        frozen = json.loads(tx.get(inputs["material_id"], inputs["subject"]))
        if (run["repository"].casefold() != self.repository.casefold()
                or run["repository_root"] is None or Path(run["repository_root"]).resolve() != self.root.resolve()
                or validate_identity_snapshot(frozen["runner_identity"]) != self.identity):
            raise KernelError("source_input_binding")
        return _number(run["parent_issue"])

    def _ancestor(self, before: str, after: str) -> bool:
        if before == after:
            return True
        # Exact objects are immutable. Fetch missing objects without updating
        # local refs or FETCH_HEAD; a local tracking ref is never an observation.
        for sha in (before, after):
            if self.git.run("cat-file", "-e", sha + "^{commit}", check=False).returncode:
                self._read(["git", "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "--", self.remote, sha])
        return self.git.is_ancestor(before, after)

    def _binding(self, effect: Effect) -> None:
        if effect.repository.casefold() != self.repository.casefold():
            raise KernelError("remote_repository_changed")
        frozen = effect.payload.get("github_identity")
        if frozen is not None and validate_identity_snapshot(frozen) != self.identity:
            raise KernelError("remote_identity_changed")
        for ref in (effect.source_ref, effect.target_ref):
            if not ref.startswith("refs/heads/") or self.git.run("check-ref-format", ref, check=False).returncode:
                raise KernelError("invalid_remote_ref")
        if effect.operation == "cleanup_remote_ref" and effect.payload.get("remote") != self.remote:
            raise KernelError("remote_identity_changed")
        if effect.source_ref == effect.target_ref or not effect.effect_id or not effect.actor:
            raise KernelError("remote_effect_binding")

    def _cleanup_git(self, authorize: Callable[[], None] | None = None) -> LocalGit:
        git = LocalGit(self.root, authorize)
        # Existing retirement verbs keep their exact CAS/inspection behavior;
        # both reads and writes now use the frozen Runner, never offline env.
        git.env = runner_git_environment(self.identity, cwd=self.root)
        return git

    def _link_issue(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        # Kernel's durable display-once intent owns whether this call can occur.
        # Readback cannot manufacture a lost acknowledgement or authorize replay.
        if effect.payload["ref"] != effect.source_ref or effect.payload["source_head"] != effect.before_sha:
            raise KernelError("issue_link_binding")
        try:
            if self.actor() != effect.actor:
                raise KernelError("remote_actor_changed")
            issue = self._api(self._issue_path(effect))
            if issue["number"] != effect.payload["issue"]:
                raise ValueError("foreign Issue identity")
        except (GitHubReadError, OSError, ValueError, KeyError, TypeError) as error:
            # No mutation has been attempted. Record this optional attempt as
            # unavailable under current authority; do not block delivery.
            authorize()
            return EffectResult("target", raw={"acknowledged_status": "unavailable", "display": "unavailable",
                                                "diagnostic": str(error)[:4096]})
        branch = effect.source_ref.removeprefix("refs/heads/")
        query = ("mutation($issueId: ID!, $name: String!, $oid: GitObjectID!) "
                 "{ createLinkedBranch(input: {issueId: $issueId, name: $name, oid: $oid}) "
                 "{ linkedBranch { ref { name target { oid } } } } }")
        response = self._write("graphql", "POST", {"query": query, "variables": {
            "issueId": _text(issue["node_id"]), "name": branch, "oid": _sha(effect.before_sha)}}, authorize)
        try:
            data = json.loads(response.stdout)
            linked = data["data"]["createLinkedBranch"]["linkedBranch"]["ref"]
            if not response.returncode and not data.get("errors") and linked["name"] == branch and linked["target"]["oid"] == effect.before_sha:
                return EffectResult("target", raw={"display": "linked", "acknowledged_status": "linked", "acknowledgement": data})
        except (ValueError, TypeError, KeyError):
            pass
        return EffectResult("unknown", raw={"display": "indeterminate", "write_returncode": response.returncode,
                                            "diagnostic": response.stderr[:4096]})

    def _expected_remote(self, effect: Effect) -> str | None:
        # Missing is a distinct frozen observation, never inferred from a failed read.
        value = effect.payload.get("expected_remote_sha", effect.before_sha)
        if value is not None:
            return _sha(value)
        return None

    def _remote_head(self, ref: str) -> str | None:
        response = self._read(["git", "ls-remote", "--refs", "--", self.remote, ref])
        lines = response.stdout.splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise ValueError("ambiguous remote ref")
        sha, separator, observed = lines[0].partition("\t")
        if not separator or observed != ref:
            raise ValueError("foreign remote ref")
        return _sha(sha)

    def _commit(self, sha: str) -> tuple[Commit, tuple[str, ...]]:
        data = self._api(f"repos/{self.repository}/git/commits/{_sha(sha)}")
        if data["sha"] != sha:
            raise ValueError("foreign commit response")
        return Commit(sha, _sha(data["tree"]["sha"])), tuple(_sha(p["sha"]) for p in data["parents"])

    def _pr(self, data: Mapping[str, Any]) -> PullRequest:
        head, base = data["head"], data["base"]
        repository = _text(base["repo"]["full_name"])
        head_repository = None if head.get("repo") is None else _text(head["repo"]["full_name"])
        merged = None
        base_commit = self._commit(_sha(base["sha"]))[0]
        if data.get("merged"):
            merged, parents = self._commit(_sha(data["merge_commit_sha"]))
            if not parents:
                raise ValueError("merged commit has no original base parent")
            # A merged receipt describes the original Git event, not a later
            # live base-branch pointer. Its actual first parent is that base.
            base_commit = self._commit(parents[0])[0]
        return PullRequest(_number(data["number"]), repository, "refs/heads/" + _text(head["ref"]),
            "refs/heads/" + _text(base["ref"]), self._commit(_sha(head["sha"]))[0], base_commit,
            _text(data["title"]), _string(data["body"]), "MERGED" if merged else _text(data["state"]).upper(),
            _login(data.get("user")), merged, head_repository)

    def _find_pr(self, source_ref: str, target_ref: str, number: int | None) -> dict[str, Any] | None:
        if number is not None:
            return self._api(f"repos/{self.repository}/pulls/{number}")
        query = urlencode({"state": "all", "head": self.repository.split('/')[0] + ':' +
                           source_ref.removeprefix("refs/heads/"), "base": target_ref.removeprefix("refs/heads/"),
                           "per_page": 100})
        prs = self._pages(f"repos/{self.repository}/pulls?{query}")
        if len(prs) > 1:
            raise ValueError("ambiguous original PR")
        return prs[0] if prs else None

    def _reconcile_pr(self, effect: Effect) -> EffectResult:
        data = self._find_pr(effect.source_ref, effect.target_ref, effect.pr_number)
        if data is None:
            source, target = self._remote_head(effect.source_ref), self._remote_head(effect.target_ref)
            return EffectResult("prior" if effect.operation == "pr_write" and effect.pr_number is None
                and source == effect.after_sha and target == effect.base.sha else "third")
        pr = self._pr(data)
        if pr.actor is None or pr.head_repository is None:
            return EffectResult("unknown", pr=pr, raw={"diagnostic": "PR identity is not observable"})
        same = (pr.repository.casefold() == self.repository.casefold() and pr.head_repository is not None
                and pr.head_repository.casefold() == self.repository.casefold()
                and pr.source_ref == effect.source_ref and pr.target_ref == effect.target_ref
                and pr.head.sha == effect.after_sha and pr.actor == effect.actor
                and (effect.pr_number is None or effect.pr_number == pr.number))
        if not same:
            return EffectResult("third", pr=pr)
        if effect.operation == "pr_write":
            if pr.state != "OPEN":
                return EffectResult("third", pr=pr)
            if pr.title == effect.payload["title"] and pr.body == effect.payload["body"]:
                return EffectResult("target", pr=pr)
            source, target = self._remote_head(effect.source_ref), self._remote_head(effect.target_ref)
            if pr.base.sha != effect.base.sha or target != effect.base.sha or source != effect.after_sha:
                return EffectResult("third", pr=pr)
            prior = effect.payload["prior"]
            return EffectResult("prior" if prior is not None and pr.title == prior["title"] and pr.body == prior["body"] else "third", pr=pr)
        source, target = self._remote_head(effect.source_ref), self._remote_head(effect.target_ref)
        if pr.state == "MERGED" and pr.merged is not None:
            commit, parents = self._commit(pr.merged.sha)
            merger = _login(data.get("merged_by"))
            if merger is None:
                return EffectResult("unknown", pr=pr, raw={"diagnostic": "Merge actor is not observable"})
            if (merger != effect.actor or parents != tuple(effect.payload["parents"])
                    or pr.base.sha != effect.before_sha or commit.tree != pr.head.tree or target is None):
                return EffectResult("third", pr=pr)
            compared = self._api(f"repos/{self.repository}/compare/{commit.sha}...{target}")
            contained = (compared["status"] in {"identical", "ahead"}
                         and compared["base_commit"]["sha"] == commit.sha
                         and compared["merge_base_commit"]["sha"] == commit.sha)
            if not contained or self._remote_head(effect.target_ref) != target:
                return EffectResult("unknown", pr=pr)
            return EffectResult("target", commit=commit, pr=pr, raw={"parents": list(parents), "target_head": target, "live_pr_base": data["base"]["sha"]})
        return EffectResult("prior" if pr.state == "OPEN" and pr.base.sha == effect.before_sha
                            and source == effect.after_sha and target == effect.before_sha else "third", pr=pr)

    def _current_pr_edit(self, effect: Effect, observed: EffectResult) -> EffectResult:
        """An original unchanged edit requires live anchors before and after authorization."""
        if observed.disposition != "target" or observed.pr is None:
            return observed
        try:
            source, target = read_remote_heads(self.root, effect.source_ref, effect.target_ref,
                                              runner_identity=self.identity, remote=self.remote)
            if observed.pr.base != effect.base or source != effect.after_sha or target != effect.base.sha:
                return replace(observed, disposition="third")
        except GitHubReadError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            return EffectResult("unknown", pr=observed.pr, raw={"diagnostic": str(error)[:4096]})
        return observed

    def _issue_path(self, effect: Effect) -> str:
        return f"repos/{self.repository}/issues/{_number(effect.payload['issue'])}"

    def _issue(self, effect: Effect) -> dict[str, Any]:
        issue = self._api(self._issue_path(effect))
        if _number(issue["number"]) != effect.payload["issue"] or issue["state"] not in {"open", "closed"}:
            raise ValueError("foreign or unknown Issue")
        return issue

    def _events(self, effect: Effect) -> list[dict[str, Any]]:
        return self._events_at(self._issue_path(effect))

    def _events_at(self, path: str) -> list[dict[str, Any]]:
        events = [event for event in self._pages(path + "/events?per_page=100")
                  if event.get("event") in {"closed", "reopened"}]
        for event in events:
            _number(event["id"])
        return sorted(events, key=lambda e: e["id"])

    def _marker(self, effect: Effect) -> str:
        return "<!-- agent-run-kernel:" + effect.effect_id + " -->"

    def _comment_intent(self, effect: Effect, baseline: int) -> dict[str, Any]:
        return {"run_id": effect.execution.run_id, "subject": effect.subject, "issue": effect.payload["issue"],
                "merge_sha": effect.payload["merge_sha"], "actor": effect.actor, "baseline_event_id": baseline,
                "effect_id": effect.effect_id, "pr_number": _number(effect.pr_number), "kind": effect.payload["kind"]}

    def _comment_body(self, effect: Effect, intent: Mapping[str, Any]) -> str:
        run_id, sha, number = effect.execution.run_id, _sha(effect.payload["merge_sha"]), _number(effect.pr_number)
        kind = effect.payload["kind"]
        if kind == "ticket":
            text = (f"Delivery Run `{run_id}` completed this ticket in PR #{number}; Run Branch commit `{sha}`. "
                    "This change has not yet entered the default branch.")
        elif kind in {"parent_only", "final_run"}:
            label = "Parent-only" if kind == "parent_only" else "Final Run"
            text = (f"Delivery Run `{run_id}` completed this Parent Issue in {label} PR #{number}; "
                    f"merge commit `{sha}` is verified on the default branch.")
        else:
            raise ValueError("unknown completion delivery kind")
        return (self._marker(effect) + "\n" + text + "\n<!-- agent-run-close-intent:"
                + json.dumps(dict(intent), sort_keys=True, ensure_ascii=False) + " -->")

    def _reconcile_comment(self, effect: Effect) -> EffectResult:
        comments = [c for c in self._pages(self._issue_path(effect) + "/comments?per_page=100")
                    if self._marker(effect) in _string(c["body"])]
        if not comments:
            return EffectResult("prior" if self._issue(effect)["state"] == "open" else "third")
        if len(comments) != 1:
            return EffectResult("third")
        author = _login(comments[0].get("user"))
        if author is None:
            return EffectResult("unknown")
        if author != effect.actor:
            return EffectResult("third")
        comment = comments[0]
        found = re.search(r"<!-- agent-run-close-intent:(.*?) -->", comment["body"])
        if found is None:
            return EffectResult("third")
        intent = json.loads(found[1])
        baseline = intent["baseline_event_id"]
        if type(baseline) is not int or baseline < 0 or intent != self._comment_intent(effect, baseline):
            return EffectResult("third")
        if comment["body"] != self._comment_body(effect, intent):
            return EffectResult("third")
        _time(comment["created_at"])
        return EffectResult("target", raw={"close_intent": {**intent, "comment_id": _number(comment["id"]),
                            "comment_created_at": comment["created_at"], "comment_body": comment["body"]}})

    def _reconcile_close(self, effect: Effect) -> EffectResult:
        intent = effect.payload.get("close_intent")
        if not isinstance(intent, Mapping):
            return EffectResult("unknown", raw={"diagnostic": "missing persisted original close intent"})
        binding = {"run_id": effect.execution.run_id, "subject": effect.subject, "issue": effect.payload["issue"],
                   "merge_sha": effect.payload["merge_sha"], "actor": effect.actor,
                   "pr_number": effect.pr_number, "kind": effect.payload["kind"]}
        if any(intent.get(key) != value for key, value in binding.items()):
            return EffectResult("third")
        baseline = intent["baseline_event_id"]
        if type(baseline) is not int or baseline < 0:
            raise ValueError("invalid close watermark")
        comments = self._pages(self._issue_path(effect) + "/comments?per_page=100")
        original = [c for c in comments if c["id"] == intent["comment_id"]]
        if len(original) != 1 or original[0]["body"] != intent["comment_body"]:
            return EffectResult("third")
        author = _login(original[0].get("user"))
        if author is None:
            return EffectResult("unknown")
        if author != effect.actor:
            return EffectResult("third")
        issue = self._issue(effect)
        events = [event for event in self._events(effect) if event["id"] > baseline]
        if not events:
            return EffectResult("prior" if issue["state"] == "open" else "unknown")
        if len(events) != 1 or events[0]["event"] != "closed":
            return EffectResult("third", raw={"events": events})
        event_actor = _login(events[0].get("actor"))
        if event_actor is None:
            return EffectResult("unknown", raw={"events": events})
        if event_actor != effect.actor:
            return EffectResult("third", raw={"events": events})
        event = events[0]
        if _time(event["created_at"]) < _time(intent["comment_created_at"]):
            return EffectResult("third", raw={"event": event})
        return EffectResult("target" if issue["state"] == "closed" else "unknown", raw={"event": event})

    def _read(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        response = run_read_command(command, cwd=self.root, runner_identity=self.identity)
        if response.returncode:
            raise GitHubReadError("remote_read_failed", response.stderr[:4096] or "Remote read failed")
        return response

    def _api(self, endpoint: str) -> dict[str, Any]:
        data = json.loads(self._read(["gh", "api", endpoint]).stdout)
        if not isinstance(data, dict):
            raise ValueError("expected remote object")
        return data

    def _pages(self, endpoint: str) -> list[dict[str, Any]]:
        pages = json.loads(self._read(["gh", "api", endpoint, "--paginate", "--slurp"]).stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("expected paginated remote list")
        rows = [row for page in pages for row in page]
        if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
            raise ValueError("invalid or oversized remote collection")
        return rows

    def _write(self, endpoint: str, method: str, body: Mapping[str, Any],
               authorize: Callable[[], None]) -> subprocess.CompletedProcess[str]:
        data = json.dumps(dict(body), ensure_ascii=False).encode()
        if len(data) > MATERIAL_LIMIT:
            raise ValueError("remote write material exceeds limit")
        with NamedTemporaryFile(prefix="agent-run-write-", suffix=".json") as stream:
            stream.write(data)
            stream.flush()
            authorize()
            return run_write_command(["gh", "api", endpoint, "--method", method, "--input", stream.name],
                                     cwd=self.root, runner_identity=self.identity)


def _login(value: Any) -> str | None:
    login = value.get("login") if isinstance(value, Mapping) else None
    return login if isinstance(login, str) and login.strip() else None


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected remote string")
    return value


def _text(value: Any) -> str:
    result = _string(value)
    if not result.strip():
        raise ValueError("expected nonempty remote string")
    return result


def _number(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected positive remote integer")
    return value


def _sha(value: Any) -> str:
    result = _text(value)
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", result):
        raise ValueError("invalid commit identity")
    return result


def _time(value: Any) -> datetime:
    result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("remote timestamp has no timezone")
    return result
