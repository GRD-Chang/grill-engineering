"""Select native libraries before importing Executor application state."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_run.executor_environment import EXIT_ENVIRONMENT_CREDENTIAL, read_environment_carrier
from agent_run.task_identity import TaskKey


_READY = "--runtime-environment-ready"


def reexec_for_runtime(module: str) -> None:
    """Peek, then exec the same Snapshot Python; the original owner consumes.

    glibc reads LD_LIBRARY_PATH at process startup. Changing os.environ before
    importing sqlite3 is insufficient. No environment value enters unit metadata
    or argv, and the startup carrier remains present across the lease handoff.
    """
    arguments = sys.argv[1:]
    if arguments[-1:] == [_READY]:
        del sys.argv[-1]
        return
    if "--help" in arguments or "-h" in arguments:
        return
    parser = argparse.ArgumentParser(add_help=False)
    for key in ("workspace", "repository", "action-id", "state-root"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--parent", required=True, type=int)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--carrier")
    args, _ = parser.parse_known_args(arguments)
    try:
        task = TaskKey(Path(args.workspace), args.repository, args.parent)
        root = Path(args.state_root)
        if not root.is_absolute():
            raise ValueError("state root must be absolute")
        binding = {"task": task.fingerprint, "action_id": args.action_id,
                   "run_id": args.run_id, "generation": args.generation, "state_root": str(root.resolve())}
        is_exit = module == "agent_run.executor_exit"
        path = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / EXIT_ENVIRONMENT_CREDENTIAL
                if is_exit else Path(args.carrier))
        environment, _ = read_environment_carrier(path, binding, check_expiry=not is_exit)
        # Restore only loader selection here. The existing entrypoint owns the
        # complete terminal snapshot and its filtering/native-metadata rules.
        native = dict(os.environ)
        native.pop("LD_LIBRARY_PATH", None)
        if "LD_LIBRARY_PATH" in environment:
            native["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH"]
        os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:], _READY], native)
    except Exception:
        print("Executor runtime environment unavailable", file=sys.stderr)
        raise SystemExit(1) from None
