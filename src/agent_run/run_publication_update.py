"""Reconcile Final PR snapshots before handing an obsolete approval to updating."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_run.external_supervision import ensure_supervision_window, wait_for_github_convergence
from agent_run.final_approval_operation import record_approval_update
from agent_run.git import GitError
from agent_run.github import GitHubReadError
from agent_run.parent_integration import ParentHistoryRewritten
from agent_run.run_currentness import ticket_completion_records

if TYPE_CHECKING:
    from agent_run.run_publication_shared import RunPublicationShared


def reconcile_publication_update(
    owner: RunPublicationShared, state: dict[str, Any], live: dict[str, Any],
) -> bool:
    """Return false after a durable update/pause; true keeps the exact old PR."""
    publication = state["run_publication"]
    record = publication.get("record")
    if not isinstance(record, dict):
        return True
    if (
        record["parent_revision"] != state["parent"]["revision"]
        or record["ticket_graph_revision"] != state["ticket_graph"]["revision"]
        or record["ticket_completion_records"] != ticket_completion_records(state)
    ):
        _pause(owner, state, "run_requirements_changed",
               "Final Run requirements or completed Ticket facts changed")
        return False
    branch = str(state["run_branch"])
    # Identity and merged-result checks precede any ref synchronization. In
    # particular a foreign/closed PR must not authorize work on its branch.
    owner._require_final_pr_identity(
        live=live, run_head=str(live.get("head_sha")), branch=branch,
        repository=str(state["repository"]), expected_base_sha=None,
    )
    if live.get("state") != "OPEN":
        raise GitHubReadError(
            "final_run_pr_not_open", "Final Run PR is not open at the approved publication boundary",
        )
    source, target = owner.github.live_branch_heads(branch, owner.default_branch)
    if source is None:
        _pause(owner, state, "remote_source_missing", "Run source ref is missing")
        return False
    if live.get("head_sha") != source or live.get("base_sha") != target:
        raise GitHubReadError(
            "github_pr_head_observation_pending", "Final Run PR and remote branch heads have not converged",
        )
    previous_head, previous_base = str(record["run_head_sha"]), str(record["default_head_sha"])
    owner.default_head_sha = target
    if source == previous_head and target == previous_base:
        return True
    try:
        for kind, ref, previous, observed in (
            ("source", branch, previous_head, source),
            ("target", owner.default_branch, previous_base, target),
        ):
            owner.git.fetch_remote_commit(ref, observed)
            if not owner.git.is_ancestor(previous, observed):
                raise ParentHistoryRewritten(kind, previous, observed)
    except (ParentHistoryRewritten, GitError) as error:
        if isinstance(publication.get("merge_intent"), dict):
            wait_for_merge_readback(owner, state, str(error))
            return False
        code = (
            "integration_history_rewritten"
            if isinstance(error, ParentHistoryRewritten) else "run_integration_failed"
        )
        _pause(owner, state, code, str(error))
        return False
    if (
        isinstance(publication.get("merge_intent"), dict)
        and owner.git.is_ancestor(previous_head, target)
    ):
        # The target already contains the approved Run. OPEN can lag a
        # successful normal merge, even when both ref SHAs agree. Retain the
        # exact intent until the PR result converges; never review our merge.
        wait_for_merge_readback(
            owner, state, "Default branch contains the approved Run; awaiting final PR merge readback",
        )
        return False
    if isinstance(publication.get("approval_grant"), dict):
        record_approval_update(
            publication, previous_head=previous_head, previous_base=previous_base,
            source=source, target=target,
        )
    # The observed append-only target does not contain the approved Run.
    # Retire the obsolete merge intent together with its grant before updating.
    publication.pop("merge_intent", None)
    owner._invalidate_for_fresh_acceptance(state)
    return False


def wait_for_merge_readback(
    owner: RunPublicationShared, state: dict[str, Any], message: str,
) -> dict[str, Any]:
    state["run_publication"]["phase"] = "waiting_external"
    wait_for_github_convergence(
        state, code="merge_outcome_unknown", message=message,
        waiting_for="Final Run merge/readback reconciliation",
    )
    ensure_supervision_window(state)
    return owner._save(state)


def _pause(owner: RunPublicationShared, state: dict[str, Any], code: str, message: str) -> None:
    publication = state["run_publication"]
    publication.pop("approval_grant", None)
    publication.update(phase="ready_for_human", blocked_reason=code)
    state.update(
        status="ready_for_human", terminal_kind="waiting_human",
        diagnostics=[{"code": code, "message": message}],
    )
    owner._save(state)
