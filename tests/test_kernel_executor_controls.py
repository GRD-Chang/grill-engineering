"""The native executor consumes original control effects and real source reads."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.model import Policy
from agent_run.runtime.executor import _make_ports
from support.core_operations import CoreOperations
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from support.remote_effects import _fixture
from test_kernel_requirements import independent_source


@pytest.mark.parametrize("control_kind", ["requeue", "abandon"])
def test_native_executor_consumes_the_original_control_action_with_fresh_source_and_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control_kind: str,
) -> None:
    scenario = GraphScenario(tmp_path / "scenario", independent_source(), policy=Policy(concurrency=2))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-offline")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    for _ in range(100):
        scenario.step()
        if kernel.status(subject)["phase"] == "checks":
            break
    else:
        raise AssertionError("The original delivery never published its PR")
    prior = scenario.port.pull_request_by_ref(scenario.kernel.delivery(subject).source_ref, scenario.kernel.delivery(subject).target_ref)
    assert prior is not None and prior.actor is not None
    original = kernel.delivery(subject)
    if control_kind == "requeue":
        scenario.requirements = replace(scenario.requirements, issues=tuple(
            replace(issue, body=issue.body + "\nClarified requirement") if issue.number == original.issue else issue
            for issue in scenario.requirements.issues))
    transport = tmp_path / "transport"
    transport.mkdir()
    _port, _git, _push, state, calls = _fixture(transport, monkeypatch)
    fake = transport / "bin" / "gh"
    fake.write_text(fake.read_text().replace("example/project", prior.repository).replace("publisher", prior.actor))
    scenario.port.git.run("remote", "add", "offline-remote", str(transport / "origin.git"))
    anchors = [prior.head.sha + ":" + prior.source_ref, prior.base.sha + ":" + prior.target_ref,
               scenario.initial.sha + ":refs/heads/main"]
    for resource in kernel.generation_resources():
        if resource.source_ref != prior.source_ref:
            anchors.append(resource.head_sha + ":" + resource.source_ref)
    scenario.port.git.run("push", "--force", "offline-remote", *dict.fromkeys(anchors))
    source = scenario.requirements
    graph = {"parent": {"number": source.parent.number, "title": source.parent.title,
                         "body": source.parent.body, "sub_issues": list(source.parent.sub_issue_numbers)},
             "issues": {str(issue.number): {"number": issue.number, "title": issue.title, "body": issue.body,
                                             "state": issue.state, "labels": sorted(issue.labels)} for issue in source.issues}}
    raw_pr = {"number": prior.number, "state": "open", "title": prior.title, "body": prior.body,
              "user": {"login": prior.actor}, "merged": False, "merge_commit_sha": None,
              "head": {"sha": prior.head.sha, "ref": prior.source_ref.removeprefix("refs/heads/"),
                       "repo": {"full_name": prior.repository}},
              "base": {"sha": prior.base.sha, "ref": prior.target_ref.removeprefix("refs/heads/"),
                       "repo": {"full_name": prior.repository}}}
    state.write_text(json.dumps({"prs": [raw_pr], "comments": [], "events": [], "issue_state": "open",
                                 "source_graph": graph, "lose_operation": "pr"}))
    operations = CoreOperations(kernel, scenario.execution, _make_ports)
    world = operations.runner.observe(subject)
    if control_kind == "requeue":
        replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                              checkout=str(Path(original.checkout).with_name("replacement-g2")))
        assert kernel.control_host(request, "requeue", "replace-original", subject=subject,
                                   world=world, replacement=replacement) == "pending"
        for _ in range(30):
            step = operations.once()
            assert step.call is None and step.kind == "progress"
            if kernel.status(subject)["generation"] == 2:
                break
        else:
            raise AssertionError("The original requeue Action did not apply its replacement")
        assert kernel.status(subject)["generation"] == 2
    else:
        resources = kernel.generation_resources()
        inspections = operations.runner.ports.inspect(resources)
        assert kernel.control_host(request, "abandon", "abandon-original", resource_inspections=inspections) == "pending"
        for _ in range(30):
            step = operations.once()
            assert step.call is None and step.kind in {"progress", "complete"}
            if step.kind == "complete":
                break
        else:
            raise AssertionError("The original Action's finite effects did not complete")
        assert all(not Path(resource.checkout).exists() for resource in resources)
    actual = json.loads(state.read_text())
    assert actual["prs"][0]["state"] == "closed"
    assert len(actual["comments"]) == len(actual["events"]) == 1
    observed_calls = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("POST" in call for call in observed_calls) == sum("PATCH" in call for call in observed_calls) == 1
    assert any(call[:2] == ["api", "graphql"] for call in observed_calls)


def test_native_executor_abandons_before_the_remote_run_branch_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path / "scenario", independent_source())
    kernel, subject = scenario.kernel, scenario.delivery.key
    # Admit the managed Host without advancing any business effect. The native
    # transport below contains only main. Unallocated resources are named but
    # absent, as at formal creation before the first workspace effect.
    kernel.control("stop", "retire-offline")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    transport = tmp_path / "transport"
    transport.mkdir()
    _port, _git, _push, state, calls = _fixture(transport, monkeypatch)
    _git.run("push", "origin", ":refs/heads/delivery")
    scenario.port.git.run("remote", "add", "offline-remote", str(transport / "origin.git"))
    scenario.port.git.run("push", "--force", "offline-remote", scenario.initial.sha + ":refs/heads/main")
    source = scenario.requirements
    graph = {"parent": {"number": source.parent.number, "title": source.parent.title,
                         "body": source.parent.body, "sub_issues": list(source.parent.sub_issue_numbers)},
             "issues": {str(issue.number): {"number": issue.number, "title": issue.title, "body": issue.body,
                                             "state": issue.state, "labels": sorted(issue.labels)} for issue in source.issues}}
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open", "source_graph": graph}))
    operations = CoreOperations(kernel, execution, _make_ports)
    world = operations.runner.observe(subject)
    assert world.target is None and world.source is None and world.pr is None
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects")
    resources = kernel.generation_resources()
    assert resources and all(not Path(resource.checkout).exists() for resource in resources)
    inspections = operations.runner.ports.inspect(resources)
    assert kernel.control_host(request, "abandon", "abandon-before-run-branch",
                               resource_inspections=inspections) == "pending"
    for _ in range(30):
        step = operations.once()
        assert step.call is None and step.kind in {"progress", "complete"}
        if step.kind == "complete":
            break
    else:
        raise AssertionError("The original Abandon Action did not finish its local resource cleanup")
    assert all(not Path(resource.checkout).exists() for resource in resources)
    observed_calls = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not any("--method" in call for call in observed_calls)
    assert operations.runner.ports.git.run("ls-remote", "--refs", "offline-remote", scenario.delivery.target_ref).stdout == ""
    assert operations.runner.ports.git.run("ls-remote", "--refs", "offline-remote", "refs/heads/main").stdout.split()[0] == scenario.initial.sha
