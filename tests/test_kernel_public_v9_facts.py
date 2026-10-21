"""Public consumers read actual native producer facts, not a legacy Run view."""
import json
from pathlib import Path

from agent_run.prompt_resources import RESOURCE_KEYS
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, RunSpec, World
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.offline import Scenario
from agent_run_kernel.query import read
from agent_run_kernel.query_cli import main


def _kernel(root: Path) -> tuple[Kernel, World]:
    defaults = UserDefaultsStore(root / 'defaults.json')
    defaults.configure(language='en', profile={'development_model': 'frozen-model', 'development_effort': 'high'},
                       github={'worker_source': 'pat', 'pat': {'env': 'FROZEN_TOKEN'}, 'runner_source': 'host'},
                       notifications={'enabled': False, 'mode': 'detailed'})
    base = Commit('base', 'tree')
    job = Delivery('ticket', DeliveryKind.TICKET, 2, 'ticket-v1', base, 'refs/heads/source', 'refs/heads/target')
    spec = freeze_creation(RunSpec('run', 'example/project', 1, 'parent-v1', 'graph-v1', (job,),
        repository_root=str(root)), defaults, prompt_resources={key: 'Original ' + key for key in RESOURCE_KEYS})
    return Kernel.create(root / 'kernel.sqlite3', spec), World('parent-v1', 'graph-v1', 'ticket-v1', base)


def _durable(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()
            and not path.name.endswith('-shm') and not (path.name.endswith('-wal') and path.stat().st_size == 0)}


def test_frozen_settings_and_old_thread_binding_survive_personal_and_run_profile_changes(tmp_path: Path, capsys, monkeypatch) -> None:
    kernel, world = _kernel(tmp_path)
    execution = kernel.claim_executor('executor')
    first = kernel.advance('ticket', execution, world).call
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
    invocation = read(kernel.storage.path, 'status').data['subjects'][0]['pending_attempt']['invocations'][0]
    assert invocation['model'] == 'frozen-model' and invocation['profile_revision'] == first.profile_revision
    assert invocation['profile_revision'] != revision
    assert _durable(tmp_path) == before


def test_query_retains_physical_output_raw_and_actual_activity_without_recounting_budget(tmp_path: Path) -> None:
    kernel, world = _kernel(tmp_path)
    now = [100.0]
    kernel.clock = lambda: now[0]
    execution = kernel.claim_executor('executor')
    first = kernel.advance('ticket', execution, world).call
    assert first is not None and kernel.dispatch_agent(first)
    kernel.bind_thread(first, 'actual-thread')
    process = {'pid': 23, 'pgid': 23, 'start_token': 'first', 'host_pid': 11, 'host_start_token': 'host'}
    kernel.agent_started(first, process)
    kernel.observe_worker_activity(first, 'start', active=True, observed_at=100.25, evidence={'event': 'turn.started'})
    kernel.observe_worker_activity(first, 'end', active=False, observed_at=100.75, evidence={'event': 'turn.completed'})
    kernel.receive(first, b'Original invalid output / raw')
    now[0] = 101.0
    kernel.agent_exited(first, process, 0)
    repair = kernel.advance('ticket', execution, world).call
    assert repair is not None and repair.output_ordinal == 1 and kernel.dispatch_agent(repair)
    next_process = {**process, 'pid': 24, 'pgid': 24, 'start_token': 'second'}
    kernel.agent_started(repair, next_process)
    kernel.receive(repair, b'{"result_kind":"development","summary":"Exact repaired raw","human_blockers":null}')
    now[0] = 103.0
    kernel.agent_exited(repair, next_process, 0)
    assert kernel.advance('ticket', execution, world).kind == 'progress'
    before = _durable(tmp_path)
    data = read(kernel.storage.path, 'history').data
    invocation = data['agent_invocations'][0]
    assert invocation['id'] == first.invocation_id
    outputs = invocation['outputs']
    assert [output['ordinal'] for output in outputs] == [0, 1]
    assert outputs[0]['raw_return'] == 'Original invalid output / raw'
    assert outputs[1]['raw_return']['summary'] == 'Exact repaired raw'
    assert outputs[0]['process_span_seconds'] == 1.0
    assert outputs[0]['elapsed_seconds'] == 0.5
    assert outputs[1]['elapsed_seconds'] is None
    assert [(event['active'], event['observed_at']) for event in outputs[0]['activity']] == [(True, 100.25), (False, 100.75)]
    assert len(data['output_attempts']) == 2
    budget = next(row for row in data['budget_windows'] if row['purpose'] == 'requirements' and row['current'])
    assert budget['developments_used'] == 1
    returned = [event for event in data['timeline'] if event['kind'] == 'raw_returned']
    assert [event['phase'] for event in returned] == ['development', 'development']
    assert data['subjects'][0]['phase'] == 'candidate'
    assert _durable(tmp_path) == before


def test_publication_actual_failure_is_not_dispatch_count_or_erased_by_successful_readback(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY)
    scenario.port.lose_response.add('pr_write')
    assert scenario.run().kind == 'approval'
    before = _durable(tmp_path)
    data = read(scenario.kernel.storage.path, 'history').data
    failures = data['publication_operation_retries']
    assert len(failures) == 1 and failures[0]['operation'] == 'pr_write'
    assert failures[0]['evidence']['type'] == 'LostResponse' and failures[0]['dispatch_serial'] == 1
    assert sum(operation['attempts'] for operation in data['effect_operations']) > len(failures)
    assert scenario.port.pull_request(scenario.delivery.key) is not None
    assert _durable(tmp_path) == before


def test_zero_resync_gate_exposes_actual_window_and_explicit_resume_without_renewal(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY, policy=Policy(resync_limit=0))
    assert scenario.run().kind == 'approval'
    scenario.port.external_remote_append(scenario.delivery.target_ref, {'external.txt': 'Original external work'}, 'append')
    assert scenario.run().kind == 'paused'
    before = _durable(tmp_path)
    data = read(scenario.kernel.storage.path, 'status').data
    assert data['integration_resync']['limit'] == 0 and data['integration_resync']['used'] == 0
    assert data['integration_resync']['window'] == 1 and data['integration_resync']['open'] is False
    action = next(item for item in data['operator_actions'] if item['reason'] == 'integration_resync_exhausted')
    assert action['operation'] == 'resume' and action['action']['command'] == 'resume'
    assert _durable(tmp_path) == before


def test_resume_application_receipt_does_not_claim_start_before_the_bound_successor(tmp_path: Path) -> None:
    kernel, world = _kernel(tmp_path)
    kernel.clock = lambda: 100.0
    executor = kernel.claim_executor('executor')
    first = kernel.advance('ticket', executor, world).call
    assert first is not None
    kernel.receive(first, b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Need original decision"]}')
    assert kernel.advance('ticket', executor, world).kind == 'paused'
    assert kernel.control('resume', 'exact-response', subject='ticket', message='Original response') == 'applied'
    before_start = read(kernel.storage.path, 'history').data['agent_resumes'][0]
    assert before_start['status'] == 'applied' and before_start['started_at'] is None
    assert before_start['successor_invocations'] == []
    successor = kernel.advance('ticket', executor, world).call
    assert successor is not None and kernel.dispatch_agent(successor)
    kernel.bind_thread(successor, 'real-resumed-thread')
    process = {'pid': 21, 'pgid': 21, 'start_token': 'successor', 'host_pid': 10, 'host_start_token': 'host'}
    kernel.agent_started(successor, process)
    before = _durable(tmp_path)
    data = read(kernel.storage.path, 'history').data
    resumed = data['agent_resumes'][0]
    assert resumed['successor_invocations'] == [successor.invocation_id]
    assert resumed['started_at'] == 100.0 and resumed['payload']['message'] == 'Original response'
    assert successor.attempt_id == first.attempt_id
    assert _durable(tmp_path) == before
