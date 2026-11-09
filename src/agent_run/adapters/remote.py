"""Bounded Git/GitHub effects and exact readback of the original effect intent.

Kernel owns preparation, dispatch, authorization, retries and receipt persistence.
This adapter has no local journal, delivery phase or writable Run representation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
import re
import random
import time
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, NoReturn, ParamSpec, Protocol, TYPE_CHECKING, TypeVar
from urllib.parse import urlencode, urlsplit

from agent_run.contracts.write_authority import WriteAuthority, admit_write
from agent_run.contracts.status_comment import StatusTarget
from agent_run.contracts.repository import same_repository
from agent_run.adapters.github import GhGitHubReader, _SourceObservation
from agent_run.adapters import github_commands
from agent_run.adapters.auth.runner_transport import runner_options
from agent_run.errors import GitHubReadError
from agent_run.errors import bounded_error
from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.adapters.github_commands import run_read_command, run_write_command
from agent_run.adapters.auth.runner_transport import runner_git_environment

from agent_run.adapters.cleanup import cleanup_remote_ref, inspect_remote_ref

from agent_run.adapters.git_objects import LocalGit
from agent_run.errors import ObservationSuperseded as ObservationSuperseded
from agent_run.model import Commit, Effect, EffectResult, PullRequest, Requirements, ResourceInspection, ResourceWitness
from agent_run.errors import KernelError
from agent_run.adapters.local_git import LocalEffects
from agent_run.contracts.pr_observation import pr_target
from agent_run.adapters.source import (read_remote_heads, read_requirements, read_required_check_facts,
                             normalize_required_checks)

from agent_run.storage.sqlite import MATERIAL_LIMIT

if TYPE_CHECKING:
    from agent_run.core.operation_contract import DeliveryFacts, DeliveryRead, PreparedRequirements


class EffectReadError(GitHubReadError):
    """A typed read failure after an actual command's response was observed."""

    def __init__(self, error: GitHubReadError, response: subprocess.CompletedProcess[str]) -> None:
        super().__init__(error.code, error.message)
        self.write_returncode = response.returncode
        self.write_diagnostic = response.stderr[:4096]


_Arguments = ParamSpec("_Arguments")
_Result = TypeVar("_Result")


def _operation_errors(method: Callable[_Arguments, _Result]) -> Callable[_Arguments, _Result]:
    """Translate physical transport failures at the Core port boundary only."""
    @wraps(method)
    def invoke(*args: _Arguments.args, **kwargs: _Arguments.kwargs) -> _Result:
        from agent_run.core.operation_contract import ReadUnavailable
        try:
            return method(*args, **kwargs)
        except GitHubReadError as error:
            returncode, diagnostic = None, ""
            if isinstance(error, EffectReadError) and error.write_returncode != 0:
                returncode, diagnostic = error.write_returncode, bounded_error(error.write_diagnostic)
            raise ReadUnavailable(error.code, bounded_error(error.message),
                                  write_returncode=returncode, write_diagnostic=diagnostic) from error
        except OSError as error:
            # An interrupted read/transport supplies no observed write status.
            raise ReadUnavailable("operation_io_unavailable", bounded_error(str(error))) from error
    return invoke


class PhysicalAPI(Protocol):
    """Raw external objects, lists and single writes; no Effect decisions."""
    def object(self, endpoint: str) -> dict[str, Any]: ...
    def list(self, endpoint: str) -> list[dict[str, Any]]: ...
    def write(self, endpoint: str, method: str, body: Mapping[str, Any]) -> subprocess.CompletedProcess[str]: ...


class PhysicalGit(Protocol):
    def refs(self) -> dict[str, str]: ...
    def fetch(self, sha: str) -> None: ...
    def push(self, ref: str, before: str | None, after: str) -> subprocess.CompletedProcess[str]: ...
    def delete(self, ref: str, expected: str) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class _SourceBasis:
    query: DeliveryRead | None
    observation: _SourceObservation
    owner: RemoteEffects


_STATUS_COMMENT_QUERY = """query StatusCommentObservation(
  $owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  VIEWER
  repository(owner:$owner,name:$name) { nameWithOwner
    pullRequest(number:$number) { number author { __typename login }
      headRefName baseRefName headRepository { nameWithOwner } baseRepository { nameWithOwner }
      comments(first:100,after:$endCursor) { totalCount pageInfo { endCursor hasNextPage }
        nodes { fullDatabaseId body author { __typename login } repository { nameWithOwner }
          pullRequest { number repository { nameWithOwner } } }
      }
    }
  }
}"""


class RemoteEffects:
    """A concrete execute/reconcile port; authorize is required for every write.

    The caller supplies the Run's frozen Runner reference and selected repository.
    Writes are never retried here. Exact target readback needs no live write grant.
    Failed reads are unknown, never absence, prior, success or permission to retry.
    """
    def __init__(self, root: Path, repository: str, *, runner_identity: Mapping[str, Any],
                 remote: str = "origin", api: PhysicalAPI | None = None,
                 git_writes: PhysicalGit | None = None, cancelled: Callable[[], bool] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected an exact GitHub owner/repository")
        if not remote or remote.startswith("-") or "\0" in remote:
            raise ValueError("Expected a remote name or URL")
        self.root, self.repository, self.remote = root, repository, remote
        self.identity = validate_identity_snapshot(runner_identity)
        self.git = LocalGit(root)
        self.api = api
        self.git_writes = git_writes
        self.cancelled = cancelled

    def actor(self, before_attempt: Callable[[], None] | None = None) -> str:
        """Resolve the selected Runner's actual login, not a fixture identity."""
        if self.identity["source"] == "app":
            from agent_run.adapters.auth.app import resolve_app_login
            from agent_run.config.github_app import GitHubAppProfile
            profile = self.identity["app_profile"]
            return resolve_app_login(GitHubAppProfile(profile["app_id"], profile["installation_id"],
                                                     Path(profile["private_key_path"])))
        return _text((self._api("user") if before_attempt is None else self._api("user", before_attempt))["login"])

    def read_status_comment(self, target: StatusTarget, marker: str, intent: Mapping[str, Any],
                            cancelled: Callable[[], bool]) -> dict[str, Any]:
        """Observe one exact display intent. Foreign/absent is never write authority."""
        def check_cancel() -> None:
            if cancelled():
                raise ValueError("Status delivery cancelled")
        check_cancel()
        if not same_repository(target.repository, self.repository):
            return {"disposition": "third"}
        actor, pr, rows = self._status_comment_facts(target, check_cancel)
        check_cancel()
        if actor != target.actor:
            return {"disposition": "third"}
        head, base = pr.get("head", {}), pr.get("base", {})
        if (not isinstance(head, dict) or not isinstance(base, dict)
                or not isinstance(head.get("repo"), dict) or not isinstance(base.get("repo"), dict)
                or pr.get("number") != target.pr_number or _login(pr.get("user")) != target.actor
                or not isinstance(head["repo"].get("full_name"), str)
                or not same_repository(head["repo"]["full_name"], self.repository)
                or not isinstance(base["repo"].get("full_name"), str)
                or not same_repository(base["repo"]["full_name"], self.repository)
                or "refs/heads/" + str(head.get("ref")) != target.source_ref
                or "refs/heads/" + str(base.get("ref")) != target.target_ref):
            return {"disposition": "third"}
        check_cancel()
        prefix = f"<!-- agent-run-status:{target.key}:"
        found = [row for row in rows if row.get("id") == intent["comment_id"]
                 or isinstance(row.get("body"), str) and prefix in row["body"]]
        if not found:
            return {"disposition": "prior" if intent["comment_id"] is None else "third"}
        if len(found) != 1:
            return {"disposition": "third"}
        row = found[0]
        issue_path = urlsplit(str(row.get("issue_url", ""))).path.split("/")
        if (type(row.get("id")) is not int or row["id"] <= 0
                or intent["comment_id"] is not None and row["id"] != intent["comment_id"]
                or _login(row.get("user")) != target.actor
                or len(issue_path) != 6 or issue_path[:2] != ["", "repos"]
                or issue_path[4:] != ["issues", str(target.pr_number)]
                or not same_repository("/".join(issue_path[2:4]), self.repository)
                or not isinstance(row.get("body"), str) or not row["body"].startswith(marker + "\n")):
            return {"disposition": "third"}
        disposition = ("target" if row["body"] == intent["body"] else "prior"
                       if intent["comment_id"] is not None and row["body"] == intent["before"] else "third")
        return {"disposition": disposition, "comment_id": row["id"]}

    def _status_comment_facts(self, target: StatusTarget, check_cancel: Callable[[], None]
                              ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """One complete round; initial, pre-write and readback calls stay independent."""
        if self.api is not None:
            return (self.actor(check_cancel),
                    self._api(f"repos/{self.repository}/pulls/{target.pr_number}", check_cancel),
                    self._pages(f"repos/{self.repository}/issues/{target.pr_number}/comments?per_page=100", check_cancel))
        app_actor = self.actor(check_cancel) if self.identity["source"] == "app" else None
        owner, name = self.repository.split("/", 1)
        query = _STATUS_COMMENT_QUERY.replace("VIEWER", "" if app_actor is not None else "viewer { login }")
        command = ["gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + query,
                   "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + str(target.pr_number)]
        pages = json.loads(self._read(command, check_cancel).stdout)
        check_cancel()
        return _status_comment_pages(pages, self.repository, app_actor)

    def write_status_comment(self, target: StatusTarget, marker: str, intent: Mapping[str, Any],
                             authorize: WriteAuthority, cancelled: Callable[[], bool]) -> dict[str, Any]:
        """Only POST/PATCH this comment. No retry, deletion, merge or Issue mutation."""
        def boundary() -> None:
            if self.read_status_comment(target, marker, intent, cancelled)["disposition"] != "prior":
                raise ValueError("Status comment changed before write")
        identifier = intent["comment_id"]
        endpoint, method = (f"issues/{target.pr_number}/comments", "POST") if identifier is None else (f"issues/comments/{identifier}", "PATCH")
        response = self._write(f"repos/{self.repository}/{endpoint}", method, {"body": intent["body"]}, authorize, boundary)
        try:
            result = self.read_status_comment(target, marker, intent, cancelled)
        except (GitHubReadError, OSError, ValueError) as error:
            raise EffectReadError(GitHubReadError("status_readback_unavailable", "Status readback unavailable"), response) from error
        return {**result, "write_returncode": response.returncode}

    @_operation_errors
    def execute(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        """One original operation, after exact readback and current authorization."""
        if effect.operation in LocalEffects.OPERATIONS:
            return self._local(effect).execute(effect, authorize)
        self._binding(effect)
        if effect.operation == "issue_link":
            return self._link_issue(effect, authorize)
        if effect.operation == "cleanup_remote_ref":
            return cleanup_remote_ref(self._cleanup_git(authorize), effect,
                delete=None if self.git_writes is None else self.git_writes.delete,
                read_refs=None if self.git_writes is None else self.git_writes.refs)
        if effect.operation == "retire_pr":
            return self._retire_pr(effect, authorize)
        if effect.operation == "reopen_ticket":
            return self._reopen_ticket(effect, authorize)
        if effect.operation == "recovery_comment":
            return self._recovery_comment(effect, authorize)
        observed = self._reconcile(effect)
        prior = effect.payload.get("prior")
        if (effect.operation == "pr_write" and effect.pr_number is not None and isinstance(prior, Mapping)
                and prior.get("title") == effect.payload.get("title")
                and prior.get("body") == effect.payload.get("body")):
            observed = self._current_pr_edit(effect, observed)
            if observed.disposition != "target":
                return observed
            if self.actor() != effect.actor:
                raise KernelError("remote_actor_changed")
            final_admission = authorize.prepare()
            fresh = self._current_pr_edit(effect, self._reconcile(effect))
            if not self._same_pr_boundary(observed, fresh):
                return replace(fresh, disposition="third") if fresh.disposition == "target" else fresh
            final_admission()
            return fresh
        if observed.disposition != "prior":
            return observed
        if (effect.operation == "pr_write" and effect.pr_number is None
                and (observed.pr is not None or observed.raw.get("target_head") != effect.base.sha)):
            # Readback may settle an absent old create after a target append;
            # it does not authorize another POST against that changed target.
            return replace(observed, disposition="third")
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        operation = effect.operation

        def check_prior() -> None:
            fresh = self._reconcile(effect)
            if fresh.disposition != "prior" or not self._same_pr_boundary(observed, fresh):
                raise KernelError("pr_boundary_changed" if operation == "pr_write" else "operation_boundary_changed")

        if operation in {"push", "run_branch"}:
            after = _text(effect.after_sha)
            if not self.git.is_ancestor(effect.before_sha, after):
                raise KernelError("non_append_write")
            expected = self._expected_remote(effect)
            command = ["git", "push", self.remote, f"{after}:{effect.source_ref}",
                       f"--force-with-lease={effect.source_ref}:{expected or ''}"]
            if self.git_writes is None:
                response = run_write_command(command, authorize=lambda: admit_write(authorize), cwd=self.root, runner_identity=self.identity)
            else:
                admit_write(authorize)
                response = self.git_writes.push(effect.source_ref, expected, after)
        elif operation == "pr_write":
            body = {"title": _text(effect.payload["title"]), "body": _text(effect.payload["body"])}
            if effect.pr_number is None:
                endpoint, method = f"repos/{self.repository}/pulls", "POST"
                body.update(head=effect.source_ref.removeprefix("refs/heads/"),
                            base=effect.target_ref.removeprefix("refs/heads/"))
            else:
                endpoint, method = f"repos/{self.repository}/pulls/{effect.pr_number}", "PATCH"
            response = self._write(endpoint, method, body, authorize, check_prior)
        elif operation == "merge":
            message = _text(effect.payload["commit_message"])
            title, _, message_body = message.partition("\n\n")
            response = self._write(f"repos/{self.repository}/pulls/{effect.pr_number}/merge", "PUT",
                {"sha": _text(effect.after_sha), "merge_method": effect.payload["method"],
                 "commit_title": title, "commit_message": message_body}, authorize, check_prior)
        elif operation == "completion_comment":
            issue = self._issue(effect)
            if issue["state"] != "open":
                return EffectResult("third", raw={"issue": issue})
            events = self._events(effect)
            baseline = max((event["id"] for event in events), default=0)
            intent = self._comment_intent(effect, baseline)

            def check_comment() -> None:
                check_prior()
                if max((event["id"] for event in self._events(effect)), default=0) != baseline:
                    raise KernelError("operation_boundary_changed")

            response = self._write(self._issue_path(effect) + "/comments", "POST",
                                   {"body": self._comment_body(effect, intent)}, authorize, check_comment)
        elif operation == "close":
            response = self._write(self._issue_path(effect), "PATCH", {"state": "closed"}, authorize, check_prior)
        else:
            raise KernelError("unsupported_remote_effect", operation)
        return self._readback(effect, response)

    def _readback(self, effect: Effect, response: subprocess.CompletedProcess[str]) -> EffectResult:
        try:
            result = self._reconcile(effect)
        except GitHubReadError as error:
            raise EffectReadError(error, response) from error
        return replace(result, raw={**result.raw, "write_returncode": response.returncode,
                                    "write_diagnostic": response.stderr[:4096]})

    @_operation_errors
    def reconcile(self, effect: Effect) -> EffectResult:
        return self._reconcile(effect)

    def _reconcile(self, effect: Effect) -> EffectResult:
        """Only inspect this intent: target, unchanged prior, third or unknown."""
        if effect.operation in LocalEffects.OPERATIONS:
            return self._local(effect).reconcile(effect)
        self._binding(effect)
        try:
            if effect.operation == "issue_link":
                return EffectResult("unknown", raw={"display": "acknowledgement_not_received"})
            if effect.operation == "cleanup_remote_ref":
                return inspect_remote_ref(self._cleanup_git(), effect,
                    read_refs=None if self.git_writes is None else self.git_writes.refs)
            if effect.operation == "retire_pr":
                return self._reconcile_retirement(effect)
            if effect.operation == "reopen_ticket":
                return self._reconcile_ticket_recovery(effect)
            if effect.operation == "recovery_comment":
                return self._reconcile_recovery_comment(effect)
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

    def find_pull_request(self, source_ref: str, target_ref: str) -> PullRequest | None:
        data = self._find_pr(source_ref, target_ref, None)
        return None if data is None else self._pr(data)

    @_operation_errors
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
            raw["state_events"] = [{"id": event["id"], "event": event["event"],
                "actor": _login(event.get("actor")), "created_at": event.get("created_at")} for event in events]
            return EffectResult("unknown", pr=pr, raw=raw)
        event = events[0]
        actor = _login(event.get("actor"))
        if actor is None:
            return EffectResult("unknown", pr=pr, raw=raw)
        if actor != effect.actor or _time(event["created_at"]) < _time(comment["created_at"]):
            return EffectResult("third", pr=pr, raw=raw)
        raw["event"] = {"id": event["id"], "event": "closed", "actor": actor, "created_at": event["created_at"]}
        return EffectResult("target", pr=pr, raw=raw)

    def _retire_pr(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        observed = self.reconcile(effect)
        if observed.disposition != "prior":
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        intent = self._retirement_intent(effect)

        def guard() -> None:
            fresh = self.reconcile(effect)
            # Reconciliation already proves the original identity and any
            # append-only base movement. The original marker must stay exact.
            if (fresh.disposition != "prior"
                    or fresh.raw.get("close_intent") != observed.raw.get("close_intent")):
                raise KernelError("retirement_boundary_changed")

        if observed.raw.get("close_intent") is None:
            self._write(self._retirement_path(effect) + "/comments", "POST", {"body": intent["comment_body"]}, authorize, guard)
            observed = self.reconcile(effect)
            if observed.disposition != "prior":
                return observed
            if observed.raw.get("close_intent") is None:
                return EffectResult("unknown", pr=observed.pr, raw={"diagnostic": "retirement marker unconfirmed"})
        response = self._write(f"repos/{self.repository}/pulls/{_number(effect.pr_number)}", "PATCH", {"state": "closed"}, authorize, guard)
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
                or intent["actor"] != effect.actor or intent["pr_number"] != effect.pr_number
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

    @staticmethod
    def _recovery_pending(message: str) -> NoReturn:
        raise GitHubReadError("github_recovery_observation_pending", message)

    def _recovery_time(self, value: Any) -> datetime:
        try:
            return _time(value)
        except (TypeError, ValueError):
            self._recovery_pending("Recovery observation timestamp is unavailable")

    def _recovery_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        actor = self._event_actor(event)
        if actor is None:
            self._recovery_pending("Recovery transition actor is unavailable")
        self._recovery_time(event.get("created_at"))
        return {"id": _number(event["id"]), "event": event["event"],
                "actor": actor, "created_at": _text(event["created_at"])}

    def _reconcile_ticket_recovery(self, effect: Effect) -> EffectResult:
        intent, original = self._ticket_recovery_proof(effect)
        issue = self._issue(effect)
        raw: dict[str, Any] = {"issue": effect.payload["issue"], "state": issue["state"].upper(),
                               "prior_close": dict(original)}
        comments = [comment for comment in self._pages(self._issue_path(effect) + "/comments?per_page=100")
                    if comment["id"] == intent["comment_id"]]
        if (len(comments) != 1 or comments[0]["body"] != intent["comment_body"]
                or _login(comments[0].get("user")) != intent["actor"]
                or self._recovery_time(comments[0].get("created_at")) != _time(intent["comment_created_at"])):
            self._recovery_pending("Original close comment is unproven")
        events = self._events(effect)
        matches = [event for event in events if event["id"] == original["id"]]
        if (len(matches) != 1 or matches[0]["event"] != "closed"
                or self._event_actor(matches[0]) != self._event_actor(original)
                or self._recovery_time(matches[0].get("created_at")) != _time(original["created_at"])):
            self._recovery_pending("Original close event is unproven")
        latest_close = next(event for event in reversed(events) if event["event"] == "closed")
        if (issue["state"] == "open" and events[-1]["event"] == "reopened"
                and events[-1]["id"] > original["id"]):
            latest_reopen = self._recovery_event(events[-1])
            if latest_reopen["actor"] != effect.actor:
                return EffectResult("third", raw={**raw, "latest_reopen": latest_reopen, "reason": "not_owned"})
        if latest_close["id"] != original["id"]:
            return EffectResult("third", raw={**raw, "latest_close": self._recovery_event(latest_close),
                                               "reason": "not_owned"})
        later = [event for event in events if event["id"] > original["id"]]
        if issue["state"] == "closed":
            if later:
                self._recovery_pending("Issue state and recovery timeline have not converged")
            transition = original
            disposition = "prior"
        else:
            if not later or later[-1]["event"] != "reopened":
                self._recovery_pending("Open Issue has no proven recovery transition")
            transition = self._recovery_event(later[-1])
            if transition["actor"] != effect.actor:
                return EffectResult("third", raw={**raw, "latest_reopen": transition, "reason": "not_owned"})
            if _time(transition["created_at"]) < _time(original["created_at"]):
                self._recovery_pending("Recovery transition precedes the original close")
            raw["reopened_event"] = transition
            disposition = "target"
        try:
            current = _time(issue["updated_at"]) == _time(transition["created_at"])
        except (KeyError, TypeError, ValueError):
            current = False
        if not current:
            self._recovery_pending("Issue updated_at does not prove the original recovery transition")
        return EffectResult(disposition, raw={**raw, "updated_at": issue["updated_at"]})

    def _recovery_comment_proof(self, effect: Effect) -> dict[str, Any]:
        _intent, original = self._ticket_recovery_proof(effect)
        receipt = effect.payload["reopen_receipt"]
        raw = receipt["raw"]
        event = raw["reopened_event"]
        if (receipt["disposition"] != "target" or raw["issue"] != effect.payload["issue"]
                or raw["state"] != "OPEN" or raw["prior_close"] != original
                or event["event"] != "reopened" or event["actor"] != effect.actor
                or _number(event["id"]) <= _number(original["id"])
                or _time(event["created_at"]) < _time(original["created_at"])):
            raise ValueError("Foreign Ticket reopen proof")
        return {"issue": effect.payload["issue"], "reopen_effect": _text(effect.payload["reopen_effect"]),
                "prior_close": dict(original), "reopened_event": dict(event)}

    @staticmethod
    def _same_recovery_event(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return (all(left[key] == right[key] for key in ("id", "event", "actor"))
                and _time(left["created_at"]) == _time(right["created_at"]))

    def _recovery_body(self, effect: Effect) -> str:
        return self._marker(effect) + "\n" + _text(effect.payload["comment_body"])

    def _reconcile_recovery_comment(self, effect: Effect) -> EffectResult:
        raw = self._recovery_comment_proof(effect)
        comments = [comment for comment in self._pages(self._issue_path(effect) + "/comments?per_page=100")
                    if self._marker(effect) in comment["body"]]
        if comments:
            # An exact sent write remains a physical fact after later Issue edits.
            if (len(comments) != 1 or comments[0]["body"] != self._recovery_body(effect)
                    or _login(comments[0].get("user")) != effect.actor):
                return EffectResult("third", raw={**raw, "diagnostic": "Recovery marker conflicts"})
            comment = comments[0]
            self._recovery_time(comment.get("created_at"))
            return EffectResult("target", raw={**raw, "comment": {"id": _number(comment["id"]),
                "body": comment["body"], "actor": effect.actor, "created_at": comment["created_at"]}})
        reopened = self._reconcile_ticket_recovery(effect)
        if reopened.disposition == "third":
            return reopened
        if reopened.disposition != "target" or not self._same_recovery_event(reopened.raw["reopened_event"], raw["reopened_event"]):
            self._recovery_pending("Frozen own reopen has not converged")
        return EffectResult("prior", raw=raw)

    def _recovery_comment(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        observed = self.reconcile(effect)
        if observed.disposition != "prior":
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")

        def guard() -> None:
            if self.reconcile(effect) != observed:
                raise KernelError("ticket_recovery_boundary_changed")

        response = self._write(self._issue_path(effect) + "/comments", "POST",
                               {"body": self._recovery_body(effect)}, authorize, guard)
        return self._readback(effect, response)

    def _reopen_ticket(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        observed = self.reconcile(effect)
        if observed.disposition not in {"prior", "target"}:
            return observed

        def guard() -> None:
            fresh = self.reconcile(effect)
            if (fresh.disposition != observed.disposition
                    or (fresh.disposition == "target" and not self._same_recovery_event(
                        fresh.raw["reopened_event"], observed.raw["reopened_event"]))):
                raise KernelError("ticket_recovery_boundary_changed")

        if observed.disposition == "target":
            # First dispatch still needs authority; sent reconciliation is read-only.
            admit_write(authorize, guard)
            return observed
        if self.actor() != effect.actor:
            raise KernelError("remote_actor_changed")
        response = self._write(self._issue_path(effect), "PATCH", {"state": "open"}, authorize, guard)
        return self._readback(effect, response)

    @_operation_errors
    def source(self, parent_number: int) -> Requirements:
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity,
                                cancelled=self.cancelled)
        return read_requirements(reader, parent_number)

    @_operation_errors
    def source_observation(self, parent_number: int, query: DeliveryRead | None) -> PreparedRequirements:
        from agent_run.core.operation_contract import PreparedRequirements
        if type(parent_number) is not int or parent_number < 1:
            raise ValueError('Parent Issue number must be a positive integer')
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity,
                                cancelled=self.cancelled)
        projection = None
        if query is not None and self.api is None:
            projection = self._delivery_projection(None, None, query.pr_number if query.read_pr else None,
                (query.source_ref, query.target_ref) if query.read_pr and query.pr_number is None else None)
            declarations, fields, _, arguments = projection
            for key, ref in (('sourceRef', query.source_ref), ('targetRef', query.target_ref)):
                declarations.append('$' + key + ':String!')
                fields.append(key + ':ref(qualifiedName:$' + key + ') { name prefix repository { nameWithOwner } '
                    'target { __typename oid ... on Commit { tree { oid } } } }')
                arguments += ['-f', key + '=' + ref]
        observation = _SourceObservation(reader, delivery_projection=projection)
        return PreparedRequirements(observation.read(parent_number), _SourceBasis(query, observation, self))

    def _projected_refs(self, envelope: Mapping[str, Any], query: DeliveryRead
                        ) -> tuple[tuple[str | None, str | None], dict[str, Any]] | None:
        """Validate ref addressing; short-name fallback requests the exact old reader."""
        if envelope.get('errors'):
            return None
        data = envelope['data']
        repository = data['repository']
        if repository is None:
            raise GitHubReadError('github_read_failed', 'Delivery repository is unavailable')
        if not same_repository(_text(repository['nameWithOwner']), self.repository):
            raise ValueError('foreign Delivery repository')
        refs: list[str | None] = []
        objects: dict[str, Any] = {}
        for key, expected in (('sourceRef', query.source_ref), ('targetRef', query.target_ref)):
            ref = repository[key]
            if ref is None:
                refs.append(None)
                continue
            if not same_repository(_text(ref['repository']['nameWithOwner']), self.repository):
                raise ValueError('foreign Delivery ref repository')
            if _text(ref['prefix']) + _text(ref['name']) != expected:
                return None
            value = ref['target']
            if value is None:
                raise GitHubReadError('github_read_failed', 'Delivery ref target is unavailable')
            if value['__typename'] != 'Commit':
                raise ValueError('Delivery ref does not name a commit')
            sha, tree = _sha(value['oid']), _sha(value['tree']['oid'])
            if sha in objects and objects[sha]['tree']['oid'] != tree:
                raise ValueError('Same commit has contradictory trees')
            objects[sha] = {'__typename': 'Commit', 'oid': sha, 'tree': {'oid': tree}}
            refs.append(sha)
        projected = dict(repository)
        projected.update({'object' + str(index): objects[sha] for index, sha in enumerate(sorted(objects))})
        return (refs[0], refs[1]), {'data': {**data, 'repository': projected}}

    @_operation_errors
    def complete_observation(self, query: DeliveryRead, prepared: PreparedRequirements) -> DeliveryFacts:
        stage = prepared.physical
        if not isinstance(stage, _SourceBasis) or stage.owner is not self:
            raise ValueError('Foreign Source observation')
        from agent_run.core.operation_contract import ReadUnavailable, requested_ancestry
        if type(query.read_checks) is not bool or not query.read_checks and query.check_head is not None:
            raise ReadUnavailable("checks_request_invalid", "An exact checks head requires a checks observation")
        requested_ancestry(query, None)
        observation = stage.observation
        if self.api is not None:
            return self.delivery(query)
        observation._remaining()
        projected = None
        if query == stage.query and observation.delivery_packet is not None:
            try:
                projected = self._projected_refs(observation.delivery_packet, query)
            except (ValueError, KeyError, TypeError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis facts are malformed') from error
        # Mismatched demand discards both facts and errors. No failed Source
        # attempt can retain a packet. This starts a new Delivery attempt.
        refs = read_remote_heads(self.root, query.source_ref, query.target_ref,
            runner_identity=self.identity, remote=self.remote, command=observation.command)
        if projected is not None and refs != projected[0]:
            raise GitHubReadError('github_observation_pending', 'GraphQL and Git refs changed during observation')
        basis, raw_pr, actor = self._delivery_basis(*refs, query.pr_number if query.read_pr else None,
            (query.source_ref, query.target_ref) if query.read_pr and query.pr_number is None else None,
            deadline=observation.deadline, attempts=int(observation.combined_attempted),
            initial=None if projected is None else projected[1])
        return self._finish_delivery(query, refs, basis, raw_pr, actor)

    def _delivery_projection(self, source_sha: str | None, target_sha: str | None,
                             pr_number: int | None, discovery: tuple[str, str] | None = None,
                             *, compact: bool = False,
                             ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Build the Delivery fields independently of the request that carries them."""
        owner, name = self.repository.split('/', 1)
        identities = sorted({_sha(sha) for sha in (source_sha, target_sha) if sha is not None})
        if pr_number is not None:
            _number(pr_number)
        declarations = ['$owner:String!', '$name:String!']
        fields = ['id nameWithOwner']
        arguments = ['-f', 'owner=' + owner, '-f', 'name=' + name]
        for index, sha in enumerate(identities):
            key = 'object' + str(index)
            declarations.append('$' + key + ':GitObjectID!')
            fields.append(key + ':object(oid:$' + key + ') { __typename oid ... on Commit { tree { oid } } }')
            arguments += ['-f', key + '=' + sha]
        if pr_number is not None and not compact:
            declarations.append('$pr:Int!')
            fields.append('pullRequest(number:$pr) { number title body state author { login } '
                'repository { nameWithOwner } baseRepository { nameWithOwner } headRepository { nameWithOwner } '
                'headRefName headRefOid baseRefName baseRefOid '
                'mergeCommit { oid parents(first:100) { nodes { oid tree { oid } } } } }')
            arguments += ['-F', 'pr=' + str(pr_number)]
        if discovery is not None and not compact:
            declarations += ['$head:String!', '$base:String!']
            fields.append('discovery:pullRequests(first:100,headRefName:$head,baseRefName:$base,'
                'states:[OPEN,CLOSED,MERGED]) { nodes { number headRefName baseRefName '
                'headRepositoryOwner { login } } pageInfo { hasNextPage } }')
            arguments += ['-f', 'head=' + discovery[0].removeprefix('refs/heads/'),
                          '-f', 'base=' + discovery[1].removeprefix('refs/heads/')]
        root_fields = ['viewer { login }'] if self.identity['source'] == 'host' else []
        return declarations, fields, root_fields, arguments

    def _parse_delivery_basis(self, envelope: Mapping[str, Any],
                              source_sha: str | None, target_sha: str | None,
                              pr_number: int | None, discovery: tuple[str, str] | None = None,
                              *, compact: bool = False, packet: Callable[..., Any],
                              ) -> tuple[dict[str, Commit], dict[str, Any] | None, str | None]:
        """Validate a complete basis and finish its exact PR discovery if needed."""
        owner = self.repository.split('/', 1)[0]
        identities = sorted({_sha(sha) for sha in (source_sha, target_sha) if sha is not None})
        host = self.identity['source'] == 'host'
        errors = envelope.get('errors', [])
        if not isinstance(errors, list):
            raise ValueError('invalid GraphQL errors envelope')
        if errors or 'data' in envelope and envelope['data'] is None:
            raise GitHubReadError('github_read_failed', 'Delivery basis is incomplete')
        data = envelope['data']
        repository = data['repository']
        if repository is None:
            raise GitHubReadError('github_read_failed', 'Delivery repository is unavailable')
        if not _text(repository['id']) or not same_repository(_text(repository['nameWithOwner']), self.repository):
            raise ValueError('foreign Delivery repository')
        commits = {}
        for index, sha in enumerate(identities):
            value = repository['object' + str(index)]
            if value is None:
                raise GitHubReadError('github_read_failed', 'Exact Delivery commit is unavailable')
            if value['__typename'] != 'Commit' or _sha(value['oid']) != sha:
                raise ValueError('foreign Delivery commit')
            commits[sha] = Commit(sha, _sha(value['tree']['oid']))
        actor = None
        if host:
            if data['viewer'] is None:
                raise GitHubReadError('github_read_failed', 'Delivery viewer is unavailable')
            actor = _text(data['viewer']['login'])
        number = pr_number
        if discovery is not None:
            rest = compact
            matches = []
            if not compact:
                connection = repository['discovery']
                nodes, more = connection['nodes'], connection['pageInfo']['hasNextPage']
                if not isinstance(nodes, list) or len(nodes) > 100 or type(more) is not bool:
                    raise ValueError('Malformed PR discovery connection')
                rest = more
                for node in nodes:
                    candidate = _number(node['number'])
                    if (_text(node['headRefName']) != discovery[0].removeprefix('refs/heads/')
                            or _text(node['baseRefName']) != discovery[1].removeprefix('refs/heads/')):
                        raise ValueError('Foreign PR discovery refs')
                    head_owner = node['headRepositoryOwner']
                    if head_owner is None:
                        rest = True  # Legal deleted-head identity, not proof of a foreign owner.
                    elif _text(head_owner['login']).casefold() == owner.casefold():
                        matches.append(candidate)
            if rest:
                rows = packet(['gh', 'api', self._pr_discovery_endpoint(*discovery),
                               '--paginate', '--slurp'], pages=True)
                matches = [_number(row['number']) for row in rows]
            if len(matches) > 1:
                raise ValueError('ambiguous original PR')
            number = matches[0] if matches else None
        raw_pr = None
        if number is not None:
            if compact or discovery is not None:
                raw_pr = packet(['gh', 'api', f'repos/{self.repository}/pulls/{number}'])
                if (_number(raw_pr['number']) != number or type(raw_pr.get('merged')) is not bool
                        or raw_pr['state'] not in {'open', 'closed'}):
                    raise ValueError('foreign or invalid Delivery PR')
                _text(raw_pr['title'])
                _string(raw_pr['body'])
                for side in ('head', 'base'):
                    _sha(raw_pr[side]['sha'])
                    _text(raw_pr[side]['ref'])
                    repo = raw_pr[side].get('repo')
                    if side == 'base' and repo is None:
                        raise GitHubReadError('github_read_failed', 'Delivery PR base repository is unavailable')
                    if repo is not None:
                        _text(repo['full_name'])
                if raw_pr['merged'] and raw_pr.get('merge_commit_sha') is None:
                    raise GitHubReadError('github_read_failed', 'Delivery merge object is unavailable')
            else:
                value = repository['pullRequest']
                if value is None:
                    raise GitHubReadError('github_read_failed', 'Known Delivery PR is unavailable')
                if value['baseRepository'] is None:
                    raise GitHubReadError('github_read_failed', 'Delivery PR base repository is unavailable')
                if not same_repository(_text(value['repository']['nameWithOwner']), self.repository):
                    raise ValueError('foreign Delivery PR repository')
                state = _text(value['state'])
                if _number(value['number']) != number or state not in {'OPEN', 'CLOSED', 'MERGED'}:
                    raise ValueError('foreign or invalid Delivery PR')
                if state == 'MERGED' and value['mergeCommit'] is None:
                    raise GitHubReadError('github_read_failed', 'Delivery merge object is unavailable')
                raw_pr = {'number': number, 'title': _text(value['title']), 'body': _string(value['body']),
                    'state': 'closed' if state == 'MERGED' else state.lower(), 'user': value['author'],
                    'head': {'sha': _sha(value['headRefOid']), 'ref': _text(value['headRefName']),
                             'repo': None if value['headRepository'] is None else
                                     {'full_name': _text(value['headRepository']['nameWithOwner'])}},
                    'base': {'sha': _sha(value['baseRefOid']), 'ref': _text(value['baseRefName']),
                             'repo': {'full_name': _text(value['baseRepository']['nameWithOwner'])}},
                    'merged': state == 'MERGED',
                    'merge_commit_sha': None if state != 'MERGED' else _sha(value['mergeCommit']['oid'])}
                if state == 'MERGED':
                    # This bounded subset supplies trees by exact OID, not parent
                    # order. The original merge REST receipt still selects parent 0.
                    parents = value['mergeCommit']['parents']['nodes']
                    if not isinstance(parents, list) or len(parents) > 100:
                        raise ValueError('Malformed merge parent objects')
                    for parent in parents:
                        if parent is None:
                            raise GitHubReadError('github_read_failed', 'Merge parent object is unavailable')
                        if not isinstance(parent, dict):
                            raise ValueError('Malformed merge parent object')
                        if parent['tree'] is None:
                            raise GitHubReadError('github_read_failed', 'Merge parent tree is unavailable')
                        parent_commit = Commit(_sha(parent['oid']), _sha(parent['tree']['oid']))
                        if parent_commit.sha in commits and commits[parent_commit.sha] != parent_commit:
                            raise ValueError('Same commit has contradictory trees')
                        commits[parent_commit.sha] = parent_commit
        return commits, raw_pr, actor

    def _delivery_basis(self, source_sha: str | None, target_sha: str | None,
                        pr_number: int | None, discovery: tuple[str, str] | None = None,
                        *, deadline: float | None = None, attempts: int = 0,
                        initial: Mapping[str, Any] | None = None,
                        ) -> tuple[dict[str, Commit], dict[str, Any] | None, str | None]:
        """One fresh bounded projection, never retained beyond this Delivery.

        PR discovery supplies a number only. Complete ref-name/owner matches
        share the packet; pagination or an unavailable owner uses the original
        REST collection. An identified PR is then read afresh. Capacity alone
        permits compact projection. All work shares three whole attempts and a
        90-second maximum; incomplete packets supply no reusable facts.
        """
        projection = self._delivery_projection(source_sha, target_sha, pr_number, discovery)
        if deadline is None:
            deadline = time.monotonic() + github_commands.MAX_READ_ATTEMPTS * github_commands.READ_TIMEOUT_SECONDS

        def remaining() -> float:
            if self.cancelled is not None and self.cancelled():
                raise InterruptedError('Delivery read cancelled')
            value = deadline - time.monotonic()
            if value <= 0:
                raise GitHubReadError('github_read_failed', 'Delivery basis observation deadline expired')
            return value

        def before_start() -> None:
            remaining()

        def cancelled() -> bool:
            remaining()
            return False

        def packet(arguments: list[str], *, pages: bool = False) -> Any:
            try:
                response = github_commands._run_bounded_command(arguments, cwd=self.root,
                    timeout=min(github_commands.READ_TIMEOUT_SECONDS, remaining()), complete_stdout=True,
                    before_start=before_start, cancelled=cancelled, **runner_options(self.identity))
            except subprocess.TimeoutExpired as error:
                raise GitHubReadError('github_read_failed', 'Delivery basis command deadline expired') from error
            remaining()
            if response.returncode:
                raise github_commands.read_response_error(response)
            try:
                data = json.loads(response.stdout)
            except (ValueError, RecursionError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis response is not complete JSON') from error
            if pages:
                if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                    raise ValueError('Expected paginated PR collection')
                rows = [row for page in data for row in page]
                if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
                    raise ValueError('Invalid or oversized PR collection')
                return rows
            if not isinstance(data, dict):
                raise GitHubReadError('github_invalid_response', 'Expected a complete Delivery object')
            return data

        compact = False
        if attempts >= github_commands.MAX_READ_ATTEMPTS and initial is None:
            raise GitHubReadError('github_read_failed', 'Delivery basis observation attempts exhausted')
        for attempt in range(attempts - int(initial is not None), github_commands.MAX_READ_ATTEMPTS):
            remaining()
            try:
                declarations, fields, root_fields, arguments = projection
                query = ('query DeliveryBasis(' + ','.join(declarations) + ') { '
                    + ' '.join(root_fields) + ' '
                    + 'repository(owner:$owner,name:$name) { ' + ' '.join(fields) + ' } }')
                envelope = initial if initial is not None else packet(['gh', 'api', 'graphql', *arguments, '-f', 'query=' + query])
                initial = None
                commits, raw_pr, actor = self._parse_delivery_basis(envelope,
                    source_sha, target_sha, pr_number, discovery, compact=compact, packet=packet)
                remaining()
                return commits, raw_pr, actor
            except github_commands.ReadOutputOverflow as error:
                if compact or pr_number is None and discovery is None:
                    raise
                if attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise GitHubReadError('github_read_failed', 'Delivery basis needs an unread compact projection') from error
                compact = True
                projection = self._delivery_projection(source_sha, target_sha, pr_number, discovery, compact=True)
            except GitHubReadError as error:
                if error.code != 'github_read_failed' or attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 ** attempt + random.uniform(0, .25), remaining()))
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis facts are malformed') from error
        raise AssertionError('Delivery basis exhausted without a result')

    @_operation_errors
    def delivery(self, request: DeliveryRead) -> DeliveryFacts:
        """Read exact Core-supplied refs and immutable ancestry anchors."""
        from agent_run.core.operation_contract import ReadUnavailable, requested_ancestry
        if type(request.read_checks) is not bool or not request.read_checks and request.check_head is not None:
            raise ReadUnavailable("checks_request_invalid", "An exact checks head requires a checks observation")
        requested_ancestry(request, None)  # Validate literal demand before I/O.
        source_sha, target_sha = read_remote_heads(self.root, request.source_ref, request.target_ref,
                                                  runner_identity=self.identity, remote=self.remote)
        basis: dict[str, Commit] = {}
        actor = None
        if self.api is None:
            number = request.pr_number if request.read_pr else None
            if request.read_pr and number is None:
                basis, raw_pr, actor = self._delivery_basis(source_sha, target_sha, None,
                    (request.source_ref, request.target_ref))
            else:
                basis, raw_pr, actor = self._delivery_basis(source_sha, target_sha, number)
        else:
            # PhysicalAPI's existing injected REST interface has no GraphQL port.
            raw_pr = self._find_pr(request.source_ref, request.target_ref, request.pr_number) if request.read_pr else None
        return self._finish_delivery(request, (source_sha, target_sha), basis, raw_pr, actor)

    def _finish_delivery(self, request: DeliveryRead, refs: tuple[str | None, str | None],
                         basis: Mapping[str, Commit], raw_pr: Mapping[str, Any] | None,
                         actor: str | None) -> DeliveryFacts:
        """Complete exact objects, ancestry, checks and the closing ref boundary."""
        from agent_run.core.operation_contract import DeliveryFacts, requested_ancestry
        source_sha, target_sha = refs
        commits: dict[str, Commit] = {}
        reused: set[str] = set()
        def read_commit(sha: str) -> Commit:
            sha = _sha(sha)
            if sha in commits:
                reused.add(sha)
                return commits[sha]
            return basis[sha] if sha in (source_sha, target_sha) and sha in basis else self._commit(sha)[0]
        source = None if source_sha is None else read_commit(source_sha)
        if source is not None:
            commits[source.sha] = source
        target = None if target_sha is None else read_commit(target_sha)
        if target is not None:
            commits[target.sha] = target
        pr = None if raw_pr is None else self._pr(raw_pr, read_commit=read_commit, merged_parents=basis)
        if pr is not None and pr.state == 'MERGED' and pr.base.sha in basis:
            # A consumed parent tree must match the actual exact Git object,
            # just like the other API facts reused in this observation.
            commits[pr.base.sha] = pr.base
            reused.add(pr.base.sha)
        checked = requested_ancestry(request, source, target, pr)
        heads = set(request.anchors) | {head for head in (source_sha, target_sha) if head is not None}
        if checked is not None:
            heads.update(head for pair in checked for head in pair)
        if pr is not None:
            heads.add(pr.base.sha)
        # Only exact objects are reused within this observation. Live refs are
        # still bracketed below; no World or write authority is cached.
        prepared = sorted({_sha(head) for head in heads})
        for head in prepared:
            self._prepare_ancestor_object(head)
        if reused:
            # Do not let reuse hide inconsistent API tree evidence. Resolve the
            # exact immutable objects already prepared above, without decoding
            # commit messages or deriving parents from shallow/grafted history.
            identities = sorted(reused)
            objects = self.git.run("log", "--no-patch", "--no-walk=unsorted", "--format=%H %T",
                "--encoding=UTF-8", "--no-use-mailmap", "--no-show-signature", "--no-notes", "--no-color",
                *identities, "--").stdout.splitlines()
            if objects != [sha + " " + commits[sha].tree for sha in identities]:
                raise GitHubReadError("github_observation_pending", "Commit object and API tree do not agree")
        pairs = ((before, after) for before in prepared for after in prepared) if request.ancestry_pairs is None else iter(checked or ())
        ancestry = tuple((before, after) for before, after in pairs
                         if before == after or self.git.is_ancestor(before, after))
        checks = None
        if pr is not None and pr.state == "OPEN":
            if source is None or pr_target(pr, repository=self.repository, pr_number=request.pr_number,
                    source_ref=request.source_ref, target_ref=request.target_ref,
                    source=source, target=target, ancestry=ancestry) is None:
                raise GitHubReadError("github_observation_pending", "PR and remote ref observations have not converged")
            if request.read_checks:
                reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity)
                checks = normalize_required_checks(read_required_check_facts(
                    reader, pr.number, expected_head_sha=request.check_head or source.sha))
        actor = self.actor() if actor is None else actor
        if read_remote_heads(self.root, request.source_ref, request.target_ref,
                runner_identity=self.identity, remote=self.remote) != (source_sha, target_sha):
            raise GitHubReadError("github_observation_pending", "Remote refs changed during delivery observation")
        return DeliveryFacts(target, source, pr, checks, ancestry, actor, dict(self.identity), checked,
                             request.read_checks)

    @_operation_errors
    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        return LocalEffects(self.git).inspect_resources(resources)

    @_operation_errors
    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]:
        return self._local(effect).prepare_publication(effect)

    def _local(self, effect: Effect) -> LocalEffects:
        # Physical capability dispatch only. Core already selected this exact
        # operation and owns every authorization and durable intent.
        root = (None if effect.operation == "inspect_resources" else
                Path(effect.payload["resource"]["owned_root"] if effect.operation == "abandon_worktree"
                     else effect.payload["owned_root"]))
        return LocalEffects(self.git, owned_root=root)

    def _prepare_ancestor_object(self, sha: str) -> None:
        # Exact objects are immutable. Fetch missing objects without updating
        # local refs or FETCH_HEAD; a local tracking ref is never an observation.
        if self.git.run("cat-file", "-e", sha + "^{commit}", check=False).returncode:
            if self.git_writes is None:
                self._read(["git", "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "--", self.remote, sha])
            else:
                self.git_writes.fetch(sha)
            if self.git.run("cat-file", "-e", sha + "^{commit}", check=False).returncode:
                raise GitHubReadError("git_ancestry_unknown", "Fetched commit object is unavailable")

    def _ancestor(self, before: str, after: str) -> bool:
        if before == after:
            return True
        for sha in (before, after):
            self._prepare_ancestor_object(sha)
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

    def _cleanup_git(self, authorize: WriteAuthority | None = None) -> LocalGit:
        git = LocalGit(self.root, None if authorize is None else lambda: admit_write(authorize))
        # Existing retirement verbs keep their exact CAS/inspection behavior;
        # both reads and writes now use the frozen Runner, never offline env.
        git.env = runner_git_environment(self.identity, cwd=self.root)
        return git

    def _link_issue(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
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
            admit_write(authorize)
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
        if self.git_writes is not None:
            return self.git_writes.refs().get(ref)
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

    def _remote_heads(self, source_ref: str, target_ref: str) -> tuple[str | None, str | None]:
        if self.git_writes is not None:
            refs = self.git_writes.refs()
            return refs.get(source_ref), refs.get(target_ref)
        return read_remote_heads(self.root, source_ref, target_ref,
                                 runner_identity=self.identity, remote=self.remote)

    def _pr_target(self, effect: Effect, pr: PullRequest,
                        source: str | None, target: str | None) -> Commit | None:
        live = None if target is None else self._commit(target)[0]
        ancestry = ((pr.base.sha, target),) if target is not None and self._ancestor(pr.base.sha, target) else ()
        return pr_target(pr, repository=self.repository, pr_number=effect.pr_number,
            source_ref=effect.source_ref, target_ref=effect.target_ref,
            source=pr.head if source == pr.head.sha else None, target=live, ancestry=ancestry)

    @staticmethod
    def _same_pr_boundary(before: EffectResult, after: EffectResult) -> bool:
        """Both reads already matched the same intent and exact live target.

        Only their reported OPEN base snapshot may converge or lag. All other
        fields, including content, actor and outcome, still compare exactly.
        Neither result nor the raw base evidence is rewritten.
        """
        if before == after:
            return True
        if before.pr is None or after.pr is None or before.pr.state != "OPEN" or after.pr.state != "OPEN":
            return False
        return replace(after, pr=replace(after.pr, base=before.pr.base)) == before

    def _commit(self, sha: str) -> tuple[Commit, tuple[str, ...], str | None]:
        data = self._api(f"repos/{self.repository}/git/commits/{_sha(sha)}")
        if data["sha"] != sha:
            raise ValueError("foreign commit response")
        message = data.get("message")
        return (Commit(sha, _sha(data["tree"]["sha"])), tuple(_sha(p["sha"]) for p in data["parents"]),
                message if isinstance(message, str) else None)

    def _pr(self, data: Mapping[str, Any], *,
            read_commit: Callable[[str], Commit] | None = None,
            merged_parents: Mapping[str, Commit] | None = None) -> PullRequest:
        commit = (lambda sha: self._commit(sha)[0]) if read_commit is None else read_commit
        head, base = data["head"], data["base"]
        repository = _text(base["repo"]["full_name"])
        head_repository = None if head.get("repo") is None else _text(head["repo"]["full_name"])
        merged = None
        base_sha = _sha(base["sha"])
        if data.get("merged"):
            # Parent order is separate receipt evidence; keep its original fresh read.
            merged, parents, _ = self._commit(_sha(data["merge_commit_sha"]))
            if not parents:
                raise ValueError("merged commit has no original base parent")
            # A merged receipt describes the original Git event, not a later
            # live base-branch pointer. Its actual first parent is that base.
            base_commit = merged_parents.get(parents[0]) if merged_parents is not None else None
            if base_commit is None:
                base_commit = commit(parents[0])
        else:
            base_commit = commit(base_sha)
        return PullRequest(_number(data["number"]), repository, "refs/heads/" + _text(head["ref"]),
            "refs/heads/" + _text(base["ref"]), commit(_sha(head["sha"])), base_commit,
            _text(data["title"]), _string(data["body"]), "MERGED" if merged else _text(data["state"]).upper(),
            _login(data.get("user")), merged, head_repository)

    def _find_pr(self, source_ref: str, target_ref: str, number: int | None) -> dict[str, Any] | None:
        if number is not None:
            return self._api(f"repos/{self.repository}/pulls/{number}")
        prs = self._pages(self._pr_discovery_endpoint(source_ref, target_ref))
        if len(prs) > 1:
            raise ValueError("ambiguous original PR")
        return prs[0] if prs else None

    def _pr_discovery_endpoint(self, source_ref: str, target_ref: str) -> str:
        query = urlencode({"state": "all", "head": self.repository.split('/')[0] + ':' +
                           source_ref.removeprefix("refs/heads/"), "base": target_ref.removeprefix("refs/heads/"),
                           "per_page": 100})
        return f"repos/{self.repository}/pulls?{query}"

    def _reconcile_pr(self, effect: Effect) -> EffectResult:
        data = self._find_pr(effect.source_ref, effect.target_ref, effect.pr_number)
        if data is None:
            source, target = self._remote_head(effect.source_ref), self._remote_head(effect.target_ref)
            return EffectResult("prior" if effect.operation == "pr_write" and effect.pr_number is None
                and source == effect.after_sha and target is not None and self._ancestor(effect.base.sha, target)
                else "third", raw={"pr_absent": True, "source_head": source, "target_head": target})
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
            source, target = self._remote_heads(effect.source_ref, effect.target_ref)
            if (self._pr_target(effect, pr, source, target) != effect.base
                    or source != effect.after_sha):
                return EffectResult("third", pr=pr)
            prior = effect.payload["prior"]
            return EffectResult("prior" if prior is not None and pr.title == prior["title"] and pr.body == prior["body"] else "third", pr=pr)
        source, target = self._remote_heads(effect.source_ref, effect.target_ref)
        if pr.state == "MERGED" and pr.merged is not None:
            commit, parents, message = self._commit(pr.merged.sha)
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
            raw = {"parents": list(parents), "target_head": target, "live_pr_base": data["base"]["sha"],
                   "commit_message": message}
            if effect.payload["method"] == "squash" and message is None:
                return EffectResult("unknown", commit=commit, pr=pr,
                    raw={**raw, "diagnostic": "Full squash commit message is not observable"})
            return EffectResult("target", commit=commit, pr=pr, raw=raw)
        if (effect.payload["method"] == "squash"
                and pr.title + "\n\n" + pr.body != effect.payload["commit_message"]):
            return EffectResult("third", pr=pr)
        return EffectResult("prior" if self._pr_target(effect, pr, source, target) == effect.base
                            and source == effect.after_sha and target == effect.before_sha else "third", pr=pr)

    def _current_pr_edit(self, effect: Effect, observed: EffectResult) -> EffectResult:
        """An original unchanged edit requires live anchors before and after authorization."""
        if observed.disposition != "target" or observed.pr is None:
            return observed
        try:
            source, target = self._remote_heads(effect.source_ref, effect.target_ref)
            if (self._pr_target(effect, observed.pr, source, target) != effect.base
                    or source != effect.after_sha):
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

    @staticmethod
    def _marker(effect: Effect) -> str:
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

    def _read(self, command: list[str], before_attempt: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
        # API envelopes are structured; fetch output remains diagnostic.
        complete_stdout = command[:2] == ["gh", "api"]
        response = (run_read_command(command, cwd=self.root, runner_identity=self.identity, complete_stdout=complete_stdout) if before_attempt is None and self.cancelled is None else
                    run_read_command(command, cwd=self.root, runner_identity=self.identity, complete_stdout=complete_stdout, before_attempt=before_attempt,
                                     **({"cancelled": self.cancelled} if self.cancelled is not None else {})))
        if response.returncode:
            raise GitHubReadError("remote_read_failed", response.stderr[:4096] or "Remote read failed")
        return response

    def _api(self, endpoint: str, before_attempt: Callable[[], None] | None = None) -> dict[str, Any]:
        if before_attempt is not None:
            before_attempt()
        data = self.api.object(endpoint) if self.api is not None else json.loads((
            self._read(["gh", "api", endpoint]) if before_attempt is None else self._read(["gh", "api", endpoint], before_attempt)).stdout)
        if not isinstance(data, dict):
            raise ValueError("expected remote object")
        return data

    def _pages(self, endpoint: str, before_attempt: Callable[[], None] | None = None) -> list[dict[str, Any]]:
        if before_attempt is not None:
            before_attempt()
        if self.api is not None:
            rows = self.api.list(endpoint)
            if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
                raise ValueError("invalid or oversized remote collection")
            return rows
        command = ["gh", "api", endpoint, "--paginate", "--slurp"]
        pages = json.loads((self._read(command) if before_attempt is None else self._read(command, before_attempt)).stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("expected paginated remote list")
        rows = [row for page in pages for row in page]
        if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
            raise ValueError("invalid or oversized remote collection")
        return rows

    def _write(self, endpoint: str, method: str, body: Mapping[str, Any],
               authorize: WriteAuthority, boundary: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
        data = json.dumps(dict(body), ensure_ascii=False).encode()
        if len(data) > MATERIAL_LIMIT:
            raise ValueError("remote write material exceeds limit")
        if self.api is not None:
            admit_write(authorize, boundary)
            return self.api.write(endpoint, method, body)
        with NamedTemporaryFile(prefix="agent-run-write-", suffix=".json") as stream:
            stream.write(data)
            stream.flush()
            return run_write_command(["gh", "api", endpoint, "--method", method, "--input", stream.name],
                                     authorize=lambda: admit_write(authorize, boundary), cwd=self.root, runner_identity=self.identity,
                                     **({"cancelled": self.cancelled} if self.cancelled is not None else {}))


def _status_author(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Missing Status author")
    kind, login = _text(value.get("__typename")), _text(value.get("login"))
    # GraphQL Bot.login is the App slug; REST and the App resolver use [bot].
    # Other Actor kinds (e.g. Mannequin) cannot adopt a User or Bot identity.
    if kind == "Mannequin":
        return None
    if kind not in {"User", "Bot"}:
        raise ValueError("Unknown Status author type")
    if login.endswith("[bot]"):
        raise ValueError("Invalid GraphQL author login")
    return {"login": login + "[bot]" if kind == "Bot" else login}


def _status_comment_pages(pages: Any, repository: str, app_actor: str | None
                          ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Validate one complete GH pagination result before the existing selector."""
    if not isinstance(pages, list) or not pages:
        raise ValueError("Status observation requires complete pages")
    rows: list[dict[str, Any]] = []
    identifiers: set[int] = set()
    cursors: set[str] = set()
    binding = None
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("errors", []) != []:
            raise ValueError("Incomplete Status observation")
        data = page.get("data")
        repo = data.get("repository") if isinstance(data, dict) else None
        pr = repo.get("pullRequest") if isinstance(repo, dict) else None
        if not isinstance(data, dict) or not isinstance(repo, dict) or not isinstance(pr, dict):
            raise ValueError("Missing Status publication")
        canonical = _text(repo.get("nameWithOwner"))
        if not same_repository(canonical, repository):
            raise ValueError("Status repository changed")
        actor = app_actor if app_actor is not None else _login(data.get("viewer"))
        actor = _text(actor)
        head, base = pr.get("headRepository"), pr.get("baseRepository")
        if (not isinstance(head, dict) or not isinstance(base, dict) or "author" not in pr
                or pr["author"] is not None and _login(pr["author"]) is None):
            raise ValueError("Missing Status publication repository")
        observed = {"number": _number(pr.get("number")), "user": _status_author(pr.get("author")),
                    "head": {"ref": _text(pr.get("headRefName")), "repo": {"full_name": _text(head.get("nameWithOwner"))}},
                    "base": {"ref": _text(pr.get("baseRefName")), "repo": {"full_name": _text(base.get("nameWithOwner"))}}}
        comments = pr.get("comments")
        nodes = comments.get("nodes") if isinstance(comments, dict) else None
        info = comments.get("pageInfo") if isinstance(comments, dict) else None
        total = comments.get("totalCount") if isinstance(comments, dict) else None
        if (not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(info, dict)
                or type(total) is not int or total < 0 or type(info.get("hasNextPage")) is not bool
                or info["hasNextPage"] != (index < len(pages) - 1)):
            raise ValueError("Incomplete Status comment pagination")
        current = (actor, observed, total)
        if binding is not None and current != binding:
            raise ValueError("Status publication changed during pagination")
        binding = current
        cursor = info.get("endCursor")
        if nodes:
            cursor = _text(cursor)
            if cursor in cursors:
                raise ValueError("Status comment cursor did not advance")
            cursors.add(cursor)
        elif total != 0 or len(pages) != 1 or cursor is not None:
            raise ValueError("Incomplete Status comment page")
        for node in nodes:
            if (not isinstance(node, dict) or "author" not in node
                    or node["author"] is not None and _login(node["author"]) is None):
                raise ValueError("Missing Status comment")
            identifier = node.get("fullDatabaseId")
            if isinstance(identifier, str) and re.fullmatch(r"[1-9][0-9]*", identifier):
                identifier = int(identifier)
            identifier = _number(identifier)
            if identifier in identifiers:
                raise ValueError("Duplicate Status comment identity")
            identifiers.add(identifier)
            comment_repo, pull = node.get("repository"), node.get("pullRequest")
            pull_repo = pull.get("repository") if isinstance(pull, dict) else None
            if not isinstance(comment_repo, dict) or not isinstance(pull, dict) or not isinstance(pull_repo, dict):
                raise ValueError("Missing Status comment owner")
            owner = _text(comment_repo.get("nameWithOwner"))
            if not same_repository(owner, _text(pull_repo.get("nameWithOwner"))):
                raise ValueError("Inconsistent Status comment owner")
            rows.append({"id": identifier, "body": _string(node.get("body")), "user": _status_author(node.get("author")),
                         "issue_url": f"https://api.github.com/repos/{owner}/issues/{_number(pull.get('number'))}"})
    assert binding is not None
    if len(rows) != binding[2] or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
        raise ValueError("Incomplete or oversized Status comment collection")
    return binding[0], binding[1], rows


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
