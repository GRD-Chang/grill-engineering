"""Non-persistent loaded ownership snapshots shared with Invocation callbacks."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


ProjectionKey = tuple[str, str, frozenset[str]]


@dataclass(frozen=True)
class ProjectionBaseline:
    # A private independent snapshot: consumers compare it, never mutate it.
    facts: dict[str, Any]
    history_positions: dict[str, int]


class ObjectProjection(dict[str, Any]):
    """A normal Run view carrying its read baseline outside its JSON facts.

    Stages pass this same view to stdout-reader callbacks. The baseline follows
    the view across threads and copies; it is not another persistent authority.
    """

    def __init__(self, facts: dict[str, Any] | None = None) -> None:
        super().__init__(facts or {})
        self._loaded_baselines: dict[ProjectionKey, ProjectionBaseline] = {}
        self._invalidated_baselines: set[ProjectionKey] = set()

    def baseline(self, key: ProjectionKey) -> ProjectionBaseline | None:
        return self._loaded_baselines.get(key)

    def record_baseline(self, key: ProjectionKey, baseline: ProjectionBaseline) -> None:
        self._loaded_baselines[key] = baseline
        self._invalidated_baselines.discard(key)

    def baseline_is_invalid(self, key: ProjectionKey) -> bool:
        return key in self._invalidated_baselines

    def invalidate_baseline(self, key: ProjectionKey) -> None:
        self._loaded_baselines.pop(key, None)
        self._invalidated_baselines.add(key)

    def __deepcopy__(self, memo: dict[int, Any]) -> ObjectProjection:
        copied = ObjectProjection()
        memo[id(self)] = copied
        for key, value in self.items():
            copied[deepcopy(key, memo)] = deepcopy(value, memo)
        # Records are fixed snapshots, so copies share them without retaining
        # another recursively copied chain of previous snapshots.
        copied._loaded_baselines = dict(self._loaded_baselines)
        copied._invalidated_baselines = set(self._invalidated_baselines)
        return copied

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        if args and isinstance(args[0], ObjectProjection):
            # Durable readback can replace the view after an unknown write.
            self._loaded_baselines.update(args[0]._loaded_baselines)
            self._invalidated_baselines.difference_update(args[0]._loaded_baselines)
            self._invalidated_baselines.update(args[0]._invalidated_baselines)
