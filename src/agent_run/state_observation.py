"""观察在提交后执行；自定义观察的各字段属于同一次保存快照。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agent_run.object_projection import ObjectProjection
from agent_run.sqlite_timeline import TimelineScope

if TYPE_CHECKING:
    from agent_run.state import StateStore


class CurrentRunObserver:
    """Declare that this observer consumes the current observation query."""

    def __init__(self, observe: Callable[[dict[str, Any]], None]) -> None:
        self._observe = observe

    def __call__(self, state: dict[str, Any]) -> None:
        self._observe(state)


def saved_observation(
    store: StateStore,
    run_id: str,
    state: dict[str, Any],
    timelines: dict[str, TimelineScope],
) -> dict[str, Any] | Callable[[], dict[str, Any] | None] | None:
    observer = store.run_saved_observer
    if observer is None:
        return None
    if isinstance(observer, CurrentRunObserver):
        return lambda: store.load_observation_run(run_id)
    saved = deepcopy(state)
    if not timelines:
        return saved

    try:
        # Capture a custom subscriber's complete Timeline in the same write
        # snapshot as its other fields. A later save in an outer admission
        # scope must not replace the earlier observation's Timeline.
        loaded = store._read_run_materials(
            run_id, lambda: store._repository.load_fields(run_id, set(timelines))
        )
        if loaded is None:
            raise ValueError("Delivery Run disappeared while preparing its saved observation")
        for field in timelines:
            if field in loaded:
                saved[field] = loaded[field]
            else:
                saved.pop(field, None)
        if isinstance(saved, ObjectProjection):
            saved.timelines = {}
        return saved
    except Exception as error:
        # A storage error that has already rolled back SQLite cannot be
        # presented as a successful business save. Ordinary observation
        # preparation failures are reported only after the commit and unlock.
        if not store._repository._local.connection.in_transaction:
            raise

        def failed(error: Exception = error) -> None:
            raise error

        return failed
