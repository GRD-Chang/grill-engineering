"""Action scope is independent of its immutable material owner's subject/order."""
from support.core_operations import prepare_scripted_agent
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Blocker, Issue, ParentIssue, Policy, Requirements
from agent_run.queries.run import read
from agent_run.queries.read_facts import QueryError
from support.offline import GraphScenario
from support.environment import durable_snapshot as _snapshot


def _graph(root):
    source = Requirements(ParentIssue(1, 'Parent', 'Current complete graph', (2, 3)), (
        Issue(2, 'First', 'First requirement', 'OPEN', frozenset({'ready-for-agent'})),
        Issue(3, 'Second', 'Second requirement', 'OPEN', frozenset({'ready-for-agent'}), (Blocker(2, 'OPEN'),)),
    ))
    scenario = GraphScenario(root, source, policy=Policy(concurrency=2))
    scenario.refresh()
    return scenario


def _cli_history(kernel):
    result = subprocess.run([sys.executable, '-m', 'agent_run', 'history', 'offline-run',
                             '--state-dir', str(kernel.storage.path), '--json'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=15)
    output = json.loads(result.stdout)
    assert result.returncode == 0, {'returncode':result.returncode, 'output':output}
    return output


def test_completed_graph_history_preserves_run_wide_action_material_owners(tmp_path):
    scenario = _graph(tmp_path/'graph')
    kernel = scenario.kernel
    assert kernel.source_status()['final_subject'] is None
    revision = kernel.configure_profiles('profiles-before-final', preset='economy')
    assert kernel.control('stop', 'stop-before-final') == 'applied'
    kernel.confirm_executor_exit(scenario.execution)
    assert kernel.control('resume', 'resume-before-final') == 'applied'
    scenario.execution = kernel.claim_executor('resumed-before-final')
    with kernel.storage.transaction(write=False) as tx:
        original = {row['id']:dict(row) for row in tx.rows('SELECT * FROM actions')}
        assert set(original) == {'profiles-before-final', 'stop-before-final', 'resume-before-final'}
        for action in original.values():
            assert action['subject'] is None
            assert tx.one('SELECT subject FROM material WHERE id=?', (action['payload_id'],))[0] == 'ticket:2'
    for subject in ('ticket:2', 'ticket:3'):
        scenario.select(subject)
        assert scenario.run().kind == 'complete'
    for _ in range(10):
        if kernel.source_status()['final_subject'] is not None:
            break
        scenario.step()
    else:
        raise AssertionError('Real completed Tickets did not activate Final')
    # Creation and completion of the real Final, without synthetic DB insertion.
    scenario.select('final:1')
    assert scenario.run(approve=True).kind == 'complete'
    kernel = Kernel(kernel.storage.path)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT id FROM subjects ORDER BY id LIMIT 1')[0] == 'final:1'
    before = _snapshot(tmp_path)
    output = _cli_history(kernel)
    actions = {item['id']:item for item in output['history']['actions']}
    for identity, saved in original.items():
        action = actions[identity]
        assert action['subject'] is None
        assert action['payload_id'] == saved['payload_id'] and action['receipt_id'] == saved['receipt_id']
        assert action['payload']['kind'] == saved['kind']
    assert actions['profiles-before-final']['receipt'] == {'profile_revision':revision}
    assert output['latest_resume']['id'] == 'resume-before-final'
    assert _snapshot(tmp_path) == before
    # A repeated original Action is a read of that Action, not a new command.
    assert kernel.control('stop', 'stop-before-final') == 'applied'
    assert kernel.control('resume', 'resume-before-final') == 'applied'
    assert kernel.configure_profiles('profiles-before-final', preset='economy') == revision
    assert _snapshot(tmp_path) == before
    with pytest.raises(KernelError, match='action_identity_changed'):
        kernel.control('resume', 'stop-before-final')
    with pytest.raises(KernelError, match='action_identity_changed'):
        kernel.control('stop', 'stop-before-final', subject='ticket:3')
    assert _snapshot(tmp_path) == before
    # Wrong-scope material reads still fail; no subject probing or fallback.
    with kernel.storage.transaction(write=False) as tx:
        for owner in ('ticket:3', 'final:1'):
            with pytest.raises(KernelError, match='material_corrupt'):
                tx.get(original['profiles-before-final']['payload_id'], owner)
    wrong = subprocess.run([sys.executable, '-m', 'agent_run', 'history', 'another-run',
                            '--state-dir', str(kernel.storage.path), '--json'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=15)
    assert wrong.returncode == 2
    assert 'run_locator_missing' in wrong.stdout
    with pytest.raises(QueryError) as mismatch:
        read(kernel.storage.path, 'history', expected_run='another-run')
    assert mismatch.value.code == 'run_locator_conflict'
    assert _snapshot(tmp_path) == before


def test_action_history_keeps_original_generation_after_explicit_requeue(tmp_path):
    scenario = _graph(tmp_path/'graph')
    kernel = scenario.kernel
    kernel.configure_profiles('profiles-g1', preset='economy')
    runner = scenario.runner()
    for _ in range(10):
        call = prepare_scripted_agent(kernel, runner.run_once()).call
        if call is not None:
            break
    else:
        raise AssertionError('Original Ticket never reached Development')
    runner.authorize_agent(call)
    kernel.receive(call, scenario.agent(call))
    assert prepare_scripted_agent(kernel, runner.run_once()).kind == 'progress'
    before_revision = kernel.status('ticket:2')['revision']
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body+'\nNew requirement') if issue.number == 2 else issue
        for issue in scenario.requirements.issues))
    world = scenario.runner().observe('ticket:2')
    old = kernel.delivery('ticket:2')
    replacement = replace(old, source_ref='refs/heads/ticket-2-g2', base=world.target,
                          checkout=str(Path(old.checkout).with_name('ticket-2-g2')))
    assert kernel.requeue('ticket:2', 'new-requirement', replacement, world) == 'applied'
    assert kernel.status('ticket:2')['generation'] == 2
    assert kernel.status('ticket:2')['revision'] != before_revision
    before = _snapshot(tmp_path)
    output = _cli_history(kernel)
    event = next(item for item in output['history']['events'] if item['kind']=='profiles_configured')
    assert event['subject'] == 'ticket:2' and event['generation'] == 1
    action = next(item for item in output['history']['actions'] if item['id']=='profiles-g1')
    assert action['subject'] is None and action['payload']['kind'] == 'configure_profiles'
    assert _snapshot(tmp_path) == before
