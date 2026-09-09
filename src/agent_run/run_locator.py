from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict


MAX_LOCATOR_ENTRIES = 32


class LocatorEntry(TypedDict):
    run_id: str
    repository_root: str
    state_dir: str
    updated_at: str
    repository: NotRequired[str]
    parent_number: NotRequired[int]


class RunLocatorError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


class RunLocatorIndex:
    """A bounded local index for locating new Run state in read-only commands."""

    def __init__(
        self, path: Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def default(cls) -> RunLocatorIndex:
        state_home = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        ).expanduser().resolve()
        return cls(state_home / "agent-run" / "run-locator.json")

    def register(
        self,
        *,
        run_id: str,
        repository_root: Path,
        state_dir: Path,
        repository: str | None = None,
        parent_number: int | None = None,
    ) -> None:
        entry: LocatorEntry = {
            "run_id": run_id,
            "repository_root": str(repository_root.resolve()),
            "state_dir": str(state_dir.resolve()),
            "updated_at": self._now().isoformat(),
        }
        if repository is not None or parent_number is not None:
            if not _valid_route(repository, parent_number):
                raise ValueError(
                    "Run locator requires a repository and positive Parent number"
                )
            assert repository is not None and parent_number is not None
            entry["repository"] = repository
            entry["parent_number"] = parent_number
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            entries = self._entries_or_raise()
            entries = [
                item
                for item in entries
                if Path(item["state_dir"]).is_dir()
                and not (
                    item["run_id"] == run_id
                    and item["state_dir"] == entry["state_dir"]
                )
            ]
            conflicts = [item for item in entries if item["run_id"] == run_id]
            if conflicts:
                raise RunLocatorError(
                    "run_locator_conflict",
                    _locator_message(
                        run_id,
                        "定位索引中存在多个不同状态目录的同名 Run，"
                        "请显式提供 --state-dir。",
                    ),
                )
            entries.append(entry)
            entries.sort(key=lambda item: item["updated_at"], reverse=True)
            self._write_entries(entries[:MAX_LOCATOR_ENTRIES])

    def resolve_state_dir(self, run_id: str) -> Path:
        if not self.path.exists():
            raise RunLocatorError(
                "run_locator_missing",
                _locator_message(run_id, "本机定位索引不存在或未登记该 Run。"),
            )
        entries = self._entries_or_raise()
        matches = [entry for entry in entries if entry["run_id"] == run_id]
        if not matches:
            raise RunLocatorError(
                "run_locator_missing",
                _locator_message(run_id, "本机定位索引未登记该 Run。"),
            )
        state_dirs = {entry["state_dir"] for entry in matches}
        if len(state_dirs) != 1:
            raise RunLocatorError(
                "run_locator_conflict",
                _locator_message(
                    run_id,
                    "定位索引指向多个状态目录，请显式提供 --state-dir。",
                ),
            )
        state_dir = Path(next(iter(state_dirs)))
        if not (state_dir / "runs" / f"{run_id}.json").is_file():
            raise RunLocatorError(
                "run_locator_stale",
                _locator_message(
                    run_id, f"定位索引已失效（记录的状态目录：{state_dir}）。"
                ),
            )
        return state_dir

    def entries(self) -> list[LocatorEntry]:
        """Read the bounded index without pruning or otherwise mutating it."""

        return [entry.copy() for entry in self._entries_or_raise()]

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.parent / ".run-locator.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _entries_or_raise(self) -> list[LocatorEntry]:
        if not self.path.exists():
            return []
        try:
            loaded: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RunLocatorError(
                "run_locator_invalid", "本机 Run 定位索引无法读取；请显式提供 --state-dir。"
            ) from error
        if not isinstance(loaded, dict) or set(loaded) != {"entries"}:
            raise RunLocatorError(
                "run_locator_invalid", "本机 Run 定位索引格式无效；请显式提供 --state-dir。"
            )
        raw_entries = loaded["entries"]
        if not isinstance(raw_entries, list):
            raise RunLocatorError(
                "run_locator_invalid", "本机 Run 定位索引格式无效；请显式提供 --state-dir。"
            )
        if len(raw_entries) > MAX_LOCATOR_ENTRIES:
            raise RunLocatorError(
                "run_locator_invalid", "本机 Run 定位索引超过最大条目数；请显式提供 --state-dir。"
            )
        entries: list[LocatorEntry] = []
        routes: dict[str, tuple[str, int]] = {}
        fields = {"run_id", "repository_root", "state_dir", "updated_at"}
        for raw_entry in raw_entries:
            if (
                not isinstance(raw_entry, dict)
                or set(raw_entry) not in (fields, fields | {"repository", "parent_number"})
                or not all(
                    isinstance(raw_entry[key], str) and raw_entry[key] for key in fields
                )
                or (
                    "repository" in raw_entry
                    and not _valid_route(raw_entry["repository"], raw_entry["parent_number"])
                )
            ):
                raise RunLocatorError(
                    "run_locator_invalid", "本机 Run 定位索引格式无效；请显式提供 --state-dir。"
                )
            if not (
                Path(raw_entry["repository_root"]).is_absolute()
                and Path(raw_entry["state_dir"]).is_absolute()
            ):
                raise RunLocatorError(
                    "run_locator_invalid", "本机 Run 定位索引格式无效；请显式提供 --state-dir。"
                )
            entry: LocatorEntry = {
                "run_id": raw_entry["run_id"],
                "repository_root": raw_entry["repository_root"],
                "state_dir": raw_entry["state_dir"],
                "updated_at": raw_entry["updated_at"],
            }
            if "repository" in raw_entry:
                entry["repository"] = raw_entry["repository"]
                entry["parent_number"] = raw_entry["parent_number"]
                route = (entry["repository"], entry["parent_number"])
                if entry["run_id"] in routes and routes[entry["run_id"]] != route:
                    raise RunLocatorError(
                        "run_locator_conflict",
                        "本机 Run 定位索引的同一 Run 身份不一致；请显式提供 --state-dir。",
                    )
                routes[entry["run_id"]] = route
            entries.append(entry)
        return entries

    def _write_entries(self, entries: list[LocatorEntry]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".run-locator.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    {"entries": entries}, temporary_file, ensure_ascii=False, indent=2
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _valid_route(repository: object, parent_number: object) -> bool:
    return (
        isinstance(repository, str)
        and len(repository.split("/")) == 2
        and all(repository.split("/"))
        and not any(character.isspace() for character in repository)
        and type(parent_number) is int
        and parent_number > 0
    )


def _locator_message(run_id: str, detail: str) -> str:
    return (
        f"无法定位 Delivery Run {run_id!r}：{detail}"
        "请在目标仓库中执行，或显式提供 --state-dir <状态目录>。"
    )
