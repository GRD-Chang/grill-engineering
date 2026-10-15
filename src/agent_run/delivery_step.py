from __future__ import annotations

"""Results of one delivery advance; durable jobs remain the sole authority."""

from dataclasses import dataclass
from enum import Enum


class DeliveryStepKind(str, Enum):
    CONTINUE = "continue"
    WAITING = "waiting"
    NEEDS_AGENT = "needs_agent"
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass(frozen=True)
class DeliveryStep:
    kind: DeliveryStepKind
    phase: str
    reason: str | None = None


class AgentWorkRequired(Exception):
    """Yield before allocating an attempt when orchestration has no work slot."""

    def __init__(self, role: str) -> None:
        super().__init__(role)
        self.role = role
