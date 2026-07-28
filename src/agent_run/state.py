from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_directory = root / "runs"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        destination = self.runs_directory / f"{run_id}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.runs_directory,
            prefix=f".{run_id}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    state,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
            self._sync_directory(self.runs_directory)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs_directory / f"{run_id}.json"
        if not path.exists():
            return None
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid run state: {path}")
        return loaded

    def find_run(self, repository: str, parent_number: int) -> dict[str, Any] | None:
        if not self.runs_directory.exists():
            return None
        matches: list[dict[str, Any]] = []
        for path in self.runs_directory.glob("*.json"):
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                continue
            parent = loaded.get("parent")
            if (
                loaded.get("repository") == repository
                and isinstance(parent, dict)
                and parent.get("number") == parent_number
            ):
                matches.append(loaded)
        if not matches:
            return None
        return max(matches, key=lambda state: str(state.get("created_at", "")))

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
