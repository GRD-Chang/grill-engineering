"""Short-lived CLI supervisor that survives the Executor long enough to clean up.

The Executor holds this process's stdin pipe open. EOF means the owner died or
cancelled; a separate deadline also bounds the CLI if the owner stops running.
"""

from __future__ import annotations

import selectors
import subprocess
import sys
import time

from .process_cleanup import child_subreaper, terminate_process_group


def main() -> int:
    deadline = time.monotonic() + min(10.0, max(0.0, float(sys.argv[1])))
    with child_subreaper():
        try:
            child = subprocess.Popen(
                sys.argv[2:], stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            return 127
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ)
                while child.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or selector.select(min(0.05, remaining)):
                        return 124
            return child.returncode or 0
        finally:
            # This dedicated process owns no unrelated children. Adopted
            # descendants include CLI helpers that change their own session.
            terminate_process_group(child, adopted_baseline=set(), timeout=0.5)


if __name__ == "__main__":
    raise SystemExit(main())
