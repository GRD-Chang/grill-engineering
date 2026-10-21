"""一次查询的明确工作范围；这些对象既不持久化，也不授予执行权。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class JobHistoryAppend(list[Any]):
    """向准确 Job 档案追加的已知后缀，保留未读取前缀的实际范围。"""

    def __init__(self, values: Any = (), *, base_bytes: int, base_count: int) -> None:
        super().__init__(values)
        self.base_bytes = base_bytes
        self.base_count = base_count


@dataclass(frozen=True)
class ExpectedPublicationAttempt:
    """准确读取的不可变版本身份；不承载可写 Attempt 副本。"""

    run_id: str
    job_id: str
    attempt_id: str
    version_id: str
    ordinal: int


@dataclass(frozen=True)
class RepairBudgetScope:
    run_id: str
    job_id: str
    generations: frozenset[str]


class RepairBudgetProjection(dict[str, Any]):
    """本次读取的准确 Repair 额度；范围随映射复制，不替换父 Job。"""

    def __init__(self, values: Mapping[str, Any], *, scope: RepairBudgetScope) -> None:
        super().__init__(values)
        self.scope = scope

    def copy(self) -> RepairBudgetProjection:
        return RepairBudgetProjection(self, scope=self.scope)

    def __deepcopy__(self, memo: dict[int, Any]) -> RepairBudgetProjection:
        result = self.copy()
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result


class JobProjection(dict[str, Any]):
    """有界当前 Job；旧材料只有显式业务消费才按准确身份补读。"""

    def __init__(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        job_id: str,
        revision: int,
        archives: Mapping[str, tuple[int, int]],
    ) -> None:
        super().__init__(values or {})
        self.job_id = job_id
        self.revision = revision
        self.archives = dict(archives)
        self.loaded_histories: set[str] = set()
        self.history_reader: Callable[[JobProjection, set[str]], dict[str, list[Any]]] | None = None
        self.previous_review_reader: Callable[[JobProjection], dict[str, Any] | None] | None = None
        self.latest_thread_reader: Callable[[JobProjection, str], str | None] | None = None
        self.publication_reader: Callable[[JobProjection], dict[str, Any] | None] | None = None
        self.expected_publication_attempt: ExpectedPublicationAttempt | None = None

    def materialize(self, field: str) -> Any:
        self.materialize_histories({field})
        return self.get(field)

    def materialize_histories(self, fields: set[str]) -> None:
        requested = (fields & self.archives.keys()) - self.loaded_histories
        if not requested:
            return
        if self.history_reader is None:
            names = next(iter(requested)) if len(requested) == 1 else str(sorted(requested))
            raise ValueError(f"Job history requires its exact query reader: {names}")
        values = self.history_reader(self, requested)
        for field in requested:
            self._materialize_history(field, values[field])

    def _materialize_history(self, field: str, value: list[Any]) -> None:
        pending = self.get(field)
        if isinstance(pending, JobHistoryAppend):
            overlap = len(value) - pending.base_count
            if overlap < 0 or overlap > len(pending) or value[pending.base_count :] != pending[:overlap]:
                raise ValueError("Job history changed before materializing its known append")
            value.extend(pending[overlap:])
        self[field] = value
        self.loaded_histories.add(field)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self and key in self.archives:
            if default != []:
                raise ValueError(f"Unloaded Job history requires an explicit append: {key}")
            size, count = self.archives[key]
            self[key] = JobHistoryAppend(base_bytes=size, base_count=count)
        return super().setdefault(key, default)

    def copy(self) -> JobProjection:
        result = JobProjection(self, job_id=self.job_id, revision=self.revision, archives=self.archives)
        result.loaded_histories = set(self.loaded_histories)
        result.history_reader = self.history_reader
        result.previous_review_reader = self.previous_review_reader
        result.latest_thread_reader = self.latest_thread_reader
        result.publication_reader = self.publication_reader
        result.expected_publication_attempt = self.expected_publication_attempt
        return result

    def __deepcopy__(self, memo: dict[int, Any]) -> JobProjection:
        result = self.copy()
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result


@dataclass(frozen=True)
class ResumeScope:
    run_id: str
    total: int
    digest: str | None
    identities: frozenset[tuple[int, str]]


class ResumeHistory(list[dict[str, Any]]):
    """当前调用关联的 Resume 事件，未读事件仍由同一查询的真实总量描述。"""

    def __init__(self, values: Any = (), *, scope: ResumeScope) -> None:
        super().__init__(values)
        self.scope = scope


def materialize_history(owner: dict[str, Any], field: str, default: Any = None) -> Any:
    if isinstance(owner, JobProjection):
        owner.materialize(field)
    return owner.get(field, default)


def history_for_append(owner: dict[str, Any], field: str) -> list[Any]:
    value = owner.setdefault(field, [])
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def latest_thread_identity(owner: dict[str, Any], field: str) -> str | None:
    """读取准确末项，不为 Timeline 或同线程恢复展开整份 Thread 档案。"""
    history = owner.get(field)
    if isinstance(history, list) and history:
        return history[-1] if isinstance(history[-1], str) else None
    if field in owner and not isinstance(history, JobHistoryAppend):
        return None
    if isinstance(owner, JobProjection) and field in owner.archives and field not in owner.loaded_histories:
        if owner.latest_thread_reader is None:
            raise ValueError("Latest Thread requires its exact query reader")
        return owner.latest_thread_reader(owner, field)
    return None


def previous_review_artifact(owner: dict[str, Any]) -> dict[str, Any] | None:
    """读取最近旧窗口的一条 Reviewer 记录，包括尚未保存的明确追加。"""
    field = "review_budget_history"
    history = owner.get(field)
    if isinstance(history, list):
        for snapshot in reversed(history):
            budget = snapshot.get("review_budget") if isinstance(snapshot, dict) else None
            artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
            if isinstance(artifacts, list) and artifacts:
                latest = artifacts[-1]
                return deepcopy(latest) if isinstance(latest, dict) else None
    if isinstance(owner, JobProjection) and field in owner.archives and field not in owner.loaded_histories:
        if owner.previous_review_reader is None:
            raise ValueError("Previous Reviewer requires its exact query reader")
        return owner.previous_review_reader(owner)
    return None


def publication_attempt_for_current_ordinal(
    owner: dict[str, Any], related_attempt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read the precise Publication Attempt without creating a writable shadow."""
    ordinal = owner.get("publication_attempts")
    history = owner.get("semantic_attempt_history")
    if isinstance(history, list):
        for attempt in reversed(history):
            if (
                isinstance(attempt, dict)
                and attempt.get("role") == "publication"
                and (type(ordinal) is not int or attempt.get("ordinal") == ordinal)
            ):
                return deepcopy(attempt)
    if related_attempt is not None:
        if (related_attempt.get("role") == "publication"
                and related_attempt.get("status") == "completed"
                and related_attempt.get("ordinal") == ordinal):
            return deepcopy(related_attempt)
        return None
    if (
        isinstance(owner, JobProjection)
        and "semantic_attempt_history" in owner.archives
        and "semantic_attempt_history" not in owner.loaded_histories
    ):
        if owner.publication_reader is None:
            raise ValueError("Publication Attempt requires its exact query reader")
        return owner.publication_reader(owner)
    return None


def restore_work_scopes(
    target: dict[str, Any], source: Mapping[str, Any], *, preserve_owner_references: bool = False
) -> None:
    """消毒与普通容器复制后，只恢复明确领域对象的非持久查询范围。"""

    def job(after: Any, before: Any) -> Any:
        if not isinstance(after, dict) or not isinstance(before, dict):
            return after
        if isinstance(before, JobProjection):
            if isinstance(after, JobProjection):
                after.job_id = before.job_id
                after.revision = before.revision
                after.archives = dict(before.archives)
                after.loaded_histories = set(before.loaded_histories)
                after.history_reader = before.history_reader
                after.previous_review_reader = before.previous_review_reader
                after.latest_thread_reader = before.latest_thread_reader
                after.publication_reader = before.publication_reader
                after.expected_publication_attempt = before.expected_publication_attempt
            elif not (
                preserve_owner_references
                and all(
                    field in before.loaded_histories
                    and isinstance(after.get(field), list)
                    and not isinstance(after.get(field), JobHistoryAppend)
                    for field in before.archives
                )
            ):
                result = before.copy()
                result.clear()
                result.update(after)
                after = result
        for key, value in before.items():
            if isinstance(value, JobHistoryAppend) and isinstance(after.get(key), list):
                after[key] = JobHistoryAppend(after[key], base_bytes=value.base_bytes, base_count=value.base_count)
        previous_budgets = before.get("repair_integration_budgets")
        budgets = after.get("repair_integration_budgets")
        if isinstance(previous_budgets, RepairBudgetProjection) and isinstance(budgets, dict):
            if isinstance(budgets, RepairBudgetProjection):
                budgets.scope = previous_budgets.scope
            else:
                after["repair_integration_budgets"] = RepairBudgetProjection(budgets, scope=previous_budgets.scope)
        if isinstance(after.get("repair_job"), dict):
            after["repair_job"] = job(after["repair_job"], before.get("repair_job"))
        return after

    for field in ("active_ticket_job", "parent_job", "run_acceptance", "run_publication"):
        if field in target:
            target[field] = job(target[field], source.get(field))
    for number, value in target.get("ticket_jobs", {}).items():
        target["ticket_jobs"][number] = job(value, source.get("ticket_jobs", {}).get(number))
    audit = target.get("resume_audit")
    previous = source.get("resume_audit")
    history = previous.get("history") if isinstance(previous, Mapping) else None
    if isinstance(audit, dict) and isinstance(audit.get("history"), list) and isinstance(history, ResumeHistory):
        audit["history"] = ResumeHistory(audit["history"], scope=history.scope)
