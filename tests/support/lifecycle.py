"""共享测试支撑：lifecycle；每次调用保持独立状态。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from agent_run.task_control import TaskKey
from support.environment import _user_environment


def _task(tmp_path: Path) -> TaskKey:
    return TaskKey(tmp_path / "checkout", "example/project", 156)


def _isolated_environment(root: Path) -> dict[str, str]:
    return _user_environment(root)


def _file_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class _InMemoryRunState:
    def __init__(self) -> None:
        self.value: dict[str, object] = {"run_id": "run-1", "status": "active"}

    @contextmanager
    def locked(self) -> Iterator[None]:
        yield

    def load_current_run(self, run_id: str) -> dict[str, object] | None:
        if self.value.get("run_id") != run_id:
            return None
        return dict(self.value)

    def save_run(self, run_id: str, state: dict[str, object]) -> None:
        if self.value.get("run_id") != run_id:
            raise AssertionError("unexpected Run ID")
        self.value = dict(state)
