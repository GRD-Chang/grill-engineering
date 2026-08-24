from __future__ import annotations

"""Best-effort retirement of Delivery Run branches after durable closeout."""

from pathlib import Path
from typing import Any

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import (
    DirtyManagedCheckoutError,
    GitRepository,
    is_managed_delivery_branch,
)
from agent_run.state import StateStore


MAX_AUTOMATIC_ATTEMPTS = 3


class DeliveryCleanupEngine:
    """Persist cleanup failures without changing the completed Delivery Run."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        github: GitHubPublisher | None = None,
    ) -> None:
        self.git = git
        self.states = states
        self.github = github

    def complete_ticket(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        if job.get("phase") != "completed" or not isinstance(
            job.get("integrated_sha"), str
        ):
            return state
        self._schedule(
            state,
            kind="ticket",
            branch=self._string(job, "ticket_branch"),
            checkout=self._worktree(state, f"ticket-{self._integer(job, 'ticket_number')}")
        )
        return self._attempt(state)

    def complete_run_repair(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        if job.get("phase") != "completed" or not isinstance(
            job.get("integrated_sha"), str
        ):
            return state
        self._schedule(
            state,
            kind="run_repair",
            branch=self._string(job, "repair_branch"),
            checkout=self._worktree(state, "run-repair"),
        )
        return self._attempt(state)

    def complete_run_repairs(
        self, state: dict[str, Any], jobs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Retire every Job identity used by one completed Repair Cycle."""

        for job in jobs:
            if job.get("phase") != "completed" or not isinstance(
                job.get("integrated_sha"), str
            ):
                continue
            self._schedule(
                state,
                kind="run_repair",
                branch=self._string(job, "repair_branch"),
                checkout=self._worktree(state, "run-repair"),
            )
        return self._attempt(state)

    def complete_parent(self, state: dict[str, Any]) -> dict[str, Any]:
        job = state.get("parent_job")
        if not isinstance(job, dict) or job.get("phase") != "completed":
            return state
        self._schedule(
            state,
            kind="parent",
            branch=self._string(job, "parent_branch"),
            checkout=self._worktree(state, "parent"),
        )
        return self._attempt(state)

    def complete_final_run(self, state: dict[str, Any]) -> dict[str, Any]:
        publication = state.get("run_publication")
        if (
            not isinstance(publication, dict)
            or publication.get("phase") != "merged"
            or publication.get("parent_closed") is not True
        ):
            return state
        self._schedule(
            state,
            kind="run",
            branch=self._string(state, "run_branch"),
            checkout=self._worktree(state, "run-publication"),
        )
        return self._attempt(state)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Retry cleanup only; no development or delivery lifecycle is advanced."""
        with self.states.locked():
            state = self.states.load_current_run(run_id)
            if state is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            if state.get("status") == "abandoned":
                return state
            self._schedule_completed_items(state)
            if not isinstance(state.get("delivery_cleanup"), dict):
                return state
            return self._attempt(state)

    def preserve_dirty_checkout(
        self,
        state: dict[str, Any],
        *,
        kind: str,
        branch: str,
        checkout: Path,
        reason: str,
    ) -> None:
        """Expose a stale dirty checkout without attempting to delete it."""

        self._schedule(
            state,
            kind=kind,
            branch=branch,
            checkout=checkout,
        )
        cleanup = self._cleanup(state)
        item = self._mapping(self._mapping(cleanup, "items"), branch)
        item.update(
            {
                "status": "cleanup_pending",
                "last_error": reason,
                "recovery_kind": "stale_dirty_checkout",
            }
        )
        cleanup.update({"status": "cleanup_pending", "last_error": reason})

    def _schedule_completed_items(self, state: dict[str, Any]) -> None:
        jobs = state.get("ticket_jobs", {})
        if isinstance(jobs, dict):
            for job in jobs.values():
                if (
                    isinstance(job, dict)
                    and job.get("phase") == "completed"
                    and isinstance(job.get("integrated_sha"), str)
                ):
                    self._schedule(
                        state,
                        kind="ticket",
                        branch=self._string(job, "ticket_branch"),
                        checkout=self._worktree(
                            state,
                            f"ticket-{self._integer(job, 'ticket_number')}",
                        ),
                    )
        run = state.get("run_acceptance")
        if isinstance(run, dict):
            repairs = run.get("completed_repair_jobs", [])
            if isinstance(repairs, list):
                for job in repairs:
                    if (
                        isinstance(job, dict)
                        and job.get("phase") == "completed"
                        and isinstance(job.get("integrated_sha"), str)
                    ):
                        self._schedule(
                            state,
                            kind="run_repair",
                            branch=self._string(job, "repair_branch"),
                            checkout=self._worktree(state, "run-repair"),
                        )
        if state.get("delivery_type") == "parent_only":
            job = state.get("parent_job")
            if isinstance(job, dict) and job.get("phase") == "completed":
                self._schedule(
                    state,
                    kind="parent",
                    branch=self._string(job, "parent_branch"),
                    checkout=self._worktree(state, "parent"),
                )
        elif state.get("delivery_type") == "ticket_run":
            publication = state.get("run_publication")
            if (
                isinstance(publication, dict)
                and publication.get("phase") == "merged"
                and publication.get("parent_closed") is True
            ):
                self._schedule(
                    state,
                    kind="run",
                    branch=self._string(state, "run_branch"),
                    checkout=self._worktree(state, "run-publication"),
                )

    def _schedule(
        self,
        state: dict[str, Any],
        *,
        kind: str,
        branch: str,
        checkout: Path,
    ) -> None:
        cleanup = self._cleanup(state)
        items = self._mapping(cleanup, "items")
        items.setdefault(
            branch,
            {
                "kind": kind,
                "branch": branch,
                "checkout": str(checkout),
                "attempts": 0,
                "status": "pending",
            },
        )

    def _attempt(self, state: dict[str, Any]) -> dict[str, Any]:
        cleanup = self._cleanup(state)
        items = self._mapping(cleanup, "items")
        last_error: str | None = None
        for item in items.values():
            if not isinstance(item, dict) or item.get("status") == "completed":
                continue
            branch = self._string(item, "branch")
            checkout = Path(self._string(item, "checkout"))
            if not is_managed_delivery_branch(branch):
                error = f"refusing to delete unmanaged branch {branch!r}"
                item.update({"status": "cleanup_pending", "last_error": error})
                last_error = error
                continue
            for _ in range(MAX_AUTOMATIC_ATTEMPTS):
                item["attempts"] = self._integer(item, "attempts") + 1
                try:
                    self.git.require_clean_managed_checkout(checkout)
                    self.git.remove_worktree(checkout)
                    self.git.delete_managed_delivery_branch(branch)
                    remote_delete = (
                        getattr(self.github, "delete_managed_branch", None)
                        if self.github is not None
                        else None
                    )
                    if callable(remote_delete):
                        remote_delete(branch)
                except (OSError, RuntimeError) as error:
                    item.update(
                        {"status": "cleanup_pending", "last_error": str(error)}
                    )
                    last_error = str(error)
                else:
                    item["status"] = "completed"
                    item.pop("last_error", None)
                    break
        pending = [
            item
            for item in items.values()
            if isinstance(item, dict) and item.get("status") != "completed"
        ]
        cleanup["status"] = "cleanup_pending" if pending else "completed"
        if pending:
            cleanup["last_error"] = last_error or "cleanup is pending"
        else:
            cleanup.pop("last_error", None)
        self.states.save_run(str(state["run_id"]), state)
        return state

    def _cleanup(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("delivery_cleanup")
        if isinstance(existing, dict):
            return existing
        cleanup = {"status": "pending", "items": {}}
        state["delivery_cleanup"] = cleanup
        return cleanup

    def _worktree(self, state: dict[str, Any], name: str) -> Path:
        return self.states.root / "worktrees" / self._string(state, "run_id") / name

    @staticmethod
    def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        return value

    @staticmethod
    def _string(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _integer(data: dict[str, Any], key: str) -> int:
        value = data.get(key)
        if not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value


def require_clean_run_worktrees(
    git: GitRepository, states: StateStore, run_id: str
) -> None:
    root = states.root / "worktrees" / run_id
    if not root.exists():
        return
    dirty: list[tuple[Path, str]] = []
    for checkout in sorted(root.iterdir()):
        if not git.is_managed_development_checkout(checkout):
            continue
        reason = git.managed_checkout_dirty_reason(checkout)
        if reason is not None:
            dirty.append((checkout.resolve(), reason))
    if not dirty:
        return
    details = "; ".join(f"{path}: {reason}" for path, reason in dirty)
    raise DirtyManagedCheckoutError(
        f"preserved dirty Managed Development Checkout(s): {details}; "
        f"inspect and retain or commit the work, then run agent-run resume {run_id}; "
        f"to irreversibly discard all Run worktrees, use "
        f"agent-run abandon {run_id} --discard-worktree"
    )


def remove_run_worktrees(
    git: GitRepository,
    states: StateStore,
    run_id: str,
    *,
    discard_worktree: bool = False,
) -> None:
    root = states.root / "worktrees" / run_id
    if not discard_worktree:
        require_clean_run_worktrees(git, states, run_id)
    if root.exists():
        for checkout in root.iterdir():
            git.remove_worktree(checkout, discard_worktree=discard_worktree)
        try:
            root.rmdir()
        except OSError:
            pass
    git.prune_worktrees()
