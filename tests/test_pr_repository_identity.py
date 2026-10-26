"""GitHub's repository spelling must not change exact PR-boundary authority."""
from dataclasses import asdict, replace
import json

import pytest

from agent_run.contracts.pr_observation import pr_target
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, DeliveryKind, PullRequest
from support.offline import Scenario
from support.offline_api import SyntheticGitHub
from support.control_scenarios import _parent_requeue, _retirement
from support.notification import _project


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


@pytest.mark.parametrize("receipt", [False, True], ids=["observation", "original-receipt"])
def test_core_pr_identity_accepts_equivalent_repository_spelling(observed_pr, receipt):
    run = {"repository": "example-owner/greeting-fixture"}
    resource = {"source_ref": observed_pr.source_ref, "target_ref": observed_pr.target_ref,
                "pr_number": observed_pr.number}
    assert Kernel._pr_identity(run, resource, asdict(observed_pr) if receipt else observed_pr)


@pytest.mark.parametrize("receipt", [False, True], ids=["observation", "original-receipt"])
@pytest.mark.parametrize("changes", [
    {"repository": "Other-Owner/Greeting-Fixture"},
    {"head_repository": "Example-Owner/another-repository"},
    {"head_repository": None},
    {"source_ref": "refs/heads/agent-run/sample-run/Ticket-2"},
    {"target_ref": "refs/heads/agent-run/sample-run/Run"},
    {"number": 4},
])
def test_core_pr_identity_rejects_foreign_bindings(observed_pr, receipt, changes):
    run = {"repository": "example-owner/greeting-fixture"}
    resource = {"source_ref": observed_pr.source_ref, "target_ref": observed_pr.target_ref,
                "pr_number": observed_pr.number}
    pr = replace(observed_pr, **changes)
    assert not Kernel._pr_identity(run, resource, asdict(pr) if receipt else pr)


@pytest.fixture
def github_repository_spelling(monkeypatch):
    read_pr = SyntheticGitHub._pr

    def github_spelling(api, number):
        value = read_pr(api, number)
        for side in ("base", "head"):
            value[side]["repo"]["full_name"] = "Example/Repository"
        return value

    monkeypatch.setattr(SyntheticGitHub, "_pr", github_spelling)


def test_case_equivalent_pr_survives_publication_retirement_and_merge_receipts(
    tmp_path, monkeypatch, github_repository_spelling,
):
    scenario, old, replacement, world = _parent_requeue(tmp_path, monkeypatch)
    assert old.pr.repository == old.pr.head_repository == "Example/Repository"
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert kernel.control("requeue", "case-equivalent-requeue", subject=subject,
                          replacement=replacement, world=world) == "pending"
    for _ in range(8):
        if kernel.status(subject)["generation"] == 2:
            break
        assert scenario.runner().run_once().kind == "progress"
    assert kernel.status(subject)["generation"] == 2
    row, payload, receipt = _retirement(scenario)
    assert row["status"] == "confirmed"
    assert payload["prior_pr"]["repository"] == receipt["pr"]["repository"] == "Example/Repository"
    assert scenario.run().kind == "approval"
    kernel.control("approve", "approve-case-equivalent-pr", subject=subject, world=scenario.world())
    assert scenario.run().kind == "complete"
    with kernel.storage.transaction(write=False) as tx:
        effects = tx.all("SELECT operation,status,receipt_id FROM effects WHERE operation IN ('pr_write','merge','retire_pr')")
        assert {e["operation"] for e in effects} == {"pr_write", "merge", "retire_pr"}
        for effect in effects:
            assert effect["status"] == "confirmed"
            saved = json.loads(tx.get(effect["receipt_id"], subject))
            assert saved["pr"]["repository"] == saved["pr"]["head_repository"] == "Example/Repository"


def test_pr_created_notification_preserves_github_repository_spelling(tmp_path, github_repository_spelling):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    created = [e for e in _project(scenario.kernel, "detailed") if e["kind"] == "pr_created"]
    assert len(created) == 1
    assert created[0]["url"].startswith("https://github.com/Example/Repository/pull/")


@pytest.mark.parametrize("fault", ["head_sha", "actor"])
def test_mixed_case_publication_receipt_still_rejects_wrong_sha_or_actor(tmp_path, monkeypatch, fault):
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    execute = scenario.port.execute

    def changed_receipt(effect, authorize):
        result = execute(effect, authorize)
        if effect.operation == "pr_write" and result is not None and result.pr is not None:
            pr = replace(result.pr, repository="Example/Repository", head_repository="Example/Repository")
            pr = (replace(pr, actor=pr.actor.swapcase()) if fault == "actor" else
                  replace(pr, head=replace(pr.head, sha="different-head")))
            return replace(result, pr=pr)
        return result

    monkeypatch.setattr(scenario.port, "execute", changed_receipt)
    with pytest.raises(KernelError, match="foreign_pr_receipt"):
        scenario.run()
    assert not any(w["operation"] in {"merge", "close"} for w in scenario.writes())


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
