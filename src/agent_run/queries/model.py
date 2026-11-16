"""Native read models: one owner for current facts, with bounded original records."""
from typing import Any, Literal, NotRequired, TypedDict


class Scheduling(TypedDict):
    concurrency: int
    integration_holder: str | None
    active: list[dict[str, Any]]
    work_fifo: list[str]
    integration_fifo: list[str]


class HistoryPage(TypedDict):
    """Page-first original records and their exact identity closure; never current substitutes."""
    time_zone: Literal["UTC"]
    events: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    effects: list[dict[str, Any]]
    effect_failures: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    rounds: dict[str, int]
    after: int
    next_after: int
    has_more: bool


class RunStatus(TypedDict):
    run_id: str
    repository: str
    parent: dict[str, Any]
    status: str
    phase: str
    created_at: float | None
    elapsed_seconds: float | None
    repository_root: str | None
    parent_revision: str
    graph_revision: str
    policy_snapshot: dict[str, Any]
    subjects: list[dict[str, Any]]
    focus_subject: str | None
    scheduling: Scheduling
    operator_actions: list[dict[str, Any]]
    next_action: str
    diagnostics: list[dict[str, Any]]
    notifications: dict[str, Any]
    executor_control: dict[str, Any]
    delivery_cleanup: dict[str, Any] | None
    supervision_windows: list[dict[str, Any]]
    latest_resume: dict[str, Any] | None
    resume_waits: list[dict[str, Any]]
    abandonment: dict[str, Any] | None
    unavailable_facts: list[str]
    history: NotRequired[HistoryPage]
    current_observation: NotRequired[dict[str, Any]]
