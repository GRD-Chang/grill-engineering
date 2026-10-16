"""Non-persistent loaded ownership snapshots shared with Invocation callbacks."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_run.sqlite_timeline import TimelineScope


ProjectionKey = tuple[str, str, frozenset[str]]


def current_attempt_scope(
    state: Mapping[str, Any], archives: Mapping[str, tuple[int, int]],
    attempts: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep a related proof only while its exact retry relationship remains."""
    from agent_run.sqlite_encoding import encode_json, object_path

    result = {}
    for encoded, attempt in attempts.items():
        path = object_path(encoded)
        owner: Any = state
        for part in path:
            owner = owner.get(part) if isinstance(owner, Mapping) else None
        pending = owner.get("pending_semantic_attempt") if isinstance(owner, Mapping) else None
        if (isinstance(owner, Mapping) and owner.get("publication_operation_retry") is not None
                and encode_json((*path, "semantic_attempt_history")) in archives
                and attempt.get("ordinal") == owner.get("publication_attempts")
                and not (isinstance(pending, Mapping) and pending.get("role") == "publication")):
            result[encoded] = attempt
    return result


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
        self.archive_scope: dict[str, tuple[int, int]] = {}
        self.attempt_scope: dict[str, dict[str, Any]] = {}
        self.timelines: dict[str, TimelineScope] = {}

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

    def copy(self) -> ObjectProjection:
        copied = ObjectProjection(dict(self))
        copied._loaded_baselines = dict(self._loaded_baselines)
        copied._invalidated_baselines = set(self._invalidated_baselines)
        copied.archive_scope = dict(self.archive_scope)
        copied.attempt_scope = deepcopy(self.attempt_scope)
        copied.timelines = deepcopy(self.timelines)
        return copied

    def refresh_scope(self) -> None:
        """Follow the selected identity without inventing unloaded evidence."""
        from agent_run.sqlite_encoding import encode_json, object_path

        selected = self.get("active_ticket_job")
        number = selected.get("ticket_number") if isinstance(selected, dict) else None
        archives = {path: metadata for path, metadata in self.archive_scope.items()
                    if object_path(path)[:1] != ("active_ticket_job",)}
        if type(number) is int:
            prefix = ("ticket_jobs", str(number))
            for path, metadata in list(archives.items()):
                parts = object_path(path)
                if parts[:-1] == prefix:
                    archives[encode_json(("active_ticket_job", parts[-1]))] = metadata

        def unloaded(encoded: str) -> bool:
            parts = object_path(encoded)
            owner: Any = self
            for part in parts[:-1]:
                owner = owner.get(part) if isinstance(owner, dict) else None
            return isinstance(owner, dict) and parts[-1] not in owner

        self.archive_scope = {path: metadata for path, metadata in archives.items() if unloaded(path)}
        attempts = {path: attempt for path, attempt in self.attempt_scope.items()
                    if object_path(path)[:1] != ("active_ticket_job",)}
        target = encode_json(("ticket_jobs", str(number)))
        if target in attempts:
            attempts[encode_json(("active_ticket_job",))] = attempts[target]
        self.attempt_scope = current_attempt_scope(self, self.archive_scope, attempts)

    def __deepcopy__(self, memo: dict[int, Any]) -> ObjectProjection:
        copied = ObjectProjection()
        memo[id(self)] = copied
        for key, value in self.items():
            copied[deepcopy(key, memo)] = deepcopy(value, memo)
        # Records are fixed snapshots, so copies share them without retaining
        # another recursively copied chain of previous snapshots.
        copied._loaded_baselines = dict(self._loaded_baselines)
        copied._invalidated_baselines = set(self._invalidated_baselines)
        copied.archive_scope = dict(self.archive_scope)
        copied.attempt_scope = deepcopy(self.attempt_scope, memo)
        copied.timelines = deepcopy(self.timelines, memo)
        return copied

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        if args and isinstance(args[0], ObjectProjection):
            # Durable readback can replace the view after an unknown write.
            self._loaded_baselines.update(args[0]._loaded_baselines)
            self._invalidated_baselines.difference_update(args[0]._loaded_baselines)
            self._invalidated_baselines.update(args[0]._invalidated_baselines)
            self.archive_scope = dict(args[0].archive_scope)
            self.attempt_scope = deepcopy(args[0].attempt_scope)
            self.timelines = deepcopy(args[0].timelines)
        self.refresh_scope()
