"""Native Agent continuation fixtures shared by lifecycle contract tests.

All boundaries are reached through real Core allocation and process events;
ReadOnlyPorts cannot execute physical effects.
"""

from support.repository_fixture import create_kernel
from pathlib import Path
import json
from dataclasses import replace

from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec, Issue, ParentIssue, Requirements
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import DeliveryFacts
from support.core_operations import CoreOperations, ReadOnlyPorts
from support.offline import fixture_inputs
from agent_run.runtime.executor import _make_ports
from support.offline import GraphScenario
from support.remote_effects import _fixture

def ticket_spec() -> RunSpec:
    base = Commit("base", "base-tree")
    return RunSpec(
        "offline-run", "example/repository", 1, "parent-v1", "graph-v1",
        (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                  "refs/heads/ticket-2", "refs/heads/run"),),
        Policy(ticket_rounds=1),
    )


def _native_spec(root: Path):
    """Explicit creation inputs and source facts, with no physical write adapter."""
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)), (
        Issue(2, "Ticket", "Implement original requirement", "OPEN", frozenset({"ready-for-agent"})),
    ))
    spec = ticket_spec()
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unread', spec.deliveries[0].base,
                     'refs/heads/run', 'refs/heads/main')
    spec = replace(spec, language='zh', repository_root=str(root.resolve()), inputs=fixture_inputs(),
                   deliveries=(*spec.deliveries, final))
    return spec, source


def _read_operations(kernel, execution, source):
    ports = ReadOnlyPorts(source, 'refs/heads/ticket-2', 'refs/heads/run',
                          DeliveryFacts(Commit('base', 'base-tree'), None))
    return CoreOperations(kernel, execution, lambda binding: ports)


def _native_boundary(tmp_path: Path, *, ordinary_used: bool = False, malformed: bool = False,
                        failure_kind: str = 'ordinary', exit_proven: bool = True, return_body: bytes | None = None):
    """Reach the boundary through native allocation/return/exit in one Run."""
    spec, requirements = _native_spec(tmp_path)
    spec = replace(spec, policy=replace(spec.policy, development_deadline_seconds=7))
    source = create_kernel(tmp_path / "source.sqlite3", spec, requirements=requirements)
    source.clock = lambda: 100.0
    owner = source.claim_executor("source-executor")
    source_operations = _read_operations(source, owner, requirements)
    call = source_operations.once().call
    assert call is not None
    source.bind_thread(call, "retained-backend")
    for index in range(2 if ordinary_used else 1):
        assert source.dispatch_agent(call)
        process = {"pid": 23 + index, "start_token": "start-" + str(index), "pgid": 23 + index,
                   "host_pid": 11, "host_start_token": "host-start"}
        source.agent_started(call, process)
        source.agent_source_ready(call)
        body = b'{"summary":"incomplete return"}' if malformed else return_body
        if body is not None:
            source.receive(call, body)
        if exit_proven:
            source.agent_exited(call, process, 0 if body is not None else 1, failure_kind=failure_kind)
        if index == 0 and ordinary_used:
            call = source_operations.once().call
            assert call is not None
    return source, owner, source_operations, call.attempt_id, call


RAW = b'{"result_kind":"development","summary":"one actual worker","human_blockers":null}'

def _allocated_worker(tmp_path, monkeypatch):
    source = Requirements(ParentIssue(1, "Parent", "Original scope", (2,)),
                          (Issue(2, "Ticket", "Original task", "OPEN", frozenset({"ready-for-agent"})),))
    scenario = GraphScenario(tmp_path / "scenario", source)
    preparation = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    call = preparation.until(lambda step: step.call is not None).call
    assert call is not None
    transport = tmp_path / "transport"
    transport.mkdir()
    _port, _git, _push, state, _calls = _fixture(transport, monkeypatch)
    scenario.port.git.run("remote", "add", "offline-remote", str(transport / "origin.git"))
    scenario.port.git.run("push", "--force", "offline-remote",
                          scenario.initial.sha + ":refs/heads/main",
                          scenario.initial.sha + ":" + scenario.delivery.target_ref,
                          scenario.initial.sha + ":" + scenario.delivery.source_ref)
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open", "source_graph": {
        "parent": {"number": 1, "title": "Parent", "body": "Original scope", "sub_issues": [2]},
        "issues": {"2": {"number": 2, "title": "Ticket", "body": "Original task", "state": "OPEN",
                           "labels": ["ready-for-agent"]}}}}))
    operations = CoreOperations(scenario.kernel, scenario.execution, _make_ports)
    operations.runner.observe(scenario.delivery.key)
    return scenario, operations, state, call
