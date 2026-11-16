"""Action scope is independent of its immutable material owner's subject/order."""
from support.core_operations import prepare_scripted_agent
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys


from agent_run.model import Blocker, Issue, ParentIssue, Policy, Requirements
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


def test_action_history_keeps_original_generation_after_explicit_requeue(tmp_path):
    scenario = _graph(tmp_path/'graph')
    kernel = scenario.kernel
    kernel.configure_profiles('profiles-g1', preset='economy')
    with kernel.storage.transaction(write=False) as tx:
        action = tx.one("SELECT payload_id FROM actions WHERE id='profiles-g1'")
        owner = tx.one('SELECT subject FROM material WHERE run_id=? AND id=?', (tx.run_id, action['payload_id']))[0]
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
    assert event['subject'] == owner and event['generation'] == 1
    action = next(item for item in output['history']['actions'] if item['id']=='profiles-g1')
    assert action['subject'] is None and action['payload']['kind'] == 'configure_profiles'
    assert _snapshot(tmp_path) == before
