"""Native Requirements consume synthetic returns through the actual attempt lifecycle."""

import json
import os
from pathlib import Path

import pytest

from support.core_operations import CoreOperations, next_call, prepare_scripted_agent
from agent_run.model import Role
from agent_run.contracts.artifact import parse_artifact, role_schema
from agent_run.errors import ArtifactContractError
from agent_run.runtime.codex import _CodexCapture
from agent_run.model import ParentIssue, Requirements, wire_json
from support.offline import GraphScenario, review_result
from agent_run.config.resources import resolve_resources
from support.worker_facts import worker_fixture, consume_local_result


def publication(body="Recorded validation and user impact.", *, message="feat: change"):
    return dict(result_kind="publication", commit_message=message, pr_title="Change",
                pr_body_markdown=body, human_blockers=None)


def human(blockers):
    return dict(result_kind="human_blocker", commit_message=None, pr_title=None,
                pr_body_markdown=None, human_blockers=blockers)


@pytest.mark.parametrize("message,expected", [
    ("feat: title", "feat: title"),
    (" \tfeat: title \t", "feat: title"),
    ("feat: title\r\n", "feat: title"),
    ("feat: title\n\n", "feat: title"),
    ("feat: title\r\n\r\n正文\r\nNext.  \r\n", "feat: title\n\n正文\r\nNext.  \r\n"),
    ("feat: title\n\nBody ends with spaces  ", "feat: title\n\nBody ends with spaces  "),
    ("feat: title\n\r\nBody", "feat: title\n\nBody"),
])
def test_publication_message_normalizes_only_the_title_separator(message, expected):
    raw = wire_json(publication(message=message))
    result = parse_artifact("publication", raw)
    assert result["commit_message"] == expected
    assert json.loads(raw)["commit_message"] == message


@pytest.mark.parametrize("message", ["Title\nBody", "\n\nNo title", "  \r\n\r\nBody", "Title\rBody"])
def test_publication_message_rejects_a_missing_or_multiline_title(message):
    with pytest.raises(ArtifactContractError, match="commit_message"):
        parse_artifact("publication", wire_json(publication(message=message)))


def returned(kernel, call, raw):
    # Original synthetic process observations, not OS sandbox proof.
    assert kernel.dispatch_agent(call)
    kernel.bind_thread(call, call.reported_thread or "synthetic-contract-" + call.thread_id)
    process = {"pid": 23 + call.output_ordinal, "pgid": 23 + call.output_ordinal,
               "start_token": f"output-{call.output_ordinal}",
               "host_pid": 11, "host_start_token": "synthetic-host"}
    kernel.agent_started(call, process)
    kernel.receive(call, raw)
    kernel.agent_exited(call, process, 0)


def role_fixture(root, role, *, language="en"):
    kernel, operations = worker_fixture(root, language=language)
    request = operations.until(lambda step: step.preparation is not None).preparation
    if role == Role.DEVELOPMENT:
        return kernel, operations, request
    development = prepare_scripted_agent(kernel, request).call
    returned(kernel, development, wire_json(dict(result_kind="development", summary="Original change", human_blockers=None)))
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, development.subject, "candidate")
    consume_local_result(kernel, operations, development.subject, "validation_checkout")
    request = operations.until(lambda step: step.preparation is not None).preparation
    assert request.role == Role.REVIEW
    if role == Role.REVIEW:
        return kernel, operations, request
    review = prepare_scripted_agent(kernel, request).call
    returned(kernel, review, wire_json(review_result("pass")))
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, review.subject, "validation_cleanup")
    consume_local_result(kernel, operations, review.subject, "integrate")
    request = operations.until(lambda step: step.preparation is not None).preparation
    assert request.role == Role.PUBLICATION
    return kernel, operations, request


def test_native_publication_rejects_contract_drift_and_repairs_same_attempt(tmp_path: Path):
    # Text/count/length variants belong to test_artifacts; one rejection proves
    # the real Attempt keeps its original raw and repairs without a new budget.
    value = publication("Fixes #2")
    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Deliver change"), ()))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    first = next_call(scenario, Role.PUBLICATION)
    raw = b"\n " + wire_json(value) + b"\n"
    returned(scenario.kernel, first, raw)
    step = operations.once()
    repair = step.call
    assert repair is not None and repair.mode == "output-repair", step
    assert repair.attempt_id == first.attempt_id and repair.invocation_id == first.invocation_id
    assert repair.thread_id == first.thread_id and repair.deadline_at == first.deadline_at
    assert repair.context["readonly"] is True and "invalid_artifact" in repair.context["contract_error"]
    with scenario.kernel.storage.transaction(write=False) as tx:
        attempt = tx.one("SELECT * FROM attempts WHERE id=?", (first.attempt_id,))
        assert attempt["status"] != "consumed" and attempt["outcome"] is None
        saved = tx.one("SELECT raw_id FROM invocation_outputs WHERE invocation=? AND ordinal=?",
                       (first.invocation_id, first.output_ordinal))[0]
        assert tx.get(saved, first.subject) == raw
        assert tx.one("SELECT source_sequence FROM runs")[0] is not None
        assert not tx.rows("SELECT id FROM effects WHERE operation='publication_commit'")
    valid = b"\n" + wire_json(publication()) + b"\n"
    returned(scenario.kernel, repair, valid)
    consumed = operations.once(agent_capacity=0)
    assert consumed.kind == "progress"
    with scenario.kernel.storage.transaction(write=False) as tx:
        outputs = tx.rows("SELECT raw_id FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (first.invocation_id,))
        assert [tx.get(row[0], first.subject) for row in outputs] == [raw, valid]
        assert tx.one("SELECT status FROM attempts WHERE id=?", (first.attempt_id,))[0] == "consumed"
        assert tx.one("SELECT phase FROM subjects WHERE id=?", (first.subject,))[0] == "publication_commit"


_FAIL_AND_BLOCKED = review_result("fail")
_FAIL_AND_BLOCKED["checks"]["e2e"].update(status="blocked")


@pytest.mark.parametrize(("role", "value", "outcome"), [
    (Role.DEVELOPMENT, dict(result_kind="development", summary="  Changed behavior.  ", human_blockers=None), "development"),
    (Role.DEVELOPMENT, dict(result_kind="human_blocker", summary=None, human_blockers=["  Grant access.  "]), "human_blocker"),
    (Role.REVIEW, review_result("pass"), "pass"),
    (Role.REVIEW, _FAIL_AND_BLOCKED, "fail"),
    (Role.REVIEW, review_result("blocked"), "blocked"),
    (Role.PUBLICATION, publication(), "publication"),
    (Role.PUBLICATION, human(["x" * 2000] * 8), "human_blocker"),
])
def test_native_valid_outputs_keep_wire_raw_and_route_all_roles(tmp_path, role, value, outcome):
    kernel, operations, request = role_fixture(tmp_path / "consumer", role)
    Path(request.context["checkout"]).mkdir(parents=True, exist_ok=True)
    # Inspect the transport's actual owned schema file without launching a
    # process: UID namespaces are unavailable in some execution environments.
    credential = tmp_path / "fixture-hosts.yml"
    credential.write_text("github.com:\n  oauth_token: synthetic-not-a-credential\n")
    credential.chmod(0o600)
    with _CodexCapture(kernel, request, executable="unused-local-codex",
                       environment={"PATH": os.defpath, "HOME": str(tmp_path)},
                       credential_file=credential, hidden_paths=(), hidden_environment=()) as capture:
        assert capture.directory is not None
        descriptor = os.open("schema.json", os.O_RDONLY, dir_fd=capture.directory)
        with os.fdopen(descriptor, "rb") as schema:
            assert json.load(schema) == role_schema(role.value)
    call = kernel.admit_agent(request).call
    assert call is not None
    raw = b"\n " + wire_json(value) + b" \n"
    returned(kernel, call, raw)
    step = operations.once(agent_capacity=0)
    blocked = outcome in {"human_blocker", "blocked"}
    assert (step.kind, step.reason) == (("paused", "human_blocker") if blocked else ("progress", ""))
    with kernel.storage.transaction(write=False) as tx:
        attempt = tx.one("SELECT * FROM attempts WHERE id=?", (call.attempt_id,))
        assert (attempt["status"], attempt["outcome"]) == ("human" if blocked else "consumed", outcome)
        assert tx.get(attempt["result_id"], call.subject) == raw
        assert tx.one("SELECT source_sequence FROM runs")[0] is not None
        if outcome == "fail":
            assert tx.one("SELECT phase FROM subjects WHERE id=?", (call.subject,))[0] == "development"
            assert "human_blocker" not in kernel.status(call.subject)["gates"]


def test_invalid_repair_is_gated_and_both_original_outputs_remain(tmp_path):
    kernel, operations, request = role_fixture(tmp_path / "consumer", Role.PUBLICATION)
    operations.prepare_agents = True
    first = prepare_scripted_agent(kernel, request).call
    rejected = [wire_json(publication(body)) for body in
                ("Resolves #7", "## Completed Tickets\n#2", "Delivery Run: forged-run")]
    call = first
    for raw in rejected[:-1]:
        returned(kernel, call, raw)
        repair = operations.once().call
        assert repair is not None and repair.mode == "output-repair"
        assert repair.invocation_id == first.invocation_id and repair.attempt_id == first.attempt_id
        call = repair
    returned(kernel, call, rejected[-1])
    step = operations.once()
    assert (step.kind, step.reason) == ("paused", "invalid_artifact")
    with kernel.storage.transaction(write=False) as tx:
        outputs = tx.rows("SELECT raw_id FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (first.invocation_id,))
        assert [tx.get(row[0], first.subject) for row in outputs] == rejected
        assert not tx.rows("SELECT id FROM effects WHERE operation='publication_commit'")


def test_frozen_run_accepts_publication_in_the_other_language(tmp_path):
    # Artifact parsing takes only role and bytes, never the frozen UI language.
    # Exercise that connection once after real development/review preparation.
    language, role = "zh", Role.PUBLICATION
    resources = resolve_resources(language)
    kernel, operations, request = role_fixture(tmp_path / "consumer", role, language=language)
    call = prepare_scripted_agent(kernel, request).call
    assert call.context["language"] == language and call.context["_prompt_resources"] == resources
    prose = "Keep this English evidence / API unchanged."
    value = publication(prose, message="feat: " + prose)
    value["pr_title"] = prose
    raw = b"\n" + wire_json(value) + b"\n"
    returned(kernel, call, raw)
    step = operations.once(agent_capacity=0)
    assert step.kind == "progress" and step.call is None
    with kernel.storage.transaction(write=False) as tx:
        attempt = tx.one("SELECT status,result_id FROM attempts WHERE id=?", (call.attempt_id,))
        assert attempt["status"] == "consumed"
        assert tx.get(attempt["result_id"], call.subject) == raw
        assert json.loads(tx.get(attempt["result_id"], call.subject)) == value
        assert len(tx.rows("SELECT id FROM invocations WHERE attempt=?", (call.attempt_id,))) == 1
    assert kernel.status(call.subject)["gates"] == []
