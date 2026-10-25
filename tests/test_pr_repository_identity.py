"""GitHub's repository spelling must not change exact PR-boundary authority."""
from dataclasses import replace

import pytest

from agent_run.contracts.pr_observation import pr_target
from agent_run.model import Commit, PullRequest


@pytest.fixture
def observed_pr() -> PullRequest:
    # Real GitHub observation shape; repository and run identifiers are sanitized.
    return PullRequest(
        number=3,
        repository="Example-Owner/Greeting-Fixture",
        head_repository="Example-Owner/Greeting-Fixture",
        source_ref="refs/heads/agent-run/sample-run/ticket-2",
        target_ref="refs/heads/agent-run/sample-run/run",
        head=Commit("4c3819b4c9ae483d449748bf057caf23ef85eba5", "206f513104da8c84df056a58b40268ca0d5d0f6c"),
        base=Commit("bec5a82e50fcf398ba5ead9451b0f653134e257c", "fb8740032e67ed571fcd10b34c5a10a940c8bcda"),
        title="Disposable greeting fixture", body="External Codex simulation only.",
        actor="Example-Owner",
    )


def target(pr: PullRequest, original: PullRequest) -> Commit | None:
    return pr_target(pr, repository="example-owner/greeting-fixture",
                     pr_number=3, source_ref=original.source_ref, target_ref=original.target_ref,
                     source=original.head, target=original.base, ancestry=())


@pytest.mark.parametrize("base_name,head_name", [
    ("Example-Owner", "Example-Owner"), ("example-owner", "EXAMPLE-OWNER"), ("EXAMPLE-OWNER", "example-owner"),
])
def test_case_equivalent_head_and_base_repositories_match(observed_pr, base_name, head_name):
    suffix = "/greeting-fixture"
    pr = replace(observed_pr, repository=base_name + suffix, head_repository=head_name + suffix.upper())
    assert target(pr, observed_pr) == observed_pr.base


@pytest.mark.parametrize("changes", [
    {"repository": "Other-Owner/Greeting-Fixture"},
    {"repository": "Example-Owner/another-repository"},
    {"head_repository": "Fork-Owner/Greeting-Fixture"},
    {"head_repository": None},
    {"head_repository": "Example-Owner/../Greeting-Fixture"},
    {"head_repository": "Example-Owner/.."},
    {"source_ref": "refs/heads/Agent-run/sample-run/ticket-2"},
    {"target_ref": "refs/heads/agent-run/sample-run/Run"},
    {"head": Commit("4C3819B4C9AE483D449748BF057CAF23EF85EBA5", "206f513104da8c84df056a58b40268ca0d5d0f6c")},
    {"head": Commit("4c3819b4c9ae483d449748bf057caf23ef85eba5", "other-tree")},
    {"base": Commit("unproven-base", "other-tree")},
    {"state": "open"},
    {"number": 4},
])
def test_repository_equivalence_does_not_relax_other_pr_boundaries(observed_pr, changes):
    assert target(replace(observed_pr, **changes), observed_pr) is None
