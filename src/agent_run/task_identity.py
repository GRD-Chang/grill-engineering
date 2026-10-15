"""SQL-free identity of a local Delivery Task, also used before runtime loading."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class TaskControlError(ValueError):
    """The task control record is unavailable or violates its contract."""


@dataclass(frozen=True)
class TaskKey:
    """Stable identity of one local Delivery Task."""

    workspace: Path
    repository: str
    parent_number: int

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).resolve()
        if not workspace.is_absolute():  # pragma: no cover - resolve is absolute
            raise TaskControlError("Delivery Task workspace must be absolute")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise TaskControlError("Delivery Task repository must be non-empty")
        if type(self.parent_number) is not int or self.parent_number <= 0:
            raise TaskControlError("Delivery Task Parent Issue must be positive")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "repository", self.repository.strip().lower())

    @property
    def identity(self) -> dict[str, object]:
        return {"workspace": str(self.workspace), "repository": self.repository, "parent": self.parent_number}

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
