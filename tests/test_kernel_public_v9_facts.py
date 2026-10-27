"""Public consumers read actual native producer facts, not a legacy Run view."""

from support.repository_fixture import create_kernel
import json
from pathlib import Path

import pytest

from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import Commit, Delivery, DeliveryKind, Policy, RunSpec, Issue, ParentIssue, Requirements
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from support.offline import Scenario
from agent_run.queries.run import read
from agent_run.cli.main import main
from agent_run.core.operation_contract import DeliveryFacts
from support.core_operations import CoreOperations, ReadOnlyPorts
from test_kernel_configuration import _actual_publication_push


def _kernel(root: Path) -> tuple[Kernel, ReadOnlyPorts]:
    defaults = UserDefaultsStore(root / 'defaults.json')
    defaults.configure(language='en', profile={'development_model': 'frozen-model', 'development_effort': 'high'},
                       github={'worker_source': 'pat', 'pat': {'env': 'FROZEN_TOKEN'}, 'runner_source': 'host'},
                       notifications={'enabled': False, 'mode': 'detailed'})
    base = Commit('base', 'tree')
    job = Delivery('ticket', DeliveryKind.TICKET, 2, 'ticket-v1', base, 'refs/heads/source', 'refs/heads/target')
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unread', base, job.target_ref, 'refs/heads/main')
    source = Requirements(ParentIssue(1, 'Parent', 'Original requirement', (2,)), (
        Issue(2, 'Ticket', 'Original behavior', 'OPEN', frozenset({'ready-for-agent'})),
    ))
    spec = freeze_creation(RunSpec('run', 'example/project', 1, 'parent-v1', 'graph-v1', (job, final),
        repository_root=str(root)), defaults, prompt_resources={key: 'Original ' + key for key in RESOURCE_KEYS})
    return (create_kernel(root / 'kernel.sqlite3', spec, requirements=source),
            ReadOnlyPorts(source, job.source_ref, job.target_ref, DeliveryFacts(base, None)))


def _durable(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()
            and not path.name.endswith('-shm') and not (path.name.endswith('-wal') and path.stat().st_size == 0)}


def test_frozen_settings_and_old_thread_binding_survive_personal_and_run_profile_changes(tmp_path: Path, capsys, monkeypatch) -> None:
    kernel, ports = _kernel(tmp_path)
    execution = kernel.claim_executor('executor')
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    first = operations.once().call
    assert first is not None
    revision = kernel.configure_profiles('explicit-configure', overrides={'development_model': 'new-thread-only'})
    config = tmp_path / 'personal' / 'agent-run' / 'user-defaults.json'
    config.parent.mkdir(parents=True)
    config.write_text('{invalid personal settings')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config.parent.parent))
    before = _durable(tmp_path)
    assert main(['settings', 'show', '--run', 'run', '--state-dir', str(kernel.storage.path), '--json']) == 0
    settings = json.loads(capsys.readouterr().out)
    assert settings['scope'] == 'run' and settings['language'] == 'en'
    assert settings['profile']['profile_revision'] == revision
    assert settings['profile']['profiles']['development']['model'] == 'new-thread-only'
    assert settings['github']['worker'] == {'source': 'pat', 'pat': {'env': 'FROZEN_TOKEN'}}
    assert settings['notifications']['mode'] == 'detailed'
    assert settings['prompt_resources'] == {key: 'Original ' + key for key in RESOURCE_KEYS}
    invocation = read(kernel.storage.path, 'status', storage=kernel.storage).data['subjects'][0]['pending_attempt']['invocations'][0]
    assert invocation['model'] == 'frozen-model' and invocation['profile_revision'] == first.profile_revision
    assert invocation['profile_revision'] != revision
    assert _durable(tmp_path) == before


def test_query_retains_physical_output_raw_and_actual_activity_without_recounting_budget(tmp_path: Path) -> None:
    kernel, ports = _kernel(tmp_path)
    now = [100.0]
    kernel.clock = lambda: now[0]
    execution = kernel.claim_executor('executor')
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    first = operations.once().call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, 'actual-thread')
    process = {'pid': 23, 'pgid': 23, 'start_token': 'first', 'host_pid': 11, 'host_start_token': 'host'}
    kernel.agent_started(first, process)
    kernel.observe_worker_activity(first, 'start', active=True, observed_at=100.25, evidence={'event': 'turn.started'})
    kernel.observe_worker_activity(first, 'end', active=False, observed_at=100.75, evidence={'event': 'turn.completed'})
    kernel.receive(first, b'Original invalid output / raw')
    now[0] = 101.0
    kernel.agent_exited(first, process, 0)
    repair = operations.once().call
    assert repair is not None and repair.output_ordinal == 1 and kernel.dispatch_agent(repair)
    next_process = {**process, 'pid': 24, 'pgid': 24, 'start_token': 'second'}
    kernel.agent_started(repair, next_process)
    kernel.receive(repair, b'{"result_kind":"development","summary":"Exact repaired raw","human_blockers":null}')
    now[0] = 103.0
    kernel.agent_exited(repair, next_process, 0)
    assert operations.once().kind == 'progress'
    before = _durable(tmp_path)
    data = read(kernel.storage.path, 'history', storage=kernel.storage).data
    invocation = data['history']['attempts'][0]['invocations'][0]
    assert invocation['id'] == first.invocation_id
    outputs = invocation['outputs']
    assert [output['ordinal'] for output in outputs] == [0, 1]
    assert outputs[0]['raw_return'] == 'Original invalid output / raw'
    assert outputs[1]['raw_return']['summary'] == 'Exact repaired raw'
    assert outputs[0]['process_span_seconds'] == 1.0
    assert outputs[0]['elapsed_seconds'] == 0.5
    assert outputs[1]['elapsed_seconds'] is None
    assert [(event['active'], event['observed_at']) for event in outputs[0]['activity']] == [(True, 100.25), (False, 100.75)]
    assert sum(len(invocation['outputs']) for attempt in data['history']['attempts'] for invocation in attempt['invocations']) == 2
    budget = next(row for row in [budget for subject in data['subjects'] for budget in subject['budgets']] if row['purpose'] == 'requirements' and row['current'])
    assert budget['developments_used'] == 1
    returned = [event for event in data['history']['events'] if event['kind'] == 'raw_returned']
    assert [event['phase'] for event in returned] == ['development', 'development']
    assert data['subjects'][0]['phase'] == 'candidate'
    assert _durable(tmp_path) == before


def test_publication_actual_failure_is_not_dispatch_count_or_erased_by_successful_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, effect = _actual_publication_push(tmp_path, monkeypatch, 7)
    before = _durable(tmp_path)
    data = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data
    failures = data['history']['effect_failures']
    assert len(failures) == 1 and failures[0]['operation'] == 'push'
    assert failures[0]['effect'] == effect.effect_id and failures[0]['dispatch_serial'] == 1
    assert failures[0]['evidence']['returncode'] == 7
    assert "actual transport command exited nonzero" in failures[0]['evidence']['message']
    assert sum(operation['attempts'] for operation in data['history']['effects']) > len(failures)
    assert next(row for row in data['history']['effects'] if row['id'] == effect.effect_id)['status'] == 'observed'
    assert _durable(tmp_path) == before


def test_publication_unknown_response_diagnostic_remains_public_after_successful_exact_readback(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    scenario.port.lose_response.add('pr_write')
    unresolved = scenario.run()
    assert unresolved.kind == "waiting" and unresolved.reason == "github_convergence"
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending is not None and pending["operation"] == "pr_write" and pending["status"] == "sent"
    diagnostic = {"code": "write_response_unavailable", "message": "pr_write took effect; response lost"}
    assert read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data['supervision_windows'][0]['latest_observation'] == diagnostic
    assert unresolved.retry_at is not None
    now[0] = unresolved.retry_at
    assert scenario.run().kind == 'approval'
    before = _durable(tmp_path)
    data = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data
    failures = data['history']['effect_failures']
    assert failures == []  # Unknown response is not a proved nonzero command exit.
    assert sum(operation['attempts'] for operation in data['history']['effects']) > len(failures)
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref) is not None
    operation = next(row for row in data['history']['effects'] if row['id'] == pending['id'])
    assert operation['attempts'] == 1 and operation['status'] == 'confirmed'
    assert sum(row['operation'] == 'pr_write' for row in scenario.port.writes()) == 1
    ci = next(row for row in [budget for subject in data['subjects'] for budget in subject['budgets']] if row['purpose'] == 'ci' and row['current'])
    assert ci['review_limit'] is None and bool(ci['review_required']) is True
    assert _durable(tmp_path) == before
    # Immutable storage alone is insufficient: the public audit must still make
    # the original uncertainty diagnosable after the exact intent is consumed.
    audit = json.dumps(data, ensure_ascii=False)
    assert diagnostic['code'] in audit and diagnostic['message'] in audit


def test_zero_resync_gate_exposes_actual_window_and_explicit_resume_without_renewal(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY, policy=Policy(resync_limit=0))
    assert scenario.run().kind == 'approval'
    scenario.port.external_remote_append(scenario.delivery.target_ref, {'external.txt': 'Original external work'}, 'append')
    assert scenario.run().kind == 'paused'
    before = _durable(tmp_path)
    data = read(scenario.kernel.storage.path, 'status', storage=scenario.kernel.storage).data
    assert data['subjects'][0]['integration_resync']['limit'] == 0 and data['subjects'][0]['integration_resync']['used'] == 0
    assert data['subjects'][0]['integration_resync']['window'] == 1 and data['subjects'][0]['integration_resync']['open'] is False
    action = next(item for item in data['operator_actions'] if item['reason'] == 'integration_resync_exhausted')
    assert action['operation'] == 'resume' and action['action']['command'] == 'resume'
    assert _durable(tmp_path) == before


def test_resume_application_receipt_does_not_claim_start_before_the_bound_successor(tmp_path: Path) -> None:
    kernel, ports = _kernel(tmp_path)
    kernel.clock = lambda: 100.0
    executor = kernel.claim_executor('executor')
    operations = CoreOperations(kernel, executor, lambda binding: ports)
    first = operations.once().call
    assert first is not None
    kernel.receive(first, b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Need original decision"]}')
    assert operations.once().kind == 'paused'
    assert kernel.control('resume', 'exact-response', subject='ticket', message='Original response') == 'applied'
    before_start = next(action for action in read(kernel.storage.path, 'history', storage=kernel.storage).data['history']['actions'] if action['kind'] == 'resume')
    assert before_start['status'] == 'applied' and before_start['resume']['started_at'] is None
    assert before_start['resume']['successor_invocations'] == []
    successor = operations.once().call
    assert successor is not None and kernel.dispatch_agent(successor)
    kernel.bind_thread(successor, 'real-resumed-thread')
    process = {'pid': 21, 'pgid': 21, 'start_token': 'successor', 'host_pid': 10, 'host_start_token': 'host'}
    kernel.agent_started(successor, process)
    bound_only = next(action for action in read(kernel.storage.path, 'history', storage=kernel.storage).data['history']['actions']
                      if action['kind'] == 'resume')
    assert bound_only['resume']['started_at'] is None
    # Synthetic backend activity is distinct from the original process binding.
    # Use the same native Go/activity producer as the current Worker contract.
    kernel.agent_source_ready(successor)
    kernel.observe_worker_activity(successor, 'original-turn-start', active=True,
        observed_at=100.0, evidence={'type': 'turn.started', 'source': 'synthetic-query-fixture'})
    before = _durable(tmp_path)
    data = read(kernel.storage.path, 'history', storage=kernel.storage).data
    resumed = next(action for action in data['history']['actions'] if action['kind'] == 'resume')
    assert resumed['resume']['successor_invocations'] == [successor.invocation_id]
    assert resumed['resume']['started_at'] == 100.0 and resumed['payload']['message'] == 'Original response'
    assert successor.attempt_id == first.attempt_id
    assert _durable(tmp_path) == before
