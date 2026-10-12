"""Native bounded patches preserve values and roll back all chunks on failure."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_run import sqlite_state
from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("mode", ["native", "raw", "legacy_nul"])
@pytest.mark.parametrize("run_id", ["run-1", "run\x00中文"])
@pytest.mark.parametrize("body", ["\x00" + "中文" * 600, "中文" * 300 + "\x00tail", "中文" * 600 + "\x00"],
                         ids=["leading", "middle", "trailing"])
def test_small_nul_materials_preserve_content_references_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, run_id: str, body: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    if mode == "raw":
        repository._aggregate_rows = False
    elif mode == "legacy_nul":
        patch = sqlite_state.patch_run

        def legacy_extract(encoded, path):
            value = json.loads(encoded)[int(path[2:-1])]
            # Older JSON1 decoders return only the prefix before an embedded
            # NUL. Keep this regression reproducible on newer SQLite builds.
            return value.split("\x00", 1)[0] if isinstance(value, str) else value

        def legacy_patch(connection, *args, **kwargs):
            connection.create_function("json_extract", 2, legacy_extract)
            return patch(connection, *args, **kwargs)

        monkeypatch.setattr(sqlite_state, "patch_run", legacy_patch)
    state = {"body": body, "review_context": {"body": body}, "version": 1}
    repository.save(run_id, state)
    repository.save("shared", {"body": body})
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            "SELECT value, byte_length FROM run_materials WHERE value = ?", (body,),
        ).fetchall() == [(body, len(body.encode("utf-8")))]
    reopened = SQLiteStateRepository(tmp_path, max_bytes=65536)
    assert reopened.load(run_id) == state
    with pytest.raises(OSError, match="before commit"):
        with repository.atomic():
            repository.save(run_id, {"version": 2})
            raise OSError("before commit")
    assert reopened.load(run_id) == state
    repository.save(run_id, {"version": 2})
    assert reopened.load(run_id) == {"version": 2}
    assert reopened.load("shared") == {"body": body}
    repository.save("shared", {"version": 3})
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_materials").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["native", "raw", "missing_json_table", "missing_json_scalar"])
def test_chunked_patches_preserve_special_text_and_exact_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    original = {"old": "original material " * 200}
    repository.save("run-1", original)
    if mode == "raw":
        repository._aggregate_rows = False
    elif mode.startswith("missing_json"):
        missing = ("no such table: json_each" if mode == "missing_json_table"
                   else "no such function: json_extract")
        patch = sqlite_state.patch_run

        class WithoutJson:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, args=()):
                if "json_each" in sql:
                    raise sqlite3.OperationalError(missing)
                return self.connection.execute(sql, args)

            def executemany(self, sql, rows):
                return self.connection.executemany(sql, rows)

        monkeypatch.setattr(sqlite_state, "patch_run", lambda connection, *args, **kwargs:
                            patch(WithoutJson(connection), *args, **kwargs))
    changed = {
        "values": {f'key-{number}\x00中文\\\"': [True, 1, 1.0, -0.0, '中文\t\x00\\"\n']
                   for number in range(400)},
        "body": '["snapshot","literal"]\x00中文\n' * 3000,
    }
    with repository.atomic():
        assert repository.load("run-1") == original
        repository.save("run-1", changed)
    saved = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024).load("run-1")
    assert json.dumps(saved, sort_keys=True) == json.dumps(changed, sort_keys=True)
    assert repository.load("run-1") == changed


@pytest.mark.parametrize("native", [False, True])
def test_later_chunk_constraint_failure_rolls_back_earlier_chunks_and_materials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = {"evidence": "original " * 300}
    repository.save("run-1", original)
    original_objects = repository._objects

    def invalid_objects(*args, **kwargs):
        objects, materials = original_objects(*args, **kwargs)
        # Enough real changes to complete the first bounded SQL batch before
        # the existing CHECK constraint rejects a later node.
        location = json.dumps(["values", "key-350"], separators=(",", ":"))
        objects[location] = ("invalid-kind", "1")
        return objects, materials

    repository._aggregate_rows = native
    monkeypatch.setattr(repository, "_objects", invalid_objects)
    with pytest.raises(OSError, match="CHECK constraint failed"):
        with repository.atomic():
            assert repository.load("run-1") == original
            repository.save("run-1", {"values": {f"key-{n}": n for n in range(400)},
                                       "evidence": "replacement " * 300})
    assert repository.load("run-1") == original
    assert SQLiteStateRepository(tmp_path, max_bytes=65536).load("run-1") == original
