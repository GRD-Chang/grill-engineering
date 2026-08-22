from __future__ import annotations

import sys


def format_execution_binding(
    *,
    role: object,
    thread_id: str | None,
    model: object,
    reasoning_effort: object,
    profile_revision: object,
) -> str:
    return (
        "Agent Execution Binding: "
        f"role={role} "
        f"thread={'resume' if thread_id is not None else 'new'} "
        f"model={model} "
        f"reasoning_effort={reasoning_effort} "
        f"profile_revision={profile_revision} "
        f"thread_id={thread_id if thread_id is not None else 'none'}"
    )


def emit_execution_binding(
    *,
    role: object,
    thread_id: str | None,
    model: object,
    reasoning_effort: object,
    profile_revision: object,
) -> None:
    print(
        format_execution_binding(
            role=role,
            thread_id=thread_id,
            model=model,
            reasoning_effort=reasoning_effort,
            profile_revision=profile_revision,
        ),
        file=sys.stderr,
        flush=True,
    )
