"""Finite managed control and observation scenarios over private real Git.

HostObservation is the original explicit synthetic observation, not an OS Host.
Callers execute and independently assert their own control and readback paths.
"""
from dataclasses import replace
import json
import os

import pytest
from pathlib import Path

from agent_run.model import DeliveryKind, EffectResult, Execution, HostObservation, Issue, ParentIssue, Policy, Requirements
from agent_run.runtime.executor import _make_ports
from agent_run.adapters.remote import RemoteEffects
import agent_run.cli.lifecycle as lifecycle_cli
from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from support.systemd_manager import controlled_manager
from support.core_operations import CoreOperations, prepare_scripted_agent
from support.offline import GraphScenario, Scenario
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


def managed_parent(tmp_path):
    return admit_scenario(Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY), tmp_path)

def admit_scenario(scenario, tmp_path):
    kernel = scenario.kernel
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("synthetic-native-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    return scenario

def sent_workspaces(tmp_path, *, count=2, execute=True):
    numbers = tuple(range(2, 2 + count))
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", numbers), tuple(
        Issue(number, "Ticket", "Original task", "OPEN", frozenset({"ready-for-agent"})) for number in numbers))
    scenario = admit_scenario(GraphScenario(tmp_path / "graph", source, policy=Policy(concurrency=2)), tmp_path)
    runner = scenario.runner()
    physical = runner.ports.execute
    sent = []

    def uncertain(effect, authorize):
        if effect.operation != "workspace":
            return physical(effect, authorize)
        if execute:
            assert physical(effect, authorize).disposition == "target"
        else:
            authorize.prepare()()  # The request was admitted; its transport has not applied it.
        sent.append(effect)
        return EffectResult("unknown")

    runner.ports.execute = uncertain
    for _ in range(16):
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        assert step.call is None
        if len(sent) == count:
            break
    assert len(sent) == count
    runner.ports.execute = lambda *a: pytest.fail("observation cannot issue a new write")
    return scenario, runner, sent

def _admit_host(scenario, tmp_path):
    """Use the admitted manager witness for this synthetic process adapter."""
    admit_scenario(scenario, tmp_path)
    action = scenario.kernel.active_host_action()
    assert action is not None
    usage = scenario.kernel.host_usage(action)
    assert usage is not None and usage.usage_ready and not usage.usage_released
    return {"host_pid": usage.main_pid, "host_start_token": usage.start_token,
            "boot_id": usage.boot_id, "host_cgroup": usage.cgroup, "cgroup": usage.cgroup}

def managed_approval(tmp_path, monkeypatch, *, tickets, language="en", interrupt_development=False, save_development=False):
    transport = tmp_path / "transport"
    transport.mkdir()
    port, git, push, state, calls = _fixture(transport, monkeypatch)
    fake = transport / "bin" / "gh"
    fake.write_text(fake.read_text().replace("'number':17,", "'number':17+len(d['prs']),"))
    graph = {"parent": {"number": 1, "title": "Parent", "body": "Original requirement", "sub_issues": list(tickets)},
             "issues": {str(n): {"number": n, "title": "Ticket", "body": "Ticket requirement", "state": "OPEN",
                                    "labels": ["ready-for-agent"]} for n in tickets}}
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open", "source_graph": graph}))
    defaults = UserDefaultsStore()
    defaults.configure(language=language, profile={"preset": "economy"})
    database, spec, source = lifecycle_cli.prepare_creation(1, "example/project", source_directory=git.root,
        defaults=defaults, remote_url=str(transport / "origin.git"))
    kernel = Kernel.create_task(database, spec, requirements=source)
    manager_root = tmp_path / "manager"
    manager_root.mkdir()
    manager, _, response, manager_calls = controlled_manager(manager_root)
    request = kernel.admit_host("original-host", kind="run", command=("fixture-native-host",),
        environment_carrier=manager_root / "original-carrier", preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    sealed = ""
    if interrupt_development:
        from agent_run.runtime.handoff import prepare_handoff
        from agent_run.runtime.package_lease import default_runner_lock_path, runner_usage_lease
        from agent_run.model import HostObservation
        manager_root.chmod(0o700)
        lock = default_runner_lock_path(dict(os.environ))
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {}, runner_lock=lock, terminal_lease=terminal, ttl_seconds=.5)
            try:
                sealed = json.dumps(handoff.receipt, sort_keys=True, separators=(",", ":"))
                assert kernel.observe_host(request, HostObservation("unknown", request.unit_name,
                    usage_pending=True, evidence=sealed)) == "unknown"
            finally:
                handoff.process.wait(timeout=3)
        assert not Path(f"/proc/{handoff.process.pid}").exists()
    document = json.loads(response.read_text())
    group = next(line[3:] for line in Path(f"/proc/{os.getpid()}/cgroup").read_text().splitlines() if line.startswith("0::"))
    document["fields"].update(Id=request.unit_name, Description=request.description,
        ActiveState="active", SubState="running", MainPID=str(os.getpid()), ControlGroup=group)
    response.write_text(json.dumps(document))
    observed = manager.observe(request)
    assert observed.disposition == "running"
    assert kernel.observe_host(request, replace(observed, usage_ready=True, evidence=sealed)) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    physical = RemoteEffects(Path(spec.repository_root), spec.repository, runner_identity=spec.inputs.runner_identity)
    runner = OperationRunner(kernel, execution, lambda binding: physical)
    # This existing fake Agent only makes files/raw bytes. All source, Git, PR,
    # Completion and operation decisions above/below remain production paths.
    agents = Scenario(tmp_path / "agents", DeliveryKind.PARENT_ONLY)
    agents.kernel, agents.port.git = kernel, physical.git
    subject = "final:1" if tickets else "parent:1"
    for _ in range(180):
        step = prepare_scripted_agent(kernel, runner.run_once())
        if step.call is not None:
            if interrupt_development:
                if save_development:
                    runner.authorize_agent(step.call)
                    kernel.receive(step.call, agents.agent(step.call))
                break
            runner.authorize_agent(step.call)
            kernel.receive(step.call, agents.agent(step.call))
        if kernel.source_status()["final_subject"] == subject or not tickets:
            status = kernel.status(subject)
            assert not status["gates"], (step, status["gates"])
            if status["phase"] == "approval":
                break
    else:
        raise AssertionError("The managed Run never reached its real approval")
    monkeypatch.setattr(lifecycle_cli, "_active_runner", lambda: True)
    import agent_run.adapters.systemd as adapter
    monkeypatch.setattr(adapter, "SystemdHost", lambda **kwargs: manager)
    return kernel, runner, physical, state, calls, request, subject, source
