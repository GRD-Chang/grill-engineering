"""在真实首次创建 / 对象提交边界注入故障，保留事务和提交后语义。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agent_run.state import StateStore


RunSaveHook = Callable[[str, dict[str, Any]], None]


def observe_run_commits(
    monkeypatch: pytest.MonkeyPatch,
    states: StateStore,
    *,
    before: RunSaveHook | None = None,
    after: RunSaveHook | None = None,
) -> None:
    """故障前置于实际写入；提交后故障由持久化成功 hook 触发。"""
    raw_save = states._create_run_unlocked
    object_save = states._repository.save_objects
    work_save = states._repository.save_work
    after_save = states._after_run_saved
    saved_run_id: str | None = None

    def save_run(run_id: str, state: dict[str, Any]) -> Any:
        nonlocal saved_run_id
        if before is not None:
            before(run_id, state)
        result = raw_save(run_id, state)
        saved_run_id = run_id
        return result

    def save_objects(run_id: str, state: dict[str, Any], **options: Any) -> None:
        nonlocal saved_run_id
        if before is not None:
            before(run_id, state)
        object_save(run_id, state, **options)
        saved_run_id = run_id

    def save_work(
        run_id: str, state: dict[str, Any], baseline: dict[str, Any], **options: Any,
    ) -> None:
        nonlocal saved_run_id
        if before is not None:
            before(run_id, state)
        work_save(run_id, state, baseline, **options)
        saved_run_id = run_id

    def saved() -> None:
        after_save()
        if after is not None and saved_run_id is not None:
            current = states.load_current_run(saved_run_id)
            assert current is not None
            after(saved_run_id, current)

    monkeypatch.setattr(states, "_create_run_unlocked", save_run)
    monkeypatch.setattr(states._repository, "save_objects", save_objects)
    monkeypatch.setattr(states._repository, "save_work", save_work)
    monkeypatch.setattr(states, "_after_run_saved", saved)
