"""Full merge messages are physical facts bound to the original intent."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Role, wire_json
from support.offline import Scenario
from support.offline_api import SyntheticGitHub


def _message_agent(scenario: Scenario, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    original = scenario.agent

    def agent(call):
        raw = original(call)
        if call.role == Role.PUBLICATION:
            value = json.loads(raw)
            value["commit_message"] = message
            return wire_json(value)
        return raw

    monkeypatch.setattr(scenario, "agent", agent)


def _merge_record(scenario: Scenario):
    # Audit only records produced through real Core APIs, never prepare a row.
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT * FROM effects WHERE subject=? AND operation='merge' ORDER BY rowid DESC LIMIT 1",
                     (scenario.delivery.key,))
        return dict(row), json.loads(tx.get(row["payload_id"], row["subject"])), (
            None if row["receipt_id"] is None else json.loads(tx.get(row["receipt_id"], row["subject"])))


@pytest.mark.parametrize("repair", [False, True], ids=["first-publication", "updated-after-ci"])
def test_ticket_squash_uses_the_complete_published_pr_not_the_latest_commit(tmp_path, monkeypatch, repair):
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET,
                        checks=("fail", "pass") if repair else ("pass",))
    original = scenario.agent
    publications = []

    def agent(call):
        raw = original(call)
        if call.role == Role.PUBLICATION:
            value = json.loads(raw)
            number = len(publications) + 1
            value.update(commit_message=f"fix: narrow patch {number}",
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
    with scenario.kernel.storage.transaction(write=False) as tx:
        published = json.loads(tx.get(tx.one("SELECT payload_id FROM effects WHERE operation='publication_commit' ORDER BY rowid DESC LIMIT 1")[0], scenario.delivery.key))
        assert published["commit_message"] == publication["commit_message"]
        assert published["title"] == publication["pr_title"]
    assert len(publications) == (2 if repair else 1)
    assert scenario.development_count == (2 if repair else 1)


def test_foreign_pr_body_cannot_be_adopted_as_the_ticket_squash_intent(tmp_path):
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
    for _ in range(80):
        scenario.step()
        if scenario.kernel.status(scenario.delivery.key)["phase"] == "merge":
            break
    else:
        raise AssertionError("The real Ticket never reached the merge boundary")
    status = scenario.kernel.status(scenario.delivery.key)
    pr_number = status["pr_number"]
    before = scenario.port.remote_commit(scenario.delivery.target_ref)
    external = SyntheticGitHub(scenario.port.git, scenario.port.database, "example/project",
        physical=scenario.port.physical, written=lambda *args: None, read_faults=set(), actor="external-maintainer")
    assert external.write(f"repos/example/project/pulls/{pr_number}", "PATCH", {"body": "Unapproved external narrative"}).returncode == 0
    result = scenario.run()
    assert result.kind == "paused"
    row, payload, _receipt = _merge_record(scenario)
    assert "Unapproved external narrative" not in payload["commit_message"]
    assert "Synthetic scenario with recorded evidence 1." in payload["commit_message"]
    assert row["attempts"] == 0
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == before
    assert not any(write["operation"] in {"merge", "completion_comment", "close"} for write in scenario.port.writes())
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "open"


@pytest.mark.parametrize("message,expected", [
    ("feat: no body\r\n", "feat: no body"),
    ("feat: CRLF title\r\n\r\nBody\r\nLast line  \r\n\r\n",
     "feat: CRLF title\n\nBody\r\nLast line  \r\n\r\n"),
], ids=["no-body", "crlf-header"])
def test_publication_commit_keeps_canonical_message_and_squash_keeps_full_pr(
    tmp_path, monkeypatch, message, expected,
):
    original = SyntheticGitHub.write
    requests = []

    def write(self, endpoint, method, fields):
        if endpoint.endswith("/merge"):
            requests.append(dict(fields))
        return original(self, endpoint, method, fields)

    monkeypatch.setattr(SyntheticGitHub, "write", write)
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
    _message_agent(scenario, monkeypatch, message)
    assert scenario.run().kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    title = "feat: synthetic behavior 1"
    body = "Parent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\nSynthetic scenario with recorded evidence 1."
    squash = title + "\n\n" + body
    assert len(requests) == 1
    assert requests[0]["commit_title"] == title and requests[0]["commit_message"] == body
    assert "\n" not in requests[0]["commit_title"] and "\r" not in requests[0]["commit_title"]
    assert payload["commit_message"] == squash and row["attempts"] == 1
    assert receipt["raw"]["commit_message"] == squash + "\n"
    with scenario.kernel.storage.transaction(write=False) as tx:
        attempt = tx.one("SELECT * FROM attempts WHERE subject=? AND role='publication'", (scenario.delivery.key,))
        output = tx.one("SELECT raw_id FROM invocation_outputs WHERE invocation=?", (attempt["current_invocation"],))
        assert json.loads(tx.get(output[0], scenario.delivery.key))["commit_message"] == message
        assert tx.get(attempt["result_id"], scenario.delivery.key) == tx.get(output[0], scenario.delivery.key)
        publication = tx.one("SELECT * FROM effects WHERE operation='publication_commit'")
        assert json.loads(tx.get(publication["payload_id"], scenario.delivery.key))["commit_message"] == expected
        commit = json.loads(tx.get(publication["receipt_id"], scenario.delivery.key))["commit"]
    actual = scenario.port.git.run("cat-file", "-p", commit["sha"]).stdout.partition("\n\n")[2]
    assert actual == (expected if expected.endswith("\n") else expected + "\n")


@pytest.mark.parametrize("fault", ["duplicate-body", "extra-blank-line"])
def test_wrong_squash_message_cannot_complete_or_repeat_the_original_merge(tmp_path, monkeypatch, fault):
    original = SyntheticGitHub.write
    requested = []

    def duplicate_body(self, endpoint, method, body):
        if endpoint.endswith("/merge"):
            requested.append(dict(body))
            details = body["commit_message"]
            body = {**body, "commit_message": details + ("\n\n" + details if fault == "duplicate-body" else "\n\n")}
        return original(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", duplicate_body)
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
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
    if fault == "extra-blank-line":
        assert receipt["raw"]["commit_message"] == payload["commit_message"] + "\n\n"
    assert receipt["commit"]["sha"] == scenario.port.remote_commit(scenario.delivery.target_ref).sha
    before = scenario.port.writes()
    scenario.kernel = Kernel(scenario.kernel.storage.path)
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
def test_original_publication_message_survives_real_git_and_complete_receipt(tmp_path, monkeypatch, message):
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
    _message_agent(scenario, monkeypatch, message)
    assert scenario.run().kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    assert row["status"] == "confirmed" and row["attempts"] == 1 and row["generation"] == 1
    expected = "feat: synthetic behavior 1\n\nParent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\nSynthetic scenario with recorded evidence 1."
    assert payload["method"] == "squash" and payload["commit_message"] == expected
    assert receipt["raw"]["commit_message"] == expected + "\n"
    with scenario.kernel.storage.transaction(write=False) as tx:
        publication = tx.one("SELECT * FROM effects WHERE operation='publication_commit'")
        assert json.loads(tx.get(publication["payload_id"], scenario.delivery.key))["commit_message"] == message
        sha = json.loads(tx.get(publication["receipt_id"], scenario.delivery.key))["commit"]["sha"]
    assert scenario.port.git.run("cat-file", "-p", sha).stdout.partition("\n\n")[2] == message + "\n"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "closed"


def test_missing_full_message_stays_unknown_until_resume_reads_original_merge(tmp_path, monkeypatch):
    original = SyntheticGitHub.object
    missing = [True]

    def read(self, endpoint):
        result = original(self, endpoint)
        if "/git/commits/" in endpoint and missing[0]:
            result.pop("message", None)
        return result

    monkeypatch.setattr(SyntheticGitHub, "object", read)
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
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
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    scenario.kernel.clock = lambda: now[0]
    assert scenario.kernel.control("resume", "read-original-merge", subject=scenario.delivery.key) == "applied"
    assert scenario.run().kind == "complete"
    after, intent, receipt = _merge_record(scenario)
    assert after["id"] == row["id"] and after["attempts"] == 1 and intent == payload
    assert receipt["raw"]["commit_message"] == payload["commit_message"] + "\n"
    assert len([w for w in scenario.port.writes() if w["operation"] == "merge"]) == 1


def test_api_message_without_git_terminator_is_not_a_content_difference(tmp_path, monkeypatch):
    original = SyntheticGitHub.object

    def read(self, endpoint):
        result = original(self, endpoint)
        if "/git/commits/" in endpoint:
            message = result["message"]
            result["message"] = message.removesuffix("\n")
        return result

    monkeypatch.setattr(SyntheticGitHub, "object", read)
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
    assert scenario.run().kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    assert row["attempts"] == 1 and receipt["raw"]["commit_message"] == payload["commit_message"]


def test_ordinary_parent_merge_keeps_its_own_message_policy(tmp_path, monkeypatch):
    original = SyntheticGitHub.write
    methods = []

    def server_merge_message(self, endpoint, method, body):
        if endpoint.endswith("/merge"):
            methods.append(body["merge_method"])
            body = {**body, "commit_title": "Merge reviewed parent", "commit_message": "Server merge details."}
        return original(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", server_merge_message)
    scenario = Scenario(tmp_path / "parent", DeliveryKind.PARENT_ONLY)
    assert scenario.run(approve=True).kind == "complete"
    row, payload, receipt = _merge_record(scenario)
    assert methods == ["merge"] and payload["method"] == "merge" and row["attempts"] == 1
    assert len(receipt["raw"]["parents"]) == 2
    assert receipt["raw"]["commit_message"] != payload["commit_message"] + "\n"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "closed"


@pytest.mark.parametrize("old_message", [False, True], ids=["exact-current", "previous-generation"])
def test_requeued_generation_uses_its_own_original_publication_message(tmp_path, monkeypatch, old_message):
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET)
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
        if old_message and endpoint.endswith("/merge"):
            title, _, details = narratives[0].partition("\n\n")
            body = {**body, "commit_title": title, "commit_message": details}
        return original_write(self, endpoint, method, body)

    monkeypatch.setattr(SyntheticGitHub, "write", service)
    if old_message:
        with pytest.raises(KernelError, match="foreign_merge_receipt"):
            scenario.run()
    else:
        assert scenario.run().kind == "complete"
    assert messages == ['feat: generation 1\n\nOriginal reviewed message.',
                        'feat: generation 2\n\nOriginal reviewed message.']
    row, payload, receipt = _merge_record(scenario)
    assert row["generation"] == 2 and row["attempts"] == 1 and payload["commit_message"] == narratives[1]
    assert receipt["raw"]["commit_message"] == narratives[0 if old_message else 1] + "\n"
    assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == ("open" if old_message else "closed")
    assert len([w for w in scenario.port.writes() if w["operation"] == "merge"]) == 1
