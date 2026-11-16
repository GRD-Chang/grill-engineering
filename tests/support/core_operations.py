"""Core runner driving and finite explicit fixture producers.

CoreOperations leaves selection and authority in the real Runner. The controlled
credential/output producers are test inputs. settle_agent retains the existing
synchronous synthetic process observations; it is never OS/Host exit proof.
"""

from support.repository_fixture import create_kernel
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from agent_run.model import Commit, AgentCall, AgentPreparation, CreationInputs, Delivery, DeliveryKind, Effect, EffectResult, Execution, Issue, Policy, ParentIssue, Requirements, wire_json, ResourceInspection, ResourceWitness, RunSpec, Step
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import DeliveryFacts, DeliveryRead, OperationBinding, OperationPorts, requested_ancestry
from agent_run.core.operation_runner import OperationRunner
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.storage.run_scope import transaction_scope

from support.offline import GraphScenario
from support.requirements_scenarios import independent_source, native_input
from agent_run.model import Role
from agent_run.core.operation_contract import ReadUnavailable
from support.offline import Scenario, fixture_inputs


def prepare_scripted_agent(kernel: Kernel, step: Step | AgentPreparation) -> Step:
    """Admit a controlled-ready issuer result at the real Core boundary.

    Pure Core/SQLite/Git scenarios do not consume native credential files. The
    fixture supplies that external prerequisite explicitly; Core still validates
    the original preparation, owner, seat, frozen request and absolute deadline.
    No Go, Thread binding, activity, raw or exit observation is supplied here.
    Native transport tests retain AgentPreparation and their real issuer/Capture;
    they must not use this shortcut as credential/startup proof.
    """
    if isinstance(step, Step) and step.preparation is None:
        return step
    preparation = step if isinstance(step, AgentPreparation) else step.preparation
    assert preparation is not None
    kernel.guard_preparation(preparation)
    return kernel.admit_agent(preparation)


def scripted_return(kernel: Kernel, runner: OperationRunner, call: AgentCall,
                    agent: Callable[[AgentCall], bytes]) -> None:
    """A fixture Agent's raw result only; no invented start or exit observation."""
    runner.authorize_agent(call)
    kernel.receive(call, agent(call))


def declared_delivery(request: DeliveryRead, facts: DeliveryFacts) -> DeliveryFacts:
    """Project declared external relations, never infer an unknown Git edge.

    Exact observed Commit identities prove only their reflexive relations.
    Other positive/negative edges must be explicit fixture inputs.
    """
    checked = requested_ancestry(request, facts.source, facts.target, facts.pr)
    ancestry = facts.ancestry
    if request.ancestry_pairs is not None:
        commits = (facts.source, facts.target) if facts.pr is None else (
            facts.source, facts.target, facts.pr.head, facts.pr.base)
        reflexive = {(commit.sha, commit.sha) for commit in commits if commit is not None}
        positive = set(ancestry) | reflexive
        known = set(facts.ancestry_checked or ()) | positive
        if not set(checked or ()) <= known:
            raise ReadUnavailable("fixture_ancestry_unknown", "Fixture has not declared the requested Git relation")
        ancestry = tuple(pair for pair in checked or () if pair in positive)
    return replace(facts, ancestry=ancestry, ancestry_checked=checked,
                   checks=facts.checks if request.read_checks else None,
                   checks_requested=request.read_checks)


class ReadOnlyPorts:
    """Explicit external facts for Agent-lifecycle tests with no Git writes.

    Effects fail loudly: this fixture cannot stand in for an offline delivery
    adapter or silently claim a side effect succeeded.
    """
    def __init__(self, source: Requirements, source_ref: str, target_ref: str,
                 facts: DeliveryFacts) -> None:
        self.requirements, self.source_ref, self.target_ref, self.facts = source, source_ref, target_ref, facts

    def source(self, parent_number: int) -> Requirements:
        assert parent_number == self.requirements.parent.number
        return self.requirements

    def delivery(self, request: DeliveryRead) -> DeliveryFacts:
        assert (request.source_ref, request.target_ref) == (self.source_ref, self.target_ref)
        return declared_delivery(request, self.facts)

    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]:
        raise AssertionError("This read-only fixture cannot inspect physical resources")

    def execute(self, effect: Effect, authorize: WriteAuthority) -> EffectResult:
        raise AssertionError(f"This read-only fixture cannot execute {effect.operation}")

    def reconcile(self, effect: Effect) -> EffectResult:
        raise AssertionError(f"This read-only fixture cannot reconcile {effect.operation}")

    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]:
        raise AssertionError("This read-only fixture cannot prepare Publication")

    def prepare_retirement(self, effect: Effect) -> Mapping[str, Any]:
        raise AssertionError("This read-only fixture cannot prepare retirement")


def native_agent_fixture(path: Path, spec: RunSpec) -> tuple[Kernel, ReadOnlyPorts]:
    """Create fresh explicit source/input facts for an isolated Agent unit.

    This is fixture setup, never an upgrade or a runtime fallback. It adds the
    real Completion target required by native Ticket creation; it cannot run
    that delivery or authorize physical effects through ReadOnlyPorts.
    """
    from support.offline import fixture_inputs

    assert not any(item.kind == DeliveryKind.FINAL_RUN for item in spec.deliveries)
    first = spec.deliveries[0]
    tickets = tuple(item for item in spec.deliveries if item.kind == DeliveryKind.TICKET)
    source = Requirements(ParentIssue(spec.parent_issue, "Parent", "Explicit original requirement",
        tuple(item.issue for item in tickets)), tuple(Issue(item.issue, "Ticket", item.revision,
            "OPEN", frozenset({"ready-for-agent"})) for item in tickets))
    deliveries = spec.deliveries
    if tickets:
        assert all(item.base == first.base and item.target_ref == first.target_ref for item in tickets)
        deliveries += (Delivery('fixture-final', DeliveryKind.FINAL_RUN, spec.parent_issue, 'unread',
            first.base, first.target_ref, 'refs/heads/fixture-main'),)
    inputs = spec.inputs if spec.inputs is not None else fixture_inputs()
    spec = replace(spec, repository_root=spec.repository_root or str(path.parent.resolve()),
                   language=spec.language or 'zh', inputs=inputs, deliveries=deliveries)
    return (create_kernel(path, spec, requirements=source),
            ReadOnlyPorts(source, first.source_ref, first.target_ref, DeliveryFacts(first.base, None)))


def final_scenario(root: Path, *, policy: Policy | None = None,
                   reviews: tuple[str, ...] = ('pass',), checks: tuple[str, ...] = ('none',),
                   inputs: CreationInputs | None = None, language: str = 'zh') -> 'GraphScenario':
    """Reach Final through a real Ticket Completion, never a seeded history row.

    The preparatory Ticket uses successful fixture Agents/checks. Only after
    Core admits Final do the requested Final Agent/check scripts start.
    """
    from support.offline import GraphScenario

    source = Requirements(ParentIssue(1, 'Parent', 'Deliver the original behavior.', (2,)), (
        Issue(2, 'Ticket', 'Implement the original behavior.', 'OPEN', frozenset({'ready-for-agent'})),
    ))
    scenario = GraphScenario(root, source, policy=policy)
    if inputs is not None:
        # Configure the fresh creation before any business operation, never
        # recreate a progressed Run and lose its real Completion history.
        scenario.spec = replace(scenario.spec, inputs=inputs, language=language)
        scenario.kernel = create_kernel(root / 'configured.sqlite3', scenario.spec, requirements=source)
        scenario.execution = scenario.kernel.claim_executor('configured-fixture')
    from support.run_scoped_kernel import prepare_domain_history
    with prepare_domain_history(scenario):
        operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
        for _ in range(100):
            step = operations.once()
            if step.call is not None:
                scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
            if step.reason == 'final_activated':
                assert len(scenario.kernel.source_status()['completions']) == 1
                scenario.select(step.subject)
                scenario.reviews, scenario.checks = list(reviews), list(checks)
                scenario.development_count = scenario.review_count = scenario.publication_count = 0
                return scenario
    raise AssertionError(f'Preparatory Ticket did not reach real Final admission: {operations.steps}')


class CoreOperations:
    def __init__(self, kernel: Kernel, execution: Execution,
                 make_ports: Callable[[OperationBinding], OperationPorts], *, prepare_agents: bool = True) -> None:
        self.prepare_agents = prepare_agents
        self.runner = OperationRunner(kernel, execution, make_ports)
        self.steps: list[Step] = []

    def once(self, *, agent_capacity: int = 1) -> Step:
        step = self.runner.run_once(agent_capacity=agent_capacity)
        if self.prepare_agents:
            step = prepare_scripted_agent(self.runner.kernel, step)
        self.steps.append(step)
        return step

    def until(self, predicate: Callable[[Step], bool], *, limit: int = 64,
              agent_capacity: int = 1) -> Step:
        for _ in range(limit):
            step = self.once(agent_capacity=agent_capacity)
            if predicate(step):
                return step
            if step.call is not None or step.preparation is not None:
                raise AssertionError(f"Test must handle the reserved Agent work: {step}")
        raise AssertionError(f"Core did not reach the expected boundary in {limit} operations: {self.steps}")


def observed_call(kernel: Kernel, request: AgentPreparation) -> AgentCall:
    """Read the real admitted identity after native transport ran, never admit.

    Tests use the actual Invocation/output bound to this original seat. No
    execution, credential readiness, raw result or exit evidence is fabricated.
    """
    with kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        assert scope.run_id == request.execution.run_id
        row = tx.one("SELECT i.id,i.deadline_seconds,i.deadline_at,o.ordinal,o.context_id FROM invocations i "
                     "JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id "
                     "WHERE i.run_id=? AND o.lease=?", (scope.run_id, request.reservation_id))
        assert tx.get(row['context_id'], request.subject) == wire_json(request.context)
        return AgentCall(request.attempt_id, row['id'], request.subject, request.role, request.purpose,
            request.thread_id, request.reservation_id, request.execution, request.context, request.profile,
            row['deadline_seconds'], row['deadline_at'], request.mode, request.reported_thread,
            row['ordinal'], request.profile_revision, request.profile_reference)


def agent_preparation_fixture(tmp_path: Path, *, concurrency: int = 1):
    base = Commit('base', 'tree')
    spec = RunSpec('preparation', 'example/repository', 1, 'parent', 'graph', (
        Delivery('ticket:2', DeliveryKind.TICKET, 2, 'original', base,
                 'refs/heads/ticket', 'refs/heads/run', checkout=str(tmp_path)),
    ), Policy(ticket_rounds=1, development_deadline_seconds=120, concurrency=concurrency))
    (tmp_path / 'state').mkdir()
    kernel, ports = native_agent_fixture(tmp_path / 'state' / 'kernel.sqlite3', spec)
    clock = [1000.0]
    kernel.clock = lambda: clock[0]
    owner = kernel.claim_executor('preparation-owner')
    runner = OperationRunner(kernel, owner, lambda _: ports)
    for _ in range(6):
        step = runner.run_once()
        if step.call is not None or getattr(step, 'preparation', None) is not None:
            return kernel, runner, ports, step, clock
    raise AssertionError('Core did not reserve Agent work')


class Ports:
    def __init__(self, scenario):
        self.scenario = scenario
        self.reads = 0

    def source(self, parent_number):
        self.reads += 1
        s = self.scenario
        assert parent_number == s.requirements.parent.number
        return s.port.source(s.requirements, parent_number)

    def delivery(self, request):
        s = self.scenario
        return s.port.delivery(request, ('none',), [], publisher_identity=s.spec.inputs.runner_identity)

    def inspect(self, resources):
        return self.scenario.port.inspect_resources(resources)

    def execute(self, effect, authorize):
        return self.scenario.port.execute(effect, authorize)

    def reconcile(self, effect):
        return self.scenario.port.reconcile(effect)

    def prepare_publication(self, effect):
        return self.scenario.port.local.prepare_publication(effect)

    def prepare_retirement(self, effect):
        return self.scenario.port.prepare_retirement(effect)


def setup(tmp_path, *, concurrency=1, dependent=False):
    source = Requirements.from_wire(native_input()["initial_requirements"]) if dependent else independent_source()
    s = GraphScenario(tmp_path / "graph", source, policy=Policy(concurrency=concurrency))
    ports = Ports(s)
    return s, ports, OperationRunner(s.kernel, s.execution, lambda binding: ports)


def settle_agent(s, runner, call):
    assert s.kernel.dispatch_agent(call)
    runner.authorize_agent(call)
    process = {"pid": 4242, "start_token": "synthetic", "pgid": 4242,
               "host_pid": 4241, "host_start_token": "synthetic-host"}
    s.kernel.agent_started(call, process)
    s.select(call.subject)  # Observe the returned call; never choose runnable work.
    s.kernel.receive(call, s.agent(call))
    assert not runner.worker_settled(call)
    s.kernel.agent_exited(call, process, 0)
    assert runner.worker_settled(call)


def _prepare_dependent_ticket(tmp_path: Path):
    scenario = GraphScenario(tmp_path / "dependencies", Requirements.from_wire(native_input()["initial_requirements"]))
    assert scenario.kernel.source_status()["remaining"]["ticket:4"] == "blocked_by_incomplete_tickets"
    assert scenario.run().kind == "complete"
    head = scenario.port.remote_commit("refs/heads/run")
    assert head is not None and head != scenario.initial
    scenario.select("ticket:4")
    assert not Path(scenario.delivery.checkout).exists()
    assert not scenario.port.git.run("for-each-ref", "--format=%(refname)", scenario.delivery.source_ref).stdout
    return scenario


def host_kernel(tmp_path: Path) -> Kernel:
    base = Commit("base", "tree")
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, 1, "requirements", base, "source", "target")
    return create_kernel(tmp_path / "state" / "kernel.sqlite3", RunSpec(
        "host-run", "example/repository", 1, "requirements", "graph", (delivery,),
        language="en", repository_root=str(tmp_path / "repository"),
    ))

def task_run(root: Path, run_id: str, *, parent: int = 1) -> tuple[Kernel, ReadOnlyPorts]:
    base = Commit("base", "tree")
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, parent, "v1", base, "refs/heads/task", "refs/heads/main")
    return native_agent_fixture(root / "kernel.sqlite3", RunSpec(run_id, "example/project", parent, "v1", "graph", (delivery,),
                         language="en", repository_root=str(root / "repository")))


def delivery_run(path: Path, *, language: str = "en") -> tuple[Kernel, Requirements]:
    source = Requirements(ParentIssue(1, "Parent", "Requirement", (2, 3)), (
        Issue(2, "First", "First work", "OPEN", frozenset({"ready-for-agent"})),
        Issue(3, "Second", "Second work", "OPEN", frozenset({"ready-for-agent"})),
    ))
    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "unread", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3))
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "unread", base, "refs/heads/run", "refs/heads/main")
    return create_kernel(path, RunSpec("run", "example/repository", 1, "unread", "unread", (*deliveries, final),
        Policy(concurrency=2), language=language, repository_root=str(path.parent.resolve()), inputs=fixture_inputs()), requirements=source), source

def source_operations(kernel, source, execution):
    """Explicit read-only external facts; selection is shared production Core."""
    deliveries = [kernel.delivery(f"ticket:{issue.number}") for issue in source.issues]
    facts = {(delivery.source_ref, delivery.target_ref): DeliveryFacts(delivery.base, None) for delivery in deliveries}
    first = deliveries[0]
    ports = ReadOnlyPorts(source, first.source_ref, first.target_ref, facts[(first.source_ref, first.target_ref)])
    ports.delivery = lambda request: facts[(request.source_ref, request.target_ref)]
    return CoreOperations(kernel, execution, lambda binding: ports)

def unavailable(parent):
    raise ReadUnavailable("github_read_failed", "Original source outage")

def next_call(scenario: Scenario, role: Role) -> AgentCall:
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    for _ in range(160):
        step = operations.once()
        if step.call is not None and step.call.role == role:
            return step.call
        assert step.kind not in {"paused", "waiting", "complete", "approval"}, step
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
    raise AssertionError("The requested role never became ready")


def budgets(scenario):
    with scenario.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        return {row['purpose']: dict(row) for row in tx.rows(
            'SELECT * FROM budgets WHERE run_id=? AND subject=? AND current=1 ORDER BY purpose',
            (scope.run_id, scenario.delivery.key))}


def _window(scenario: Scenario) -> dict[str, float]:
    with scenario.kernel.storage.transaction(write=False) as tx:
        scope = transaction_scope(tx)
        row = tx.one("SELECT started_at,deadline FROM waits WHERE run_id=? AND subject=?", (scope.run_id, scenario.delivery.key))
        return {"started_at": row["started_at"], "deadline": row["deadline"]}
