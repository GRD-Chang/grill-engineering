"""Full merge messages are physical facts bound to the original intent."""
from support.effect_boundary import _merge_record
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.core.kernel import Kernel
from agent_run.contracts.artifact import commit_message_matches
from agent_run.contracts.write_authority import WriteAuthority
from support.remote_effects import publication
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Role, wire_json
from support.run_scoped_kernel import domain_scenario
from support.offline_api import SyntheticGitHub


def test_ticket_squash_uses_the_complete_published_pr_not_the_latest_commit(tmp_path, monkeypatch, domain_scenario):
    scenario = domain_scenario(tmp_path / "ticket", DeliveryKind.TICKET,
                        checks=("fail", "pass"))
    original = scenario.agent
    publications = []

    def agent(call):
        raw = original(call)
        if call.role == Role.PUBLICATION:
            value = json.loads(raw)
            number = len(publications) + 1
            value.update(commit_message=f"fix: narrow patch {number}\r\n\r\nBody\r\nLast line  \r\n\r\n",
                         pr_title=f"feat(ticket): 完整交付 {number}",
                         pr_body_markdown="## What Problem This Solves\r\n完整任务。\r\n\r\n## Evidence\r\nReviewed implementation " + str(number))
            publications.append(dict(value))
            return wire_json(value)
        return raw

    monkeypatch.setattr(scenario, "agent", agent)
    assert scenario.run().kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    publication = publications[-1]
    expected_body = "Parent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\n" + publication["pr_body_markdown"]
    expected = publication["pr_title"] + "\n\n" + expected_body
    assert payload["commit_message"] == expected
    assert receipt["raw"]["commit_message"] == expected + "\n"
    assert row["status"] == "confirmed" and row["attempts"] == 1
    assert scenario.port.git.run("show", "-s", "--format=%B", receipt["commit"]["sha"]).stdout == expected + "\n\n"
    # The same journey checks both the original and CI-updated publication;
    # each owns its raw result, canonical intent and physical Git message.
    commits = []
    with scenario.kernel.storage.transaction(write=False) as tx:
        scope = (scenario.execution.run_id, scenario.delivery.key)
        attempts = tx.rows("SELECT * FROM attempts WHERE run_id=? AND subject=? AND role='publication' ORDER BY rowid", scope)
        effects = tx.rows("SELECT * FROM effects WHERE run_id=? AND subject=? AND operation='publication_commit' ORDER BY rowid", scope)
        assert len(attempts) == len(effects) == len(publications) == 2
        for number, (attempt, effect, published_result) in enumerate(zip(attempts, effects, publications, strict=True), 1):
            output = tx.one("SELECT raw_id FROM invocation_outputs WHERE run_id=? AND invocation=?",
                            (scenario.execution.run_id, attempt["current_invocation"]))
            raw = tx.get(output[0], scenario.delivery.key)
            assert raw == wire_json(published_result)
            assert tx.get(attempt["result_id"], scenario.delivery.key) == raw
            published = json.loads(tx.get(effect["payload_id"], scenario.delivery.key))
            canonical = f"fix: narrow patch {number}\n\nBody\r\nLast line  \r\n\r\n"
            assert published["commit_message"] == canonical
            assert published["title"] == published_result["pr_title"]
            assert effect["status"] == "confirmed"
            commit = json.loads(tx.get(effect["receipt_id"], scenario.delivery.key))["commit"]
            commits.append((commit["sha"], canonical.encode("utf-8")))
    for sha, canonical_bytes in commits:
        physical = scenario.port.git.run("cat-file", "-p", sha).stdout.encode("utf-8")
        assert physical.partition(b"\n\n")[2] == canonical_bytes
    assert scenario.development_count == 2


def test_wrong_squash_message_cannot_complete_or_repeat_the_original_merge(monkeypatch, ticket_before_pr):
    original = SyntheticGitHub.write
    requested = []

    def duplicate_body(self, endpoint, method, body):
        if endpoint.endswith("/merge"):
            requested.append(dict(body))
            details = body["commit_message"]
            body = {**body, "commit_message": details + "\n\n" + details}
        return original(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", duplicate_body)
    scenario = ticket_before_pr
    with pytest.raises(KernelError, match="foreign_merge_receipt"):
        scenario.run()
    status = scenario.kernel.status(scenario.delivery.key)
    assert status["generation"] == 1 and status["phase"] == "merge"
    pending = status["pending_write"]
    assert pending is not None and pending["operation"] == "merge" and pending["status"] == "observed"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "open"
    assert len(requested) == 1
    assert not any(w["operation"] in {"completion_comment", "close"} for w in scenario.port.writes())
    row, payload, receipt = _merge_record(scenario)
    assert row["attempts"] == 1 and row["generation"] == status["generation"]
    assert receipt["raw"]["write_returncode"] == 0
    assert receipt["raw"]["commit_message"] != payload["commit_message"] + "\n"
    assert receipt["commit"]["sha"] == scenario.port.remote_commit(scenario.delivery.target_ref).sha
    before = scenario.port.writes()
    scenario.kernel = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    with pytest.raises(KernelError, match="foreign_merge_receipt"):
        scenario.step()
    assert scenario.kernel.status(scenario.delivery.key)["pending_write"] == pending
    assert _merge_record(scenario) == (row, payload, receipt)
    assert scenario.port.writes() == before
    assert len(requested) == 1


@pytest.mark.parametrize("message", [
    'feat: one line only',
    'feat: 中文 "quotes"\n\nFirst paragraph.\n\nSecond paragraph.',
    'feat: preserve body whitespace\n\nTrailing spaces remain here  \n\n  Indented line.',
    'feat: retain body line endings\n\nFirst body line\r\nSecond body line',
])
def test_publication_adapter_preserves_message_bytes(git_repo, tmp_path, message):
    git, local, effect = publication(git_repo, tmp_path)
    effect = replace(effect, payload={**effect.payload, "commit_message": message})
    witness = local.prepare_publication(effect)
    effect = replace(effect, payload={**effect.payload, "publication_commit_intent": witness})
    result = local.execute(effect, WriteAuthority(lambda: lambda: None))
    assert result.disposition == "target" and result.commit is not None
    assert git.run("cat-file", "-p", result.commit.sha).stdout.partition("\n\n")[2] == message + "\n"


@pytest.mark.parametrize("expected,actual,matches", [
    ("Title\n\nBody", "Title\n\nBody", True),
    ("Title\n\nBody", "Title\n\nBody\n", True),
    ("Title\n\nBody", "Title\n\nBody\n\n", False),
    ("Title\n\nBody", "Title\n\nBody\n\nBody", False),
    ("Title\n\nBody\n", "Title\n\nBody\n", True),
    ("Title\n\nBody\n", "Title\n\nBody\n\n", False),
    ("Title\n\nBody", None, False),
])
def test_merge_message_receipt_accepts_only_an_optional_git_terminator(expected, actual, matches):
    assert commit_message_matches(expected, actual) is matches


def test_missing_full_message_stays_unknown_until_resume_reads_original_merge(monkeypatch, ticket_before_pr):
    original = SyntheticGitHub.object
    missing = [True]

    def read(self, endpoint):
        result = original(self, endpoint)
        if "/git/commits/" in endpoint and missing[0]:
            result.pop("message", None)
        return result

    monkeypatch.setattr(SyntheticGitHub, "object", read)
    scenario = ticket_before_pr
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    step = scenario.run()
    assert step.kind == "paused" and step.reason == "unknown"
    assert scenario.kernel.status(scenario.delivery.key)["gates"] == ["write_result_unknown"]
    row, payload, receipt = _merge_record(scenario)
    assert row["status"] == "sent" and row["attempts"] == 1 and receipt is None
    assert len([w for w in scenario.port.writes() if w["operation"] == "merge"]) == 1
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "open"
    missing[0] = False
    scenario.kernel = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    scenario.kernel.clock = lambda: now[0]
    assert scenario.kernel.control("resume", "read-original-merge", subject=scenario.delivery.key) == "applied"
    assert scenario.run().kind == "complete"
    after, intent, receipt = _merge_record(scenario)
    assert after["id"] == row["id"] and after["attempts"] == 1 and intent == payload
    assert receipt["raw"]["commit_message"] == payload["commit_message"] + "\n"
    assert len([w for w in scenario.port.writes() if w["operation"] == "merge"]) == 1


def test_ordinary_parent_merge_keeps_its_own_message_policy(monkeypatch, parent_at_approval):
    original = SyntheticGitHub.write
    methods = []

    def server_merge_message(self, endpoint, method, body):
        if endpoint.endswith("/merge"):
            methods.append(body["merge_method"])
            body = {**body, "commit_title": "Merge reviewed parent", "commit_message": "Server merge details."}
        return original(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", server_merge_message)
    scenario = parent_at_approval
    assert scenario.run(approve=True).kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    assert methods == ["merge"] and payload["method"] == "merge" and row["attempts"] == 1
    assert len(receipt["raw"]["parents"]) == 2
    assert receipt["raw"]["commit_message"] != payload["commit_message"] + "\n"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "closed"


def test_requeued_generation_rejects_the_previous_publication_message(tmp_path, monkeypatch, domain_scenario):
    scenario = domain_scenario(tmp_path / "ticket", DeliveryKind.TICKET)
    messages = []
    narratives = []
    original_agent = scenario.agent

    def agent(call):
        raw = original_agent(call)
        if call.role == Role.PUBLICATION:
            value = json.loads(raw)
            value["commit_message"] = f'feat: generation {call.context["generation"]}\n\nOriginal reviewed message.'
            value["pr_title"] = f'feat: complete generation {call.context["generation"]}'
            value["pr_body_markdown"] = f'Complete reviewed requirement for generation {call.context["generation"]}.'
            messages.append(value["commit_message"])
            narratives.append(value["pr_title"] + "\n\nParent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\n" + value["pr_body_markdown"])
            return wire_json(value)
        return raw

    monkeypatch.setattr(scenario, "agent", agent)
    for _ in range(50):
        scenario.step()
        if scenario.kernel.status(scenario.delivery.key)["phase"] == "publication_commit":
            break
    else:
        raise AssertionError("Original publication was never recorded")
    assert messages == ['feat: generation 1\n\nOriginal reviewed message.']
    source = scenario.requirements
    scenario.requirements = replace(source, issues=tuple(replace(issue, body=issue.body + "\nNew requirement")
        for issue in source.issues))
    world = scenario.world()
    old = scenario.delivery
    replacement = replace(old, source_ref=old.source_ref + "-g2",
        checkout=str(Path(old.checkout).with_name("ticket-g2")), base=world.target)
    assert scenario.kernel.requeue(old.key, "accept-current-generation", replacement, world) == "applied"
    scenario.delivery = scenario.kernel.delivery(old.key)
    assert scenario.kernel.status(old.key)["generation"] == 2
    original_write = SyntheticGitHub.write

    def service(self, endpoint, method, body):
        if endpoint.endswith("/merge"):
            title, _, details = narratives[0].partition("\n\n")
            body = {**body, "commit_title": title, "commit_message": details}
        return original_write(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", service)
    with pytest.raises(KernelError, match="foreign_merge_receipt"):
        scenario.run()
    assert messages == ['feat: generation 1\n\nOriginal reviewed message.',
                        'feat: generation 2\n\nOriginal reviewed message.']
    row, payload, receipt = _merge_record(scenario)
    assert row["generation"] == 2 and row["attempts"] == 1 and payload["commit_message"] == narratives[1]
    assert receipt["raw"]["commit_message"] == narratives[0] + "\n"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "open"
    assert len([w for w in scenario.port.writes() if w["operation"] == "merge"]) == 1
