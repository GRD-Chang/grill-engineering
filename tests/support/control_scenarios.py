"""Prepare a published Ticket for native control tests over private real Git.

HostObservation is the original explicit synthetic observation, not an OS Host.
Callers execute and independently assert their own control and readback paths.
"""
from dataclasses import replace
import json
from pathlib import Path

from agent_run.model import Execution, HostObservation, Policy
from agent_run.runtime.executor import _make_ports
from support.core_operations import CoreOperations
from support.offline import GraphScenario
from support.remote_effects import _fixture
from support.requirements_scenarios import independent_source


def prepared_control(tmp_path: Path, monkeypatch, control_kind: str):
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
    return scenario, request, original, state, calls, operations, world
