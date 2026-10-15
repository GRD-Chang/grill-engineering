"""Join control and business repositories in the same thread's short transaction."""
from __future__ import annotations

from pathlib import Path
from threading import Lock, local
from weakref import WeakValueDictionary


_contexts: WeakValueDictionary[Path, local] = WeakValueDictionary()
_registry_lock = Lock()


def context_for(root: Path) -> local:
    with _registry_lock:
        key = root.resolve()
        context = _contexts.get(key)
        if context is None:
            context = local()
            _contexts[key] = context
        return context
