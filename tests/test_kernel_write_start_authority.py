"""Command environment preparation must precede final Core write authority."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from threading import Event

import pytest

from agent_run.adapters import github_commands, remote
from agent_run.adapters.auth import runner_transport
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, ReadWait
from support.offline import Scenario
from test_kernel_exploration import advance_to_effect
from support.remote_effects import FAKE_GH


@pytest.mark.parametrize('operation', ['pr_write', 'push'])
def test_public_stop_during_command_environment_preparation_prevents_process_write(
    tmp_path: Path, monkeypatch, operation: str,
) -> None:
    scenario = Scenario(tmp_path / 'scenario', DeliveryKind.TICKET)
    held = advance_to_effect(scenario, monkeypatch, operation, sent=True)
    effect = held.effect
    before_refs = scenario.port.physical.refs()
    binary = tmp_path / 'bin'
    binary.mkdir()
    fake = binary / 'gh'
    fake.write_text('#!' + sys.executable + '\n' + FAKE_GH.replace(
        'example/project', effect.repository).replace('publisher', effect.actor))
    fake.chmod(0o700)
    state, calls = tmp_path / 'github.json', tmp_path / 'calls.jsonl'
    state.write_text(json.dumps({'prs': [], 'comments': [], 'events': [], 'issue_state': 'open'}))
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('REMOTE_TEST_STATE', str(state))
    monkeypatch.setenv('REMOTE_TEST_CALLS', str(calls))
    monkeypatch.setenv('REMOTE_TEST_BARE', str(scenario.port.physical.remote))
    # This is the real subprocess adapter, without the Scenario API/Git ports.
    port = remote.RemoteEffects(scenario.port.git.root, effect.repository,
        runner_identity={'source': 'host'}, remote=str(scenario.port.physical.remote))
    ready, release, preparing_write = Event(), Event(), Event()
    paused = []
    write = github_commands.run_write_command
    capture = github_commands.capture_command
    physical_starts = []

    def record_capture(process, **kwargs):
        arguments = list(process.args)
        if arguments[:2] == ['git', 'push'] or '--method' in arguments:
            physical_starts.append(arguments)
        return capture(process, **kwargs)

    def tracked_write(*args, **kwargs):
        preparing_write.set()
        return write(*args, **kwargs)

    def synthetic_environment(_identity):
        # Replace only credential/environment preparation, not Popen, capture,
        # authorization or the real private Git/fake-gh command execution.
        if preparing_write.is_set() and not paused:
            paused.append(True)
            ready.set()
            assert release.wait(8), 'the test did not release environment preparation'
        names = ('PATH', 'HOME', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL',
                 'GIT_CONFIG_SYSTEM', 'REMOTE_TEST_STATE', 'REMOTE_TEST_CALLS', 'REMOTE_TEST_BARE')
        return {name: os.environ[name] for name in names if name in os.environ}

    monkeypatch.setattr(remote, 'run_write_command', tracked_write)
    monkeypatch.setattr(runner_transport, 'runner_environment', synthetic_environment)
    monkeypatch.setattr(github_commands, 'capture_command', record_capture)
    refused = []

    def execute():
        try:
            return port.execute(effect, held.authorize)
        except (KernelError, ReadWait) as error:
            refused.append(error)
            return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            assert ready.wait(8), 'the real write transport never prepared its environment'
            scenario.kernel.control('stop', 'stop-during-command-prepare')
            assert scenario.kernel.status(effect.subject)['stopped']
        finally:
            release.set()
        future.result(timeout=8)
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not [args for args in requests if '--method' in args]
    assert json.loads(state.read_text())['prs'] == []
    assert scenario.port.physical.refs() == before_refs
    assert physical_starts == []
    assert len(refused) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects WHERE id=?', (effect.effect_id,))) == ('sent', 1)
