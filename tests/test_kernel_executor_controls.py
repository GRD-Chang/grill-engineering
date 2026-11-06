"""The native executor consumes original control effects and real source reads."""
import json
from pathlib import Path

import pytest

from agent_run.runtime.executor import _make_ports
from support.core_operations import CoreOperations
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from support.remote_effects import _fixture
from support.requirements_scenarios import independent_source


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
