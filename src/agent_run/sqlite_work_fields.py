"""Reuse complete field encodings only after current source and closure proofs."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from agent_run.sqlite_encoding import object_path, scalar_value
from agent_run.sqlite_timeline import FIELDS as TIMELINE_FIELDS

if TYPE_CHECKING:
    from agent_run.sqlite_state import _RunView
    from agent_run.sqlite_work_objects import DecodeContext, ParsedRow

_SPARSE_FIELDS = frozenset({
    "agent_invocation_history", "ticket_agent_invocations", "ticket_jobs",
    "active_ticket_job", *TIMELINE_FIELDS,
})


def field_node_counts(objects: Mapping[str, tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw in objects:
        path = object_path(raw)
        if path and isinstance(path[0], str):
            counts[path[0]] = counts.get(path[0], 0) + 1
    return counts


def reuse_fields(prior: _RunView | None, rows: list[ParsedRow], context: DecodeContext) -> dict[str, str]:
    if prior is None or prior.fields is None:
        return {}
    previous_counts = prior.field_node_counts
    candidates = {key for key, text in prior.fields.items()
                  if key not in _SPARSE_FIELDS and previous_counts.get(key, 0) >= 4
                  and text.startswith(("{", "["))}
    if not candidates:
        return {}
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for raw, path, kind, encoded in rows:
        if not path or path[0] not in candidates:
            continue
        key = path[0]
        assert isinstance(key, str)
        if raw in seen or prior.objects.get(raw) != (kind, encoded):
            candidates.discard(key)
            continue
        seen.add(raw)
        counts[key] = counts.get(key, 0) + 1
        if kind not in {"material", "snapshot"}:
            continue
        identity = scalar_value(encoded)
        references = (frozenset({identity}) if kind == "material"
                      else prior.snapshot_refs.get(raw))
        actual = context.object_refs.get(raw)
        if (references is None or actual is None or len(actual) != len(references)
                or not references.issubset(actual) or not references <= context.materials.keys()):
            candidates.discard(key)
    fields = {key: prior.fields[key] for key in candidates
              if counts.get(key) == previous_counts.get(key)}
    for raw, path, kind, encoded in rows:
        if path and path[0] in fields and kind in {"material", "snapshot"}:
            context.verified_objects[raw] = kind, encoded
    return fields
