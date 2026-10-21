"""Immutable values crossing the replacement kernel's three interfaces.

The wire artifacts deliberately keep the existing role contracts. No value in
this module is a second mutable Run, an executable plan, or a store protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Any, Mapping


class KernelError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class DeliveryKind(StrEnum):
    TICKET = "ticket"
    PARENT_ONLY = "parent_only"
    FINAL_RUN = "final_run"


class Role(StrEnum):
    DEVELOPMENT = "development"
    REVIEW = "review"
    PUBLICATION = "publication"


class Purpose(StrEnum):
    REQUIREMENTS = "requirements"
    INTEGRATION = "integration"
    CI = "ci"


@dataclass(frozen=True)
class Commit:
    sha: str
    tree: str

    def __post_init__(self) -> None:
        if not self.sha or not self.tree:
            raise ValueError("Commit requires both identity and tree")


@dataclass(frozen=True)
class Profile:
    model: str = "configured-model"
    effort: str = "configured-effort"


@dataclass(frozen=True)
class Policy:
    ticket_rounds: int = 3
    parent_rounds: int = 10
    run_repairs: int = 3
    integration_developments: int = 3
    ci_developments: int = 3
    ticket_ci_review: bool = False
    concurrency: int = 1
    new_thread_per_development: bool = False
    development_profile: Profile = field(default_factory=Profile)
    review_profile: Profile = field(default_factory=Profile)
    publication_profile: Profile = field(default_factory=Profile)

    def __post_init__(self) -> None:
        for value in (self.ticket_rounds, self.parent_rounds, self.run_repairs,
                      self.integration_developments, self.ci_developments, self.concurrency):
            if type(value) is not int or value < 1:
                raise ValueError("policy limits must be positive integers")


@dataclass(frozen=True)
class Delivery:
    key: str
    kind: DeliveryKind
    issue: int
    revision: str
    base: Commit
    source_ref: str
    target_ref: str
    dependencies: tuple[str, ...] = ()
    completion_revision: str = "no-tickets"


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    repository: str
    parent_issue: int
    parent_revision: str
    graph_revision: str
    deliveries: tuple[Delivery, ...]
    policy: Policy = field(default_factory=Policy)


@dataclass(frozen=True)
class PullRequest:
    number: int
    repository: str
    source_ref: str
    target_ref: str
    head: Commit
    base: Commit
    title: str
    body: str
    state: str = "OPEN"
    actor: str = "offline-publisher"
    merged: Commit | None = None


@dataclass(frozen=True)
class Checks:
    pr_number: int
    head_sha: str
    result: str
    details: tuple[tuple[str, str], ...] = ()
    logs: str = ""

    def __post_init__(self) -> None:
        if self.result not in {"none", "pass", "pending", "unknown", "fail"}:
            raise ValueError("unknown checks result")
        if self.result == "none" and self.details:
            raise ValueError("none means no configured required checks")
        if self.result in {"pass", "fail"} and not self.details:
            raise ValueError("checks conclusion requires complete check details")
        if self.result == "pass" and any(state != "pass" for _, state in self.details):
            raise ValueError("pass requires every configured check to pass")
        if self.result == "fail" and not any(state == "fail" for _, state in self.details):
            raise ValueError("fail requires an actual failing check")


@dataclass(frozen=True)
class World:
    parent_revision: str
    graph_revision: str
    revision: str
    target: Commit
    source: Commit | None = None
    pr: PullRequest | None = None
    checks: Checks | None = None
    completion_revision: str = "no-tickets"


@dataclass(frozen=True)
class Execution:
    run_id: str
    owner: str
    epoch: int


@dataclass(frozen=True)
class AgentCall:
    attempt_id: str
    invocation_id: str
    subject: str
    role: Role
    purpose: Purpose
    thread_id: str
    lease_id: str
    execution: Execution
    context: Mapping[str, Any]
    profile: Profile


@dataclass(frozen=True)
class Effect:
    effect_id: str
    subject: str
    operation: str
    repository: str
    source_ref: str
    target_ref: str
    before_sha: str
    after_sha: str | None
    candidate: Commit | None
    base: Commit
    pr_number: int | None
    actor: str
    payload: Mapping[str, Any]
    execution: Execution


@dataclass(frozen=True)
class EffectResult:
    disposition: str
    commit: Commit | None = None
    expected_tree: str | None = None
    pr: PullRequest | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    kind: str
    subject: str
    reason: str = ""
    call: AgentCall | None = None
    effect: Effect | None = None


def wire_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact(role: Role, raw: bytes) -> dict[str, Any]:
    """Validate the complete existing role contract; keep original bytes elsewhere."""
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise KernelError("invalid_artifact", str(error)) from error
    if not isinstance(data, dict):
        raise KernelError("invalid_artifact", "expected object")

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if role == Role.REVIEW:
        if set(data) != {"checks"} or not isinstance(data["checks"], dict):
            raise KernelError("invalid_artifact", "review requires checks")
        if set(data["checks"]) != {"e2e", "standards", "spec"}:
            raise KernelError("invalid_artifact", "all three review lanes are required")
        for lane in data["checks"].values():
            if (not isinstance(lane, dict) or set(lane) != {"status", "evidence", "findings"}
                    or lane["status"] not in {"pass", "fail", "blocked"}
                    or not text(lane["evidence"]) or not isinstance(lane["findings"], list)
                    or not all(text(value) for value in lane["findings"])
                    or bool(lane["findings"]) != (lane["status"] == "fail")):
                raise KernelError("invalid_artifact", "inconsistent review lane")
        return data
    fields = ({"result_kind", "summary", "human_blockers"} if role == Role.DEVELOPMENT
              else {"result_kind", "commit_message", "pr_title", "pr_body_markdown", "human_blockers"})
    if set(data) != fields:
        raise KernelError("invalid_artifact", "role wire fields do not match")
    content = ["summary"] if role == Role.DEVELOPMENT else ["commit_message", "pr_title", "pr_body_markdown"]
    if data["result_kind"] == "human_blocker":
        blockers = data["human_blockers"]
        if (not isinstance(blockers, list) or not 1 <= len(blockers) <= 32
                or not all(text(value) and len(value) <= 8192 for value in blockers)
                or any(data[key] is not None for key in content)):
            raise KernelError("invalid_artifact", "human blocker requires original blockers and null content")
    elif data["result_kind"] != role.value or data["human_blockers"] is not None or not all(text(data[key]) for key in content):
        raise KernelError("invalid_artifact", "role content is missing")
    return data


def review_outcome(data: Mapping[str, Any]) -> str:
    states = {lane["status"] for lane in data["checks"].values()}
    return "fail" if "fail" in states else "blocked" if "blocked" in states else "pass"
