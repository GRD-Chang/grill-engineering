"""Read-only checks before accepting recovery of a managed work scene."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.git import GitError, GitRepository
from agent_run.parent_integration import (
    PARENT_INTEGRATION_COMMIT_MESSAGE, parent_conflict_commit_boundary,
)
from agent_run.resume_intent import is_ticket_execution_recovery
from agent_run.resume_work import resume_message_owner
from agent_run.state_errors import MissingRunMaterialsError


def require_resume_checkout(
    state: dict[str, Any], states_root: Path, git: GitRepository,
    *, ticket_number: int | None = None,
) -> None:
    """Do not silently replace an interrupted Agent's missing delivery tree.

    A fresh job has no scene to lose. Validation checkouts are disposable;
    completed jobs and independent Ticket pauses are not this resume's work.
    Manual reconstruction is subsequently checked by the normal delivery gates.
    """
    if ticket_number is None and is_ticket_execution_recovery(state, state.get("status")):
        for number in state.get("frontier", []):
            job = state.get("ticket_jobs", {}).get(str(number))
            if (not isinstance(job, dict) or job.get("phase") in {"blocked", "publication_pending"}
                    or str(number) in state.get("ticket_pause_requests", {})):
                continue
            require_change_checkout(state, job, f"ticket:{number}", states_root, git)
        return
    owner = resume_message_owner(state, ticket_number)
    if owner is None:
        return
    subject, job, _generation = owner
    require_change_checkout(state, job, subject, states_root, git)


def require_change_checkout(
    state: dict[str, Any], job: dict[str, Any], subject: str,
    states_root: Path, git: GitRepository,
) -> None:
    """Recheck one lane immediately before it prepares an existing scene."""
    if subject.startswith("ticket:"):
        name, branch_key = f"ticket-{job['ticket_number']}", "ticket_branch"
    elif subject.startswith("parent-only:"):
        name, branch_key = "parent", "parent_branch"
    elif subject.startswith("run-repair:"):
        name, branch_key = "run-repair", "repair_branch"
    elif subject.startswith("run-acceptance:"):
        # Review checkouts are disposable, unfinished integration work is not.
        if not isinstance(job.get("run_integration_conflict"), dict):
            return
        name, branch_key = "run-integration", "run_branch"
    else:
        return
    phase = job.get("phase")
    if phase in {"completed", "merged", "waiting_checks", "waiting_merge", "merging", "ready_for_approval"}:
        return
    # Only unfinished Development needs unreconstructable worktree materials.
    # Review, publication and CI can rebuild their checkout from saved commits.
    pending = job.get("pending_semantic_attempt")
    pending_development = (isinstance(pending, dict) and pending.get("status") == "pending"
                           and pending.get("role") == "development")
    continuation = (job.get("execution_resume_phase") or job.get("pause_resume_phase")
                    or job.get("human_blocker_phase") or phase)
    scene_key = next((key for key in (
        "integration_conflict", "parent_integration_conflict", "run_repair_integration_conflict",
        "run_integration_conflict",
    ) if isinstance(job.get(key), dict)), None)
    scene = job[scene_key] if scene_key is not None else None
    # "repairing" may denote a new, not-yet-started Development after a
    # committed Candidate's review or CI. Its predecessor has no unsaved work.
    if (not pending_development and not job.get("pending_attempt") and scene is None
            and (job.get("candidate_sha") or job.get("publication_sha"))):
        return
    needs_scene = pending_development or scene is not None or continuation in {
        "developing", "repairing", "committing_candidate",
    }
    if not needs_scene:
        return
    branch = state.get(branch_key) if name == "run-integration" else job.get(branch_key)
    if not branch and not job.get("managed_checkout_head") and not pending_development:
        # A budget owner may exist before a concrete repair Job has started.
        # An immutable failed publication is not evidence of a lost worktree.
        return
    head = (job.get("managed_checkout_head") if pending_development else
            job.get("publication_sha") or job.get("candidate_sha") or job.get("managed_checkout_head"))
    if name == "run-integration" and scene is not None:
        try:
            head, _merge_head = parent_conflict_commit_boundary(scene)
        except (KeyError, TypeError, ValueError) as error:
            raise MissingRunMaterialsError(
                f"Run {state['run_id']} / {subject}: {scene_key} 的集成提交边界缺损：{error}；"
                "请从可信状态备份补足原集成阶段及提交边界，再使用同一 agent-run resume 重新核验。"
            ) from error
    if not isinstance(head, str) or not head:
        if pending_development:
            raise MissingRunMaterialsError(
                f"Run {state['run_id']} / {subject}: pending Development 缺少 managed_checkout_head；"
                "请从可信状态备份补足原 Attempt 的准确提交边界，不从当前分支猜测；"
                "补足后使用同一 agent-run resume 重新核验。"
            )
        return
    checkout = states_root / "worktrees" / str(state["run_id"]) / name
    if checkout.exists() or checkout.is_symlink():
        # A resumed Development must start at its saved boundary, not at a
        # same-named branch that a manual reconstruction happened to advance.
        # Do this before calling the Worker so integrity recovery cannot erase
        # the operator's reconstructed materials as Agent-owned mutations.
        if pending_development or name == "run-integration":
            expected = scene.get("head_sha", head) if scene is not None else head
            expected_merge = scene.get("merge_head_sha") if scene is not None else None
            parent_scene = scene_key in {
                "parent_integration_conflict", "run_repair_integration_conflict", "run_integration_conflict",
            }
            if parent_scene and scene is not None:
                try:
                    expected, expected_merge = parent_conflict_commit_boundary(scene)
                except (KeyError, TypeError, ValueError) as error:
                    raise MissingRunMaterialsError(
                        f"Run {state['run_id']} / {subject}: {scene_key} 的集成提交边界缺损：{error}；"
                        "请从可信状态备份补足原集成阶段及提交边界，再使用同一 agent-run resume 重新核验。"
                    ) from error
            failure = ""
            try:
                observed = git.checkout_head(checkout)
                boundary_matches = observed == expected
                if boundary_matches and name == "run-integration":
                    boundary_matches = git.checkout_merge_head(checkout) == expected_merge
                if not boundary_matches and scene is not None:
                    boundary_matches = git.recovered_integration_commit_matches(
                        checkout, expected_head_sha=str(expected),
                        expected_merge_sha=expected_merge,
                        message=(PARENT_INTEGRATION_COMMIT_MESSAGE if parent_scene else
                                 f"chore(ticket-{job['ticket_number']}): resolve integration conflict"),
                    )
                if not boundary_matches and continuation == "committing_candidate":
                    # The program may have committed its Candidate before the
                    # state save crashed. Only its existing exact intent can
                    # prove this boundary; a manual same-branch commit cannot.
                    prefix = ("run-repair" if name == "run-repair" else
                              f"ticket-{job['ticket_number']}" if name.startswith("ticket-") else "ticket-0")
                    if name == "run-repair" and job.get("repair_mode") == "merge_resolution":
                        boundary_matches = (
                            git.managed_checkout_dirty_reason(checkout) is None
                            and git.merge_candidate_matches_intent(
                                checkout, checkout_head=observed,
                                run_head_sha=str(job["base_sha"]),
                                default_head_sha=str(job["default_base_sha"]),
                                expected_message=f"chore(run-repair): merge-resolution candidate {job.get('pending_attempt')}",
                                candidate_intent=job.get("candidate_commit_intent"),
                            )
                        )
                    else:
                        boundary_matches = (
                            git.managed_checkout_dirty_reason(checkout) is None
                            and git.candidate_commit_matches_intent(
                                checkout, observed_head=observed, expected_head=expected,
                                expected_subject=f"chore({prefix}): candidate {job.get('pending_attempt')}",
                                candidate_intent=job.get("candidate_commit_intent"),
                            )
                        )
                valid = (boundary_matches and git.ticket_checkout_matches(checkout, str(branch))
                         and git.checkout_belongs_to_repository(checkout))
            except GitError as error:
                valid = False
                failure = f"对账失败：{error}；"
            if not valid:
                raise MissingRunMaterialsError(
                    f"Run {state['run_id']} / {subject}: managed checkout 恢复身份或 HEAD 不符：{checkout}；"
                    f"要求原受管仓库、branch={branch}, head={expected}, merge_head={expected_merge}；{failure}"
                    "请保全人工补足文件并恢复准确工作边界，不能强制 reset/clean 丢弃材料；"
                    "补足后使用同一 agent-run resume 重新核验。"
                )
        return
    try:
        available = git.resolve(f"{head}^{{commit}}") == head
    except GitError:
        available = False
    raise MissingRunMaterialsError(
        f"Run {state['run_id']} / {subject}: 缺少 managed checkout {checkout}；"
        f"branch={branch}, head={head}（{'可取得' if available else '提交对象也缺失'}）；"
        "请从可信材料补建原分支的工作区，恢复尚未提交的成果及适用的集成现场；"
        "准确提交不足以证明未提交材料已恢复。保留原状态、历史和工作额度，"
        "不要猜测验收、CI、文案或批准；补足后使用同一 agent-run resume 重新核验并继续。"
    )
