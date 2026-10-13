"""Current-read proofs for append-only shared Timeline fields."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import encode_json, object_path, scalar_value

if TYPE_CHECKING:
    from agent_run.sqlite_objects import DecodeContext
    from agent_run.sqlite_state import _RunView

FIELDS = frozenset({"timeline", "timeline_continuation"})


def limit(key: str) -> int:
    # Import lazily: StateStore owns these budgets and imports this reader.
    from agent_run.state import MAX_TIMELINE_EVENTS, MAX_TIMELINE_CONTINUATION_EVENTS
    return MAX_TIMELINE_EVENTS if key == "timeline" else MAX_TIMELINE_CONTINUATION_EVENTS


@dataclass(frozen=True)
class TimelineScope:
    """Canonical text references proved again in this read, never save authority."""
    encoded: str
    entries: tuple[str, ...]
    references: tuple[frozenset[str], ...]

    @property
    def count(self) -> int:
        return len(self.entries)

    def tail(self) -> dict[str, Any] | None:
        return json.loads(self.entries[-1]) if self.entries else None


def prove(
    prior: _RunView | None, rows: list[tuple[str, str, str]], context: DecodeContext,
) -> dict[str, TimelineScope]:
    """Keep every present Timeline complete unless all its current facts agree."""
    if prior is None or prior.fields is None:
        return {}
    grouped: dict[str, dict[str, tuple[str, str]]] = {}
    for path, kind, encoded in rows:
        parsed = object_path(path)
        if parsed[:1] and parsed[0] in FIELDS:
            key = str(parsed[0])
            group = grouped.setdefault(key, {})
            if path in group:
                return {}
            group[path] = kind, encoded
    if "timeline" not in grouped:
        return {}
    scopes = {}
    for key, objects in grouped.items():
        root = encode_json((key,))
        item = objects.get(root)
        if item is None or item[0] != "list" or prior.objects.get(root) != item:
            return {}
        count = scalar_value(item[1])
        if type(count) is not int or not 0 <= count <= limit(key) or len(objects) != count + 1:
            return {}
        texts, references = [], []
        for position in range(count):
            path = encode_json((key, position))
            child = objects.get(path)
            if child is None or child[0] != "snapshot" or prior.objects.get(path) != child:
                return {}
            identity = scalar_value(child[1])
            text = context.snapshot_encodings.get(identity)
            refs = context.snapshot_refs.get(identity)
            if (text is None or not text.startswith("{") or refs is None
                    or context.object_refs.get(path) != set(refs)):
                return {}
            texts.append(text)
            references.append(refs)
        encoded = "[" + ",".join(texts) + "]"
        if prior.fields.get(key) != encoded:
            # A full view may contain descendant overrides. Its assembled
            # field must equal these individually proved snapshot encodings.
            return {}
        scopes[key] = TimelineScope(prior.fields[key], tuple(texts), tuple(references))
    return scopes


def append_events(
    state: dict[str, Any], previous: dict[str, Any], scopes: dict[str, TimelineScope],
) -> dict[str, dict[str, Any]]:
    """Use the same marker and duplicate rules without expanding old events."""
    from agent_run.state import (
        MAX_TIMELINE_EVENTS, _timeline_change, _timeline_capacity_event,
        _continuation_kind, _event_matches_marker, _timeline_event,
    )

    marker, previous_marker = _timeline_change(state, previous)
    if marker is None:
        return {}
    updates = {}
    timeline = scopes["timeline"]
    if state.get("timeline_at_capacity") is not True:
        if timeline.count >= MAX_TIMELINE_EVENTS - 1:
            updates["timeline"] = _timeline_capacity_event(state)
            state["timeline_at_capacity"] = True
        elif not _event_matches_marker(timeline.tail(), marker):
            updates["timeline"] = _timeline_event(marker, previous_marker)
    if state.get("timeline_at_capacity") is True:
        continuation_marker = dict(marker)
        continuation_marker["kind"] = _continuation_kind(marker, previous_marker)
        continuation = scopes.get("timeline_continuation")
        if not _event_matches_marker(continuation.tail() if continuation else None, continuation_marker):
            event = _timeline_event(continuation_marker, previous_marker)
            if continuation is None:
                # This field did not exist in the actual baseline. Its new
                # single event is complete, so the ordinary writer handles it.
                state["timeline_continuation"] = [event]
            else:
                updates["timeline_continuation"] = event
    return updates
