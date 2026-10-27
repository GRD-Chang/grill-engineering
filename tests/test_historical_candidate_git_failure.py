"""Historical fault regression; synthetic providers and private local Git only."""
from support.core_operations import prepare_scripted_agent
import json
from pathlib import Path

import pytest

def test_crlf_conflict_repair_reaches_a_recoverable_current_work_boundary(tmp_path, monkeypatch):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind
    from agent_run.errors import KernelError
    scenario = Scenario(tmp_path/'scenario',DeliveryKind.TICKET,edits=(
        {'shared.txt':'Candidate content\n'},
        {'shared.txt':'Resolved complete content\n','new.csv':'\ufeffa,b\r\n1,2\r\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase']=='integration':break
        scenario.step()
    else:raise AssertionError('Original reviewed candidate did not reach integration')
    scenario.port.external_append(scenario.delivery.target_ref,{'shared.txt':'External content\n'},'conflict')
    try:
        end = scenario.run(approve=True)
    except KernelError as error:
        status=scenario.kernel.status(scenario.delivery.key)
        result={'code':error.code,'reason_contains_cached_diff_check':"'--cached', '--check'" in str(error),
                'reason_contains_csv_trailing_whitespace':'new.csv' in str(error) and 'trailing whitespace' in str(error),
                'phase':status['phase'],'generation':status['generation'],
                'agent_roles':scenario.role_calls,'network_requests':0,'real_git':True}
        (tmp_path/'crlf-conflict-observation.json').write_text(json.dumps(result,indent=2)+'\n')
        pytest.fail('Real CRLF conflict repair escapes normal progression: '+error.code)
    assert end.kind in {'complete','paused'}, end
    assert end.kind == 'complete'
    assert scenario.kernel.status(scenario.delivery.key)['resource_cleanup']['state'] == 'complete'


def _candidate(git_repo, tmp_path):
    from dataclasses import replace
    from support.remote_effects import publication
    git, local, seed = publication(git_repo, tmp_path)
    return git, local, replace(seed, operation='candidate', effect_id='content-check',
                              payload={**seed.payload, 'parents': [seed.before_sha]})


@pytest.mark.parametrize('content', [b'a,b\n1,2\n', b'\xef\xbb\xbfa,b\r\n1,2\r\n'])
def test_candidate_preserves_line_endings_on_commit_and_readback(git_repo, tmp_path, content):
    from agent_run.contracts.write_authority import WriteAuthority
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'values.csv').write_bytes(content)
    admitted = []
    result = local.execute(effect, WriteAuthority(lambda: lambda: admitted.append(True)))
    assert result.disposition == 'target' and result.commit is not None and admitted
    assert git.parents(result.commit.sha) == (effect.before_sha,)
    assert git.run('show', result.commit.sha+':values.csv').stdout.encode('utf-8') == content
    assert local.reconcile(effect) == result
    def no_new_authority():
        pytest.fail('An existing committed candidate must be read back without another admission')
    assert local.execute(effect, WriteAuthority(no_new_authority)) == result


@pytest.mark.parametrize('content,diagnostic', [
    (b'a,b \r\n', 'trailing whitespace'),
    (b'<<<<<<< ours\nleft\n=======\nright\n>>>>>>> theirs\n', 'leftover conflict marker'),
])
def test_new_candidate_validation_returns_failure_without_committing(git_repo, tmp_path, content, diagnostic):
    from agent_run.contracts.write_authority import WriteAuthority
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'invalid.txt').write_bytes(content)  # Previously invisible to unstaged diff.
    admitted = []
    result = local.execute(effect, WriteAuthority(lambda: lambda: admitted.append(True)))
    assert result.disposition == 'failure' and admitted
    assert result.raw['returncode'] != 0 and diagnostic in result.raw['stdout']
    assert '--cached' in result.raw['command'] and '--check' in result.raw['command']
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == effect.before_sha
    assert (checkout/'invalid.txt').read_bytes() == content


@pytest.mark.parametrize('content', [b'bad \n', b'<<<<<<< ours\n=======\n>>>>>>> theirs\n'])
def test_existing_candidate_identity_cannot_bypass_content_validation(git_repo, tmp_path, content):
    from agent_run.contracts.write_authority import WriteAuthority
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'invalid.txt').write_bytes(content)
    git.run('add', '--all', cwd=checkout)
    tree = git.run('write-tree', cwd=checkout).stdout.strip()
    committed = git.make_commit(tree, (effect.before_sha,), 'candidate '+effect.effect_id)
    git.run('reset', '--hard', committed.sha, cwd=checkout)
    result = local.reconcile(effect)
    assert result.disposition == 'failure'
    assert result.commit == committed and result.raw['returncode'] != 0
    assert effect.before_sha in result.raw['command'] and committed.sha in result.raw['command']
    def no_new_authority():
        pytest.fail('An existing invalid candidate cannot obtain first-dispatch authority')
    assert local.execute(effect, WriteAuthority(no_new_authority)) == result


def test_candidate_real_git_failure_preserves_command_and_current_head(git_repo, tmp_path):
    from agent_run.contracts.write_authority import WriteAuthority
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'new.txt').write_text('accepted content\n')
    git_dir = Path(git.run('rev-parse', '--absolute-git-dir', cwd=checkout).stdout.strip())
    lock = git_dir/'index.lock'
    lock.write_text('Another local Git operation holds the index.\n')
    admitted = []
    try:
        result = local.execute(effect, WriteAuthority(lambda: lambda: admitted.append(True)))
    finally:
        lock.unlink()
    assert result.disposition == 'failure' and admitted
    assert result.raw['returncode'] != 0 and 'index.lock' in result.raw['stderr']
    assert list(result.raw['command'][-2:]) == ['add', '--all']
    assert result.commit is not None and result.commit.sha == effect.before_sha


def test_git_failure_cannot_adopt_a_foreign_head_as_repair_authority(git_repo, tmp_path, monkeypatch):
    from agent_run.adapters.git_objects import LocalGit
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.errors import KernelError
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'new.txt').write_text('accepted content\n')
    foreign = git.make_commit(effect.base.tree, (), 'Independent foreign root')
    original = LocalGit.candidate
    lock = Path(git.run('rev-parse', '--absolute-git-dir', cwd=checkout).stdout.strip())/'index.lock'
    lock.write_text('Another local Git operation holds the index.\n')
    # The independent actor releases its index lock before changing HEAD.
    def changing_candidate(writer, *args):
        try:
            return original(writer, *args)
        except KernelError:
            lock.unlink()
            git.run('reset', '--hard', foreign.sha, cwd=checkout)
            raise
    monkeypatch.setattr(LocalGit, 'candidate', changing_candidate)
    try:
        result = local.execute(effect, WriteAuthority(lambda: lambda: None))
    finally:
        lock.unlink(missing_ok=True)
    assert result.disposition == 'third' and result.commit is None
    assert result.raw['command_failure']['returncode'] != 0
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == foreign.sha


@pytest.mark.parametrize('code', ['automatic_step_paused', 'executor_fenced'])
@pytest.mark.parametrize('name', ['new.txt', 'README.md'])
def test_candidate_git_failure_handling_does_not_swallow_final_authority_refusal(git_repo, tmp_path, code, name):
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.errors import KernelError
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/name).write_text('invalid trailing whitespace \n')
    def denied():
        raise KernelError(code)
    with pytest.raises(KernelError, match=code):
        local.execute(effect, WriteAuthority(lambda: denied))
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == effect.before_sha
    assert git.run('diff', '--cached', '--name-only', cwd=checkout).stdout == ''


@pytest.mark.parametrize('name', ['new.txt', 'README.md'])
def test_candidate_failure_uses_existing_development_repair_and_budget(tmp_path, name):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind, Role
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.TICKET, edits=(
        {name:'invalid trailing space \n'}, {name:'repaired content\n'}))
    runner = scenario.runner()
    repaired = None
    for _ in range(80):
        state = scenario.kernel.status(scenario.delivery.key)
        if (state['phase'] == 'complete' and state['resource_cleanup'] is not None
                and state['resource_cleanup']['state'] == 'complete'):
            break
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        if step.call is None:
            assert step.kind not in {'paused', 'waiting'}, step
            if step.kind == 'complete':
                break
            continue
        call = step.call
        if call.role == Role.DEVELOPMENT and call.context.get('repair_source') == 'git_integrity':
            repaired = call
            evidence = call.context['git_integrity_evidence']
            assert evidence['disposition'] == 'failure' and evidence['raw']['returncode'] != 0
            assert 'trailing whitespace' in evidence['raw']['stdout']
            assert '--check' in evidence['raw']['command']
            assert call.purpose == 'requirements' and call.context['generation'] == 1
        runner.authorize_agent(call)
        scenario.kernel.receive(call, scenario.agent(call))
    else:
        raise AssertionError('Candidate failure did not finish through the existing repair loop')
    assert repaired is not None
    status = scenario.kernel.status(scenario.delivery.key)
    assert status['phase'] == 'complete' and status['generation'] == 1
    assert status['budgets']['requirements']['developments_used'] == 2


def test_tracked_content_validation_admits_the_check_without_staging_or_committing(git_repo, tmp_path):
    from agent_run.contracts.write_authority import WriteAuthority
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'README.md').write_text('Tracked invalid whitespace \n')
    admitted = []
    def prepare():
        admitted.append('prepare')
        return lambda: admitted.append('final')
    before = git.run('write-tree', cwd=checkout).stdout.strip()
    for _ in range(2):
        result = local.execute(effect, WriteAuthority(prepare))
        assert result.disposition == 'failure' and result.raw['returncode'] != 0
    assert admitted == ['prepare', 'final', 'prepare', 'final']
    assert git.run('write-tree', cwd=checkout).stdout.strip() == before
    assert git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == effect.before_sha


@pytest.mark.parametrize('change', ['foreign', 'unavailable'])
def test_tracked_candidate_failure_rechecks_identity_after_slow_preparation(git_repo, tmp_path, change):
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.errors import KernelError
    git, local, effect = _candidate(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    (checkout/'README.md').write_text('Tracked invalid whitespace \n')
    admitted = []
    def prepare():
        if change == 'foreign':
            foreign = git.make_commit(effect.base.tree, (), 'Independent foreign root')
            git.run('reset', '--hard', foreign.sha, cwd=checkout)
        else:
            checkout.rename(checkout.with_name('independently-moved'))
        return lambda: admitted.append(True)
    with pytest.raises(KernelError, match='local_effect_boundary_changed'):
        local.execute(effect, WriteAuthority(prepare))
    assert admitted == []


def test_public_stop_during_tracked_candidate_validation_never_dispatches(tmp_path, monkeypatch):
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.model import DeliveryKind
    from support.offline import Scenario
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.TICKET, edits=({'README.md':'Invalid trailing space \n'},))
    execute = scenario.port.execute
    stopped = []
    final_returned = []
    def stop_before_final(effect, authority):
        if effect.operation != 'candidate':
            return execute(effect, authority)
        def prepare():
            final = authority.prepare()
            scenario.kernel.control('stop', 'stop-before-candidate-validation')
            stopped.append(effect.effect_id)
            def admit():
                final()
                final_returned.append(effect.effect_id)
            return admit
        return execute(effect, WriteAuthority(prepare))
    monkeypatch.setattr(scenario.port, 'execute', stop_before_final)
    step = scenario.run()
    assert step.kind == 'paused' and stopped and final_returned == []
    assert scenario.kernel.status(scenario.delivery.key)['stopped']
    with scenario.kernel.storage.transaction(write=False) as tx:
        effect = tx.one('SELECT status,attempts,receipt_id FROM effects WHERE id=?', (stopped[0],))
        assert effect['attempts'] == 0 and effect['receipt_id'] is None
        assert effect['status'] not in {'sent', 'observed', 'confirmed'}
    checkout = Path(scenario.delivery.checkout)
    assert scenario.port.git.run('rev-parse', 'HEAD', cwd=checkout).stdout.strip() == scenario.initial.sha
    assert scenario.port.git.run('diff', '--cached', '--name-only', cwd=checkout).stdout == ''
