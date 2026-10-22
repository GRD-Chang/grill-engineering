"""Real copied authorities return their own marker around normal process exit."""
from pathlib import Path
from contextlib import closing
import json
import os
import select
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import migration_cutover as cutover
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.model import HostObservation, Profile
from agent_run_kernel.task_location import locked_task, read_task
from test_kernel_migration import original_v6
from test_kernel_migration_cutover import _WORKSPACE_CHILD, _continue_with_original_02


@pytest.fixture
def copied_authority(tmp_path, git_repo, original_v6):
    root = tmp_path / 'source-state'
    fixture = Path(original_v6('fixture', root=root, fixture_path=git_repo / 'github.json', issues={'2': {
        'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
        'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}}))
    state = original_v6('start', root=root, git=git_repo, fixture=fixture)['state']
    snapshot = capture_v6(root / 'state.sqlite3', tmp_path / 'sealed.sqlite3')
    private = tmp_path / 'private'
    private.mkdir()
    target_root = private / 'repo'
    source = LocalGit(git_repo)
    source.run('clone', '--no-hardlinks', str(git_repo), str(target_root))
    git = LocalGit(target_root)
    for name in ('user.name', 'user.email'):
        git.run('config', name, source.run('config', '--get', name).stdout.strip())
    git.run('update-ref', 'refs/heads/' + state['run_branch'], state['base']['sha'])
    work = private / 'work'
    work.mkdir()
    options = dict(repository_root=target_root, checkout=work / 'ticket-2', final_checkout=work / 'final',
        source_ref='refs/heads/copied-ticket-2', development_profile=Profile('fixture-development', 'high'),
        review_profile=Profile('fixture-review', 'high'), publication_profile=Profile('fixture-publication', 'high'))
    return snapshot, options, git, private / 'kernel.sqlite3', state['base']['sha']


def _settled_copy(copied_authority, workspace):
    snapshot, options, git, target, base = copied_authority
    git.run('worktree', 'add', '--detach', str(options['checkout']), base)
    kernel = convert_initial_v6(snapshot, target, **options)
    private = target.parent
    carrier = private / 'carrier'
    carrier.write_text('Synthetic actual child only')
    if workspace:
        command = (sys.executable, '-u', '-c', _WORKSPACE_CHILD, str(target), 'copied-handoff',
                   str(Path(__file__).resolve().parents[1] / 'src'), 'confirmed')
    else:
        command = (sys.executable, '-u', '-c',
                   "import sys; sys.stdin.buffer.readline(); print('ready',flush=True); sys.stdin.buffer.readline()")
    old = private / 'old/state.sqlite3'
    nonce, launch = cutover.cutover_idle_copied_v6(snapshot, old, kernel, private_root=private,
        action_id='copied-handoff', command=command, environment_carrier=carrier, preflight=lambda: None)
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    try:
        stat = Path(f'/proc/{child.pid}/stat').read_text()
        token = stat[stat.rfind(')') + 2:].split()[19]
        cgroup = Path(f'/proc/{child.pid}/cgroup').read_text().splitlines()[0].split(':', 2)[2]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        invocation = f'copied-handoff-{child.pid}-{token}'
        assert kernel.observe_host(launch, HostObservation('running', launch.unit_name, invocation,
            cgroup, boot, child.pid, token, evidence='actual child', usage_ready=True)) == 'running'
        child.stdin.write(b'go\n'); child.stdin.flush()
        assert select.select([child.stdout], [], [], 10)[0], 'actual child did not finish'
        observed = child.stdout.readline()
        assert observed, child.stderr.read().decode()
        if workspace:
            assert json.loads(observed)['head'] == base
        else:
            assert observed == b'ready\n'
        kernel.control('stop', 'copied-handoff-stop')
    finally:
        child.stdin.close()
        try:
            code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=10)
            raise
    assert code == 0, child.stderr.read().decode()
    assert kernel.observe_host(launch, HostObservation('exited', launch.unit_name, invocation,
        cgroup, boot, child.pid, token, code, time.time(), 'actual child reaped', usage_released=True)) == 'exited'
    return snapshot, options, git, kernel, old, nonce


@pytest.mark.parametrize('workspace,checkpoint', [(False, None), (False, 'before'), (False, 'after'),
                                                (True, 'before'), (True, 'after')])
def test_copied_marker_handoff_and_reentry_preserve_source_and_original_resume(
        copied_authority, tmp_path, workspace, checkpoint):
    snapshot, options, git, kernel, old, nonce = _settled_copy(copied_authority, workspace)
    before = snapshot.source.read_bytes(), snapshot.path.read_bytes()
    with closing(sqlite3.connect(snapshot.path)) as db:
        run_id, source_uuid = db.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchone()
    marker = git.root / '.git/agent-run-checkout-id'
    initial = marker.stat()
    copy_uuid = marker.read_text().strip()
    assert copy_uuid != source_uuid
    restore = cutover.restore_copied_v6_after_workspace if workspace else cutover.restore_idle_copied_v6_without_new_effects
    task = TaskKey(git.root, 'example/project', 1)
    if checkpoint is not None:
        fenced = old.read_bytes()
        process = os.fork()
        if process == 0:
            actual = cutover._handoff_copy_marker
            def interrupted(*args):
                if checkpoint == 'before':
                    os._exit(73)
                actual(*args)
                os._exit(74)
            cutover._handoff_copy_marker = interrupted
            try:
                restore(snapshot, old, kernel, nonce=nonce)
            except BaseException:
                import traceback
                (tmp_path / 'inverse-child-trace.txt').write_text(traceback.format_exc())
                os._exit(75)
            os._exit(76)
        _, status = os.waitpid(process, 0)
        assert os.waitstatus_to_exitcode(status) == (73 if checkpoint == 'before' else 74), (
            (tmp_path / 'inverse-child-trace.txt').read_text() if (tmp_path / 'inverse-child-trace.txt').exists() else status)
        assert old.read_bytes() == fenced
        with locked_task(task) as locator:
            assert read_task(locator) == (run_id, old)
        assert marker.read_text().strip() == (copy_uuid if checkpoint == 'before' else source_uuid)
    assert restore(snapshot, old, kernel, nonce=nonce) == old
    assert old.read_bytes() == snapshot.path.read_bytes()
    assert marker.read_text().strip() == source_uuid
    assert (marker.stat().st_dev, marker.stat().st_ino) == (initial.st_dev, initial.st_ino)
    _continue_with_original_02(tmp_path, git.root, old, run_id)
    advanced = old.read_bytes()
    with pytest.raises(MigrationRefused):
        restore(snapshot, old, kernel, nonce=nonce)
    assert old.read_bytes() == advanced
    assert (snapshot.source.read_bytes(), snapshot.path.read_bytes()) == before
