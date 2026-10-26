"""Exact publication identity of one non-authoritative Status comment."""
from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class StatusTarget:
    repository: str
    run_id: str
    parent_issue: int
    subject: str
    generation: int
    issue: int
    pr_number: int
    actor: str
    publication_effect: str
    publication_receipt: str
    source_ref: str
    target_ref: str

    @property
    def key(self) -> str:
        # Detect PR/actor changes instead of silently adopting another object.
        return hashlib.sha256(json.dumps([self.run_id, self.subject, self.generation]).encode()).hexdigest()

    @property
    def identity(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class StatusProjection:
    target: StatusTarget
    body: str
