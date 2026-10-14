"""Current-read proofs for append-only shared Timeline fields."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
import json
from sys import getsizeof
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agent_run.sqlite_encoding import scalar_value

if TYPE_CHECKING:
    from agent_run.sqlite_state import _RunView
    from agent_run.sqlite_work_objects import DecodeContext, ParsedRow

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


@dataclass(frozen=True)
class ScopeSource:
    """Immutable metadata referring to an existing complete bounded view."""
    scope: TimelineScope
    headers: Mapping[str, tuple[str, str]]
    references: Mapping[str, frozenset[str]]
    refunion: frozenset[str]


def scopes_from_view(view: _RunView) -> Mapping[str, ScopeSource]:
    if view.fields is None or "timeline" not in view.fields:
        return MappingProxyType({})
    # Keep the original logical and physical view bounds after adding only
    # pointer containers. All strings/entry closures below already belong to
    # the view; no decoded body or additional canonical text is retained.
    physical = view.physical_bytes
    if physical is None:
        physical = sum(len(raw.encode("utf-8")) + len(encoded.encode("utf-8"))
                       for raw, (_, encoded) in view.objects.items())
        physical += sum(len(text.encode("utf-8")) for text in view.materials.values())
    available = 512 * 1024 - max(view.logical_bytes, physical)
    sources: dict[str, ScopeSource] = {}
    used = getsizeof(sources) + getsizeof(MappingProxyType(sources)) + getsizeof(view.field_node_counts)
    used += sum(getsizeof(key) + getsizeof(count) for key, count in view.field_node_counts.items())
    for key in FIELDS.intersection(view.fields):
        root = f'["{key}"]'
        item = view.objects.get(root)
        if item is None or item[0] != "list" or not view.fields[key].startswith("["):
            continue
        count = scalar_value(item[1])
        if (type(count) is not int or not 0 <= count <= limit(key)
                or view.field_node_counts.get(key) != count + 1):
            continue
        prefix = root[:-1] + ","
        headers = {raw: node for raw, node in view.objects.items()
                   if raw == root or raw.startswith(prefix)}
        if len(headers) != count + 1:
            continue
        entries, refs = [], []
        for position in range(count):
            path = f'["{key}",{position}]'
            child = headers.get(path)
            text, closure = view.snapshots.get(path), view.snapshot_refs.get(path)
            if (child is None or child[0] != "snapshot" or text is None or not text.startswith("{")
                    or closure is None or not closure <= view.materials.keys()):
                break
            identity = scalar_value(child[1])
            if not isinstance(identity, str) or identity not in closure:
                break
            entries.append(text)
            refs.append(closure)
        else:
            # Preserve the existing path string keys and immutable closures.
            references = {raw: view.snapshot_refs[raw] for raw in headers if raw != root}
            scope = TimelineScope(view.fields[key], tuple(entries), tuple(refs))
            union = frozenset().union(*refs)
            source = ScopeSource(scope, MappingProxyType(headers), MappingProxyType(references), union)
            cost = sum(getsizeof(value) for value in (source, scope, headers, references,
                source.headers, source.references, scope.entries, scope.references, union,
                source.__dict__, scope.__dict__))
            # A one-entry dictionary's growth is a conservative bound for
            # each of the at most two source-map entries.
            cost += getsizeof({key: source}) - getsizeof({})
            if used + cost <= available:
                sources[key] = source
                used += cost
    return MappingProxyType(sources)


def prove(
    rows: list[ParsedRow], context: DecodeContext, restore: Callable[[str], Any],
    *, sources: Mapping[str, ScopeSource] | None = None,
) -> dict[str, TimelineScope]:
    """Keep every present Timeline complete unless all its current facts agree."""
    grouped: dict[str, dict[str, tuple[str, str]]] = {}
    for path, parsed, kind, encoded in rows:
        if parsed and parsed[0] in FIELDS:
            key = str(parsed[0])
            group = grouped.setdefault(key, {})
            if path in group:
                return {}
            group[path] = kind, encoded
    if "timeline" not in grouped:
        return {}
    scopes = {}
    for key, objects in grouped.items():
        prior = sources.get(key) if sources is not None else None
        if (prior is not None and objects == prior.headers
                and prior.refunion <= context.materials.keys()
                and all((actual := context.object_refs.get(path)) is not None
                        and len(actual) == len(refs) and refs.issubset(actual)
                        for path, refs in prior.references.items())):
            scopes[key] = prior.scope
            continue
        # Field names are the two fixed ASCII protocol keys; indices are
        # bounded nonnegative ints. This is the exact canonical path spelling.
        root = f'["{key}"]'
        item = objects.get(root)
        if item is None or item[0] != "list":
            return {}
        count = scalar_value(item[1])
        if type(count) is not int or not 0 <= count <= limit(key) or len(objects) != count + 1:
            return {}
        texts, references = [], []
        for position in range(count):
            path = f'["{key}",{position}]'
            child = objects.get(path)
            if child is None or child[0] != "snapshot":
                return {}
            identity = scalar_value(child[1])
            if not isinstance(identity, str):
                return {}
            text = context.snapshot_encodings.get(identity, context.restored_encodings.get(identity))
            if text is None:
                # Only the standard current-read decoder may establish new
                # canonical text and its transitive material closure.
                restore(identity)
                text = context.restored_encodings.get(identity)
            refs = context.snapshot_refs.get(identity)
            actual = context.object_refs.get(path)
            if (text is None or not text.startswith("{") or refs is None
                    or actual is None or len(actual) != len(refs) or not refs.issubset(actual)):
                return {}
            texts.append(text)
            references.append(refs)
        encoded = "[" + ",".join(texts) + "]"
        # count+1 exact consecutive nodes excluded descendant overrides;
        # every entry now carries this read's actual material/reference proof.
        scopes[key] = TimelineScope(encoded, tuple(texts), tuple(references))
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
