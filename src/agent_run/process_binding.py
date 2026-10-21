"""Read-only Linux process identity evidence shared by host and outbox."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal


def _process_start_token(pid: int) -> str | None:
    if pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _process_binding_status(
    pid: int, expected_start_token: object
) -> Literal["matches", "absent", "unknown"]:
    if pid <= 1:
        return "unknown"
    current = _process_start_token(pid)
    if isinstance(expected_start_token, str) and current is not None:
        return "matches" if current == expected_start_token else "absent"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    return "unknown"
