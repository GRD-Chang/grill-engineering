from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.publication_evidence import publication_is_current
from agent_run.git import GitRepository
from agent_run.change_delivery_publication_agent import publication
from agent_run.semantic_attempt import canonical_fingerprint, allocate_semantic_attempt
from agent_run.run_publication import RunPublicationEngine
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run


@pytest.mark.parametrize("pending", [False, True])
def test_publication_reuse_survives_reload_but_rejects_changed_input(pending: bool) -> None:
    artifact = {"commit_message": "change", "pr_title": "title", "pr_body_markdown": "body"}
    boundary = {
        "candidate_sha": "candidate", "candidate_tree": "tree", "base_sha": "base",
        "effective_revision": "requirements", "ticket_graph_revision": "graph",
        "ticket_completion_records_fingerprint": "completion",
        "expected_merge_tree": "merged-tree",
    }
    attempt = {
        "role": "publication", "currentness_boundary_fingerprint": canonical_fingerprint(boundary),
        "outcome": "publication_artifact", "publication": artifact,
    }
    owner = {"pending_semantic_attempt": attempt} if pending else {"semantic_attempt_history": [attempt]}
    reloaded = deepcopy(owner)
    assert publication_is_current(reloaded, deepcopy(artifact), deepcopy(boundary))
    for key in boundary:
        changed = {**boundary, key: "changed"}
        assert not publication_is_current(reloaded, artifact, changed)


def test_completed_publication_must_match_result_and_cannot_fall_back_to_new_record() -> None:
    artifact = {"commit_message": "change", "pr_title": "title", "pr_body_markdown": "body"}
    owner = {"semantic_attempt_history": [{
        "role": "publication", "outcome": "publication_artifact",
        "currentness_boundary_fingerprint": canonical_fingerprint({"base_sha": "old"}), "publication": artifact,
    }]}
    assert not publication_is_current(owner, artifact, {"base_sha": "new"}, recorded_boundary={"base_sha": "new"})
    assert not publication_is_current(owner, {**artifact, "pr_title": "other"}, {"base_sha": "old"})


def test_publication_without_attempt_requires_complete_existing_boundary() -> None:
    artifact = {"pr_title": "title"}
    boundary = {"base_sha": "base", "candidate_sha": "candidate"}
    assert not publication_is_current({}, artifact, boundary)
    assert not publication_is_current({}, artifact, boundary, recorded_boundary={"base_sha": "base"})
    assert publication_is_current({}, artifact, boundary, recorded_boundary=deepcopy(boundary))


def test_reloaded_final_publication_rejects_result_outside_its_attempt(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])
    engine.advance(run_id, allow_agent=True)
    saved = states.load_current_run(run_id)
    assert saved is not None
    saved["run_publication"]["artifact"]["pr_title"] = "Different delivery narrative"
    states.save_run(run_id, saved)
    restored = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    restored.advance(run_id)
    rejected = states.load_current_run(run_id)
    assert rejected is not None
    assert rejected["run_publication"]["phase"] == "pending"
    assert "artifact" not in rejected["run_publication"]
    assert rejected["run_acceptance"]["phase"] == "accepted"
    assert len(agents.requests) == 1
    assert publisher.data["delivery"].get("pull_requests", []) == []
    restored.advance(run_id, allow_agent=True)
    restored.advance(run_id)
    ready = states.load_current_run(run_id)
    assert ready is not None
    assert ready["run_publication"]["phase"] == "ready_for_approval"
    assert ready["run_acceptance"]["phase"] == "accepted"
    assert len(agents.requests) == 2


def test_stale_publication_preserves_unknown_commit_intent(tmp_path: Path) -> None:
    intent = {"expected_sha": "commit", "raw_commit": "original bytes"}
    job = {
        "phase": "accepted", "publication_commit_intent": deepcopy(intent),
        "pending_publication_result": {"pr_title": "original narrative"},
    }
    pauses: list[str] = []
    stage = SimpleNamespace(
        git=GitRepository(tmp_path),
        _publication_is_current=lambda *_args: False,
        _block=lambda _state, _job, code, _message: pauses.append(code),
        save=lambda state: state,
    )
    publication(stage, {}, job, tmp_path)
    assert pauses == ["publication_intent_mismatch"]
    assert job["publication_commit_intent"] == intent
    assert job["pending_publication_result"] == {"pr_title": "original narrative"}


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("delivery_is_stale", [False, True])
def test_stale_publication_observes_original_commit_before_retiring_evidence(
    git_repo: Path, present: bool, delivery_is_stale: bool,
) -> None:
    import subprocess

    git = GitRepository(git_repo)
    original_head = git.resolve("HEAD")
    raw = subprocess.run(
        ["git", "cat-file", "commit", original_head], cwd=git_repo,
        text=True, capture_output=True, check=True,
    ).stdout
    if not present:
        raw += "unique uninstalled publication\n"
    expected = subprocess.run(
        ["git", "hash-object", "-t", "commit", "--stdin"], cwd=git_repo,
        input=raw, text=True, capture_output=True, check=True,
    ).stdout.strip()
    intent = {"expected_sha": expected, "raw_commit": raw}
    job = {
        "phase": "accepted", "publication_commit_intent": deepcopy(intent),
        "pending_publication_result": {"pr_title": "original narrative"},
        "candidate_sha": original_head, "acceptance_artifact": {"original": "review"},
        "repair_source": "required_checks", "ci_evidence": {"log": "RAW_CI_FAILURE"},
    }
    current_delivery = {key: deepcopy(job[key]) for key in (
        "candidate_sha", "acceptance_artifact", "repair_source", "ci_evidence",
    )}
    attempt = allocate_semantic_attempt(
        job, role="publication", work_subject="ticket:1", generation=1,
        currentness_boundary={"candidate_sha": original_head}, ordinal=1,
    )
    invalidations: list[str] = []
    stage = SimpleNamespace(
        git=git, _publication_is_current=lambda *_args: not delivery_is_stale,
        _invocation_boundary=lambda _job: {"candidate_sha": "different-publication-boundary"},
        _invalidate_stale=lambda *_args: invalidations.append("invalidated"),
        _block=lambda *_args: pytest.fail("the original object is observable"),
        save=lambda state: state,
    )
    publication(stage, {}, job, git_repo)
    assert invalidations == (["invalidated"] if delivery_is_stale else [])
    assert all(job[key] == value for key, value in current_delivery.items())
    assert "publication_commit_intent" not in job
    history = job["semantic_attempt_history"][-1]
    assert history["attempt_id"] == attempt["attempt_id"]
    assert history["outcome"] == "currentness_invalidated"
    assert history["publication_commit_observation"] == {
        "expected_sha": expected, "result": "present" if present else "absent",
    }
    assert git.resolve("HEAD") == original_head
    if not present:
        observed = subprocess.run(
            ["git", "cat-file", "--batch-check"], cwd=git_repo,
            input=expected + "\n", text=True, capture_output=True, check=True,
        ).stdout.strip()
        assert observed == f"{expected} missing"



def test_new_final_narrative_updates_existing_pr_without_repeating_review(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    ready = engine.publish(run_id)
    original_review = deepcopy(ready["run_acceptance"]["acceptance_record"])
    number = ready["run_publication"]["pr_number"]
    ready["run_publication"]["artifact"]["pr_title"] = "Unbound result"
    states.save_run(run_id, ready)

    class UpdatedNarrative(RunPublicationAgents):
        def run_publication(self, request: dict) -> dict:
            artifact = super().run_publication(request)
            artifact["pr_title"] = "Updated bound narrative"
            return artifact

    updated = UpdatedNarrative()
    result = RunPublicationEngine(
        git=git, states=states, agents=updated, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(run_id)
    assert result["status"] == "run_approval_pending"
    assert result["run_acceptance"]["acceptance_record"] == original_review
    assert result["run_publication"]["pr_number"] == number
    assert len(updated.requests) == 1
    assert publisher.read_run_pr_narrative(number)["title"] == "Updated bound narrative"
