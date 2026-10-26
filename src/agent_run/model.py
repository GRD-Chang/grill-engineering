"""Immutable values crossing the replacement kernel's three interfaces.

The wire artifacts deliberately keep the existing role contracts. No value in
this module is a second mutable Run, an executable plan, or a store protocol.
"""
from __future__ import annotations

from agent_run.errors import KernelError


from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from agent_run.errors import DeliveryPolicyError
from agent_run.contracts.policy import POLICY_VALUE_FIELDS, canonical_number, nonnegative_integer, positive_seconds, positive_integer

from agent_run.errors import ArtifactContractError
from agent_run.contracts.artifact import parse_artifact






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
    """The validated frozen values used by both configuration and delivery.

    Profiles remain creation hints; the Run stores its actual Thread bindings
    separately, so they are omitted from the public policy snapshot.
    """

    ticket_rounds: int = 3
    parent_rounds: int = 10
    run_repairs: int = 3
    integration_developments: int = 3
    ticket_ci_developments: int = 3
    parent_ci_developments: int = 3
    resync_limit: int = 3
    ticket_ci_review: bool = False
    concurrency: int = 3
    new_thread_per_development: bool = False
    development_profile: Profile = field(default_factory=Profile)
    review_profile: Profile = field(default_factory=Profile)
    publication_profile: Profile = field(default_factory=Profile)
    development_deadline_seconds: float = 18000.0
    review_deadline_seconds: float = 7200.0
    publication_deadline_seconds: float = 3600.0

    def __post_init__(self) -> None:
        for key, field_name in POLICY_VALUE_FIELDS.items():
            value = getattr(self, field_name)
            if key == "ticket_ci_review":
                if type(value) is not bool:
                    raise DeliveryPolicyError("ticket_ci_review must be boolean")
            else:
                validate = nonnegative_integer if key == "integration_resync_limit" else positive_integer
                object.__setattr__(self, field_name, validate(value, key))
        if type(self.new_thread_per_development) is not bool:
            raise DeliveryPolicyError("new_thread_per_development must be boolean")
        for role in ("development", "review", "publication"):
            field_name = role + "_deadline_seconds"
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, positive_seconds(value, role + " deadline"))

    @property
    def invocation_deadlines(self) -> dict[str, float]:
        return {"development": self.development_deadline_seconds,
                "review": self.review_deadline_seconds, "publication": self.publication_deadline_seconds}

    def snapshot(self) -> dict[str, Any]:
        """Encode complete public policy values; never resolve future defaults."""
        return {
            **{key: getattr(self, field_name) for key, field_name in POLICY_VALUE_FIELDS.items()},
            "development_thread_policy": "new-per-attempt" if self.new_thread_per_development else "reuse",
            "invocation_deadlines": {role: canonical_number(value) for role, value in self.invocation_deadlines.items()},
        }


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
    checkout: str = ""
    owned_root: str = ""
    common_git_dir: str = ""


@dataclass(frozen=True)
class CreationInputs:
    """Resolved non-secret inputs; personal files are never read on continuation."""
    profiles: Mapping[str, Any]
    prompt_resources: Mapping[str, str]
    worker_identity: Mapping[str, Any]
    runner_identity: Mapping[str, Any]
    notifications: Mapping[str, Any]
    provenance: Mapping[str, Any]
    repository_remote: str = "origin"

    def __post_init__(self) -> None:
        if (not self.repository_remote or self.repository_remote.startswith("-")
                or any(not (character.isalnum() or character in "-_.") for character in self.repository_remote)):
            raise ValueError("repository_remote must be a named Git remote")


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    repository: str
    parent_issue: int
    parent_revision: str
    graph_revision: str
    deliveries: tuple[Delivery, ...]
    policy: Policy = field(default_factory=Policy)
    language: str | None = None
    repository_root: str | None = None
    inputs: CreationInputs | None = None

    def __post_init__(self) -> None:
        if self.language not in {None, "zh", "en"}:
            raise ValueError("language must be explicitly zh or en")
        if self.repository_root is not None and not Path(self.repository_root).is_absolute():
            raise ValueError("repository_root must be absolute")


@dataclass(frozen=True)
class Blocker:
    number: int
    state: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: frozenset[str] = frozenset()
    blocked_by: tuple[Blocker, ...] = ()


@dataclass(frozen=True)
class ParentIssue:
    number: int
    title: str
    body: str
    sub_issue_numbers: tuple[int, ...] = ()
    sub_issue_order_reliable: bool = True


@dataclass(frozen=True)
class Requirements:
    """Exact source facts, without accepted scope or scheduling decisions."""
    parent: ParentIssue
    issues: tuple[Issue, ...]

    def __post_init__(self) -> None:
        numbers = [issue.number for issue in self.issues]
        if len(set(numbers)) != len(numbers) or len(set(self.parent.sub_issue_numbers)) != len(self.parent.sub_issue_numbers):
            raise ValueError("duplicate source Issue")
        if any(number < 1 for number in (*numbers, self.parent.number, *self.parent.sub_issue_numbers)):
            raise ValueError("Issue numbers must be positive")

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> Requirements:
        parent = data["parent"]
        issues = data["issues"]
        values = issues.values() if isinstance(issues, dict) else issues
        return cls(ParentIssue(parent["number"], parent["title"], parent["body"],
                               tuple(parent["sub_issues"]), parent["sub_issue_order_reliable"]),
                   tuple(Issue(value["number"], value["title"], value["body"], value["state"],
                               frozenset(value["labels"]), tuple(Blocker(item["number"], item["state"])
                                                              for item in value["blocked_by"])) for value in values))


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
    actor: str | None = None
    merged: Commit | None = None
    head_repository: str | None = None


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
    target: Commit | None
    source: Commit | None = None
    pr: PullRequest | None = None
    checks: Checks | None = None
    completion_revision: str = "no-tickets"
    ancestry: tuple[tuple[str, str], ...] = ()
    publisher_actor: str | None = None
    publisher_identity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Execution:
    run_id: str
    owner: str
    epoch: int


@dataclass(frozen=True)
class HostLaunch:
    run_id: str
    epoch: int
    owner: str
    nonce: str
    unit_name: str | None
    command: tuple[str, ...]
    environment_carrier: Path
    backend: Literal["systemd", "foreground"] = "systemd"

    @property
    def description(self) -> str:
        return "agent-run-kernel:" + json.dumps(
            [self.run_id, self.epoch, self.owner, self.nonce], separators=(",", ":"), ensure_ascii=True,
        )


@dataclass(frozen=True)
class HostObservation:
    disposition: Literal["running", "exited", "unknown"]
    unit_name: str | None
    invocation_id: str | None = None
    cgroup: str | None = None
    boot_id: str | None = None
    main_pid: int | None = None
    start_token: str | None = None
    exit_status: int | None = None
    ended_at: float | None = None
    evidence: str = ""
    usage_ready: bool = False
    usage_released: bool = False
    usage_pending: bool = False
    backend: Literal["systemd", "foreground"] = "systemd"
    domain: dict[str, Any] | None = None
    namespace_exit_status: int | None = None
    exit_evidence: str = ""


@dataclass(frozen=True)
class AgentPreparation:
    """An Attempt's bounded preparation seat; no Invocation or execution clock."""
    reservation_id: str
    attempt_id: str
    subject: str
    role: Role
    purpose: Purpose
    thread_id: str
    execution: Execution
    context: Mapping[str, Any]
    profile: Profile
    deadline_seconds: float
    preparation_deadline: float
    mode: str = "fresh"
    reported_thread: str | None = None
    output_ordinal: int = 0
    profile_revision: int = 1
    profile_reference: str | None = None


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
    deadline_seconds: float
    deadline_at: float
    mode: str = "fresh"
    reported_thread: str | None = None
    output_ordinal: int = 0
    profile_revision: int = 1
    profile_reference: str | None = None


@dataclass(frozen=True)
class ResourceWitness:
    subject: str
    generation: int
    kind: Literal["development", "validation"]
    checkout: str
    owned_root: str
    common_git_dir: str
    source_ref: str
    head_sha: str


@dataclass(frozen=True)
class ResourceInspection:
    resource: ResourceWitness
    disposition: Literal["clean", "dirty", "absent", "unknown", "foreign"]
    head_sha: str | None = None
    reason: str = ""


def resource_wire(resource: ResourceWitness) -> dict[str, Any]:
    return asdict(resource)


def inspection_wire(inspection: ResourceInspection) -> dict[str, Any]:
    value = asdict(inspection)
    value["resource"] = resource_wire(inspection.resource)
    return value


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
    actor: str | None
    payload: Mapping[str, Any]
    execution: Execution
    dispatch_serial: int = 1


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
    retry_at: float | None = None
    preparation: AgentPreparation | None = None


class ReadWait(KernelError):
    """A Core-selected read retry, consumed mechanically by the process adapter."""

    def __init__(self, step: Step) -> None:
        self.step = step
        self.retry_at = step.retry_at
        super().__init__(step.reason)


def wire_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact(role: Role, raw: bytes) -> dict[str, Any]:
    """Use the role contract while keeping the Core's existing error boundary."""
    try:
        return parse_artifact(role.value, raw)
    except ArtifactContractError as error:
        raise KernelError("invalid_artifact", str(error)) from error


def review_outcome(data: Mapping[str, Any]) -> str:
    states = {lane["status"] for lane in data["checks"].values()}
    return "fail" if "fail" in states else "blocked" if "blocked" in states else "pass"


@dataclass(frozen=True)
class Repository:
    name_with_owner: str
    default_branch: str
    default_head_sha: str | None


def same_repository(left: object, right: object) -> bool:
    """GitHub owner/name is case-insensitive; retain its display spelling."""
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()
