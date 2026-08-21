from __future__ import annotations

"""Durable Change Delivery state persistence and shape validation."""

from dataclasses import dataclass
from typing import Any

from agent_run.state import StateStore


@dataclass(frozen=True)
class ChangeDeliveryStateStore:
    """Persist one Run through the existing atomic StateStore seam."""

    states: StateStore
    run_id: str

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(self.run_id, state)
        return state


def require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must contain strings")
    return list(value)
