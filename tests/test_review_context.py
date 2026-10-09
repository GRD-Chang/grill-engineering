"""低成本组合规则；真实交付与保存重载由 delivery/Run 回归覆盖。"""
from copy import deepcopy
import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.prompt_resources import builtin_resources
from agent_run.review_context import prepare_review_context, review_context_is_current
from support.run_artifacts import _repair_artifact
from support.prompt_capture import _capture_public_prompt


def _commit(repo: Path, content: str) -> str:
    (repo / "result.txt").write_text(content)
    subprocess.run(["git", "add", "result.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", content], cwd=repo, check=True, capture_output=True)
    return GitRepository(repo).resolve("HEAD")


@pytest.mark.parametrize("scope", ["ticket", "parent_only", "run"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_full_scope_and_repair_focus_survive_new_thread(
    git_repo: Path, scope: str, language: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    c1 = _commit(git_repo, "candidate-one")
    c2 = _commit(git_repo, "candidate-two")
    state = {"parent": {"revision": "parent", "body": "FULL_REQUIREMENTS"},
             "ticket_graph": {"revision": "graph"}}
    job = {"base_sha": base, "candidate_sha": c1, "effective_revision": "revision"}
    identity = {"reviewed_base_sha": base, "reviewed_candidate_sha": c1,
                "reviewed_candidate_tree": git.resolve(f"{c1}^{{tree}}")}
    request = {"acceptance_scope": scope, "current_review_identity": identity,
               "effective_revision": "revision", "parent": deepcopy(state["parent"]),
               "run_review_facts": {"ticket_completion_records": []}}
    first = prepare_review_context(request, {}, job, git, state)
    assert first["full_range"] == first["focus_range"] == {"base_sha": base, "candidate_sha": c1}
    job["semantic_attempt_history"] = [{"outcome": "acceptance_artifact", "review_context": first}]
    job["candidate_sha"] = c2
    artifact = _repair_artifact()
    second_request = {**request, "current_review_identity": {**identity, "reviewed_candidate_sha": c2},
                      "previous_review_identity": identity, "previous_acceptance_artifact": artifact,
                      "_prompt_resources": builtin_resources(language), "_invocation_mode": "new-thread"}
    attempt = {}
    second = prepare_review_context(second_request, attempt, job, git, state)
    assert second["full_range"] == {"base_sha": base, "candidate_sha": c2}
    assert second["focus_range"] == {"base_sha": c1, "candidate_sha": c2}
    assert second["has_complete_baseline"]
    prompt = _capture_public_prompt(
        git_repo.parent, monkeypatch, "review", second_request, name="review-context-new-thread",
    )
    assert base in prompt and c1 in prompt and c2 in prompt
    assert "full_range" in prompt and "focus_range" in prompt
    assert prompt.count(artifact["checks"]["e2e"]["findings"][0]) == 1
    # Human messages and execution continuation retain the original evidence.
    resumed_attempt = deepcopy(attempt)
    resumed = {**second_request, "_invocation_mode": "resume"}
    resumed.pop("previous_acceptance_artifact")
    assert prepare_review_context(resumed, resumed_attempt, job, git, state) == second
    resumed["thread_id"] = "public-prompt-test"
    resumed_prompt = _capture_public_prompt(
        git_repo.parent, monkeypatch, "review", resumed, name="review-context-resume",
    )
    assert artifact["checks"]["e2e"]["findings"][0] in resumed_prompt
    job["pending_semantic_attempt"] = resumed_attempt
    assert review_context_is_current(job)
    job["base_sha"] = c1
    assert not review_context_is_current(job)


@pytest.mark.parametrize("change", ["requirements", "base", "missing", "blocked"])
def test_inapplicable_prior_review_does_not_authorize_incremental_scope(
    git_repo: Path, change: str,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve("HEAD")
    candidate = _commit(git_repo, "candidate")
    state = {"parent": {"revision": "parent"}, "ticket_graph": {"revision": "graph"}}
    job = {"base_sha": base, "candidate_sha": candidate}
    request = {"acceptance_scope": "parent_only", "parent": deepcopy(state["parent"]),
               "current_review_identity": {"reviewed_base_sha": base, "reviewed_candidate_sha": candidate}}
    first = prepare_review_context(request, {}, job, git, state)
    job["semantic_attempt_history"] = [{"outcome": "acceptance_artifact", "review_context": first}]
    request["previous_review_identity"] = deepcopy(request["current_review_identity"])
    request["previous_acceptance_artifact"] = _repair_artifact()
    if change == "requirements":
        request["parent"]["revision"] = "new-parent"
    elif change == "base":
        job["base_sha"] = candidate
        request["current_review_identity"]["reviewed_base_sha"] = candidate
    elif change == "missing":
        job["semantic_attempt_history"] = []
    else:
        lane = request["previous_acceptance_artifact"]["checks"]["spec"]
        lane.update(status="blocked", findings=[])
    context = prepare_review_context(request, {}, job, git, state)
    assert not context["has_complete_baseline"]
    assert context["full_range"] == context["focus_range"]
