"""A recorded acceptance cannot certify a different candidate in local Status."""
from dataclasses import replace
from io import StringIO
import json

import pytest

from agent_run.cli.main import main
from agent_run.cli.render import render
from agent_run.model import DeliveryKind
from agent_run.queries import run as query
from support.environment import durable_snapshot
from support.offline import Scenario


def test_local_status_does_not_present_the_previous_pass_as_current_after_target_changes(tmp_path, capsys):
    scenario = Scenario(tmp_path / "run", DeliveryKind.PARENT_ONLY)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert scenario.run().kind == "approval"
    document = query.read(kernel.storage.path, "status", storage=kernel.storage)
    original = next(row["review"] for row in document.data["subjects"] if row["subject"] == scenario.delivery.key)
    assert original["outcome"] == "pass"
    arguments = ["status", scenario.spec.run_id, "--state-dir", str(kernel.storage.path), "--plain"]
    assert main(arguments) == 0
    assert "已记录验收结论: 通过" in capsys.readouterr().out
    scenario.port.external_remote_append(scenario.delivery.target_ref,
        {"new-target.txt": "Independent target update\n"}, "status-target-update")
    assert kernel.control("approve", "update-target", subject=subject, world=scenario.world()) == "updating"
    before = durable_snapshot(tmp_path)
    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert "已记录验收结论: 通过" not in output
    document = query.read(kernel.storage.path, "status", storage=kernel.storage)
    job = next(row for row in document.data["subjects"] if row["subject"] == subject)
    assert job["review"] == original  # Original audit evidence stays available.
    assert job["review_state"] == "stale"
    assert durable_snapshot(tmp_path) == before


def _assert_current_view(scenario, state, capsys, *, hidden_finding=None):
    before = durable_snapshot(scenario.kernel.storage.path.parent)
    document = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
    job = next(item for item in document.data["subjects"] if item["subject"] == scenario.delivery.key)
    assert job["review_state"] == state
    for language in ("en", "zh"):
        for style in ("plain", "rich"):
            output = StringIO()
            render(replace(document, language=language), style=style, details=False, stream=output)
            value = output.getvalue()
            if state in {"stale", "pending", "not_recorded"}:
                assert "Known synthetic acceptance evidence" not in value
                assert "Recorded review: Passed" not in value and "已记录验收结论: 通过" not in value
            if hidden_finding:
                assert hidden_finding not in value
    assert main(["status", scenario.spec.run_id, "--state-dir", str(scenario.kernel.storage.path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    actual = next(item for item in data["subjects"] if item["subject"] == scenario.delivery.key)
    assert actual["review"] == job["review"] and actual["review_state"] == state
    assert durable_snapshot(scenario.kernel.storage.path.parent) == before
    return job


@pytest.mark.parametrize("kind", [DeliveryKind.PARENT_ONLY, DeliveryKind.TICKET])
def test_local_status_binds_failure_pending_and_pass_to_their_original_candidate(tmp_path, capsys, kind):
    scenario = Scenario(tmp_path / "run", kind, reviews=["fail", "pass"])
    _assert_current_view(scenario, "not_recorded", capsys)
    for _ in range(100):
        scenario.step()
        document = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
        job = next(row for row in document.data["subjects"] if row["subject"] == scenario.delivery.key)
        if job["review"] and job["review"]["outcome"] == "fail":
            break
    else:
        pytest.fail("Normal Review failure was not consumed")
    original = job["review"]
    original_candidate = job["candidate_sha"]
    finding = original["artifact"]["checks"]["spec"]["findings"][0]
    assert _assert_current_view(scenario, "request_changes", capsys)["review"] == original
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "development"
    observed = set()
    for _ in range(100):
        step = scenario.step()
        document = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
        job = next(row for row in document.data["subjects"] if row["subject"] == scenario.delivery.key)
        if job["candidate_sha"] != original_candidate:
            assert job["review"] == original  # An unconsumed return does not replace the original audit record.
            observed.add(job["review_state"])
            _assert_current_view(scenario, job["review_state"], capsys, hidden_finding=finding)
        if step.call is not None and step.call.role == "review":
            assert job["review_state"] == "pending"
            assert step.call.context["candidate_sha"] != original_candidate
            break
    else:
        pytest.fail("Repair did not produce a fresh Review return")
    assert observed == {"stale", "pending"}
    assert scenario.run().kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    current = _assert_current_view(scenario, "pass", capsys, hidden_finding=finding)
    assert current["review"]["attempt_id"] != original["attempt_id"]
    assert current["review"]["outcome"] == "pass"
    history = query.read(scenario.kernel.storage.path, "history", storage=scenario.kernel.storage).data["history"]
    previous = next(item for item in history["attempts"] if item["id"] == original["attempt_id"])
    assert previous["result"] == original["artifact"]


def test_source_revision_change_does_not_reuse_the_recorded_pass(tmp_path, capsys):
    scenario = Scenario(tmp_path / "run", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    original = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage).data["subjects"][0]["review"]
    scenario.requirements = replace(scenario.requirements,
        parent=replace(scenario.requirements.parent, body="A changed requirement needing a new review"))
    scenario.kernel.refresh(lambda: scenario.requirements)
    job = _assert_current_view(scenario, "stale", capsys)
    assert job["review"] == original
