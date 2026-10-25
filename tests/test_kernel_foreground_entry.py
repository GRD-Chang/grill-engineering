"""Installed foreground entry with existing local Git/GitHub/Codex fixtures.

These two physical stories require the host's normal bubblewrap capability.
They never contact a real account or modify host service/security settings.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_run.adapters.foreground import ProcessPin, read_record, subreaper
from agent_run.core.kernel import Kernel
from agent_run.runtime.package_lease import default_runner_lock_path, runner_management_lease
from test_kernel_managed_entry_end_to_end import _CODEX, _control_environment, _git
from agent_run.adapters.workspace import ManagedWorkspace


@pytest.mark.parametrize('stop_first', [False, True], ids=['normal', 'stop-resume'])
def test_installed_foreground_parent_only_public_lifecycle(git_repo, tmp_path, monkeypatch, stop_first):
    ready = tmp_path / 'development-ready'
    released = tmp_path / 'development-released'
    codex = _CODEX
    if stop_first:
        codex = codex.replace("if role == 'development':", """if role == 'development':
    import time
    ready, released = Path(os.environ['FG_TEST_READY']), Path(os.environ['FG_TEST_RELEASED'])
    if not ready.exists():
        ready.write_text('Original development reached its controlled boundary')
        deadline = time.monotonic() + 60
        while not released.exists():
            if time.monotonic() >= deadline:raise RuntimeError('Foreground test release expired')
            time.sleep(.02)
""")
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=())
    environment.update(FG_TEST_READY=str(ready), FG_TEST_RELEASED=str(released))
    lock = default_runner_lock_path(environment)
    children = []
    pins = []
    exited_handles = []

    def start(*arguments):
        child = subprocess.Popen([sys.executable, '-m', 'agent_run', *arguments,
            '--repo', 'example/project', '--json'], cwd=git_repo, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        return child

    def result(child):
        out, err = child.communicate(timeout=90)
        assert child.returncode == 0, (out, err)
        return json.loads(out)

    def public(*arguments):return result(start(*arguments))

    with subreaper():
        try:
            first = start('run', '1', '--foreground', '--no-notifications')
            if stop_first:
                deadline = time.monotonic() + 40
                while not ready.exists():
                    assert first.poll() is None and time.monotonic() < deadline
                    time.sleep(.02)
                stopped = public('stop', '1')
                assert stopped['status'] == 'action_applied'
                original = result(first)
                assert original['host'] == 'exited' and original['host_backend'] == 'foreground'
                released.write_text('Allow only a later admitted invocation')
                continued = public('resume', original['run_id'], '--foreground')
                assert continued['run_id'] == original['run_id'] and continued['host'] == 'exited'
            else:
                original = result(first)
            assert original['host_backend'] == 'foreground' and original['host_unit'] is None
            public('status', original['run_id'])
            # The CLI is the source of the selected Task; the DB is opened only
            # to inspect committed binding/raw/lease facts, never edited.
            workspace = ManagedWorkspace.for_repository('example/project')
            kernel = Kernel(workspace.state_root / original['run_id'] / 'kernel.sqlite3')
            assert kernel.host_request(original['action_id']).run_id == original['run_id']
            before = kernel.status('parent:1')
            assert before['phase'] == 'approval'
            approved = public('approve', original['run_id'], '--foreground')
            assert approved['host'] == 'exited' and approved['host_backend'] == 'foreground'
            after = kernel.status('parent:1')
            assert after['phase'] == 'complete' and after['resource_cleanup']['state'] == 'complete'
            assert after['budgets']['requirements'] == before['budgets']['requirements']
            with kernel.storage.transaction(write=False) as tx:
                assert tx.one('SELECT count(*) FROM leases')[0] == 0
                assert not tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0")
                assert not tx.rows("SELECT owner FROM hosts WHERE backend!='foreground' OR unit_name IS NOT NULL OR cgroup IS NOT NULL")
                assert tx.one('SELECT count(*) FROM attempts WHERE role=\'development\'')[0] == 1
            with runner_management_lease(lock):pass
            document = json.loads(state.read_text())
            assert document['source_graph']['parent']['state'] == 'CLOSED'
            assert _git(tmp_path / 'origin.git', 'show', 'refs/heads/main:managed-delivery.txt') == 'Implementation from the managed native Worker.'
            assert not list((lock.parent / 'foreground-hosts').iterdir())
        finally:
            # Retain only identities from this test's private launch records.
            root = lock.parent / 'foreground-hosts'
            cleanup_errors = []
            for path in root.glob('*.json'):
                try:record = read_record(path)
                except (OSError, RuntimeError, ValueError) as error:
                    cleanup_errors.append(str(error))
                    continue
                witnesses = list((record.get('domain') or {}).get(key) for key in ('init', 'executor'))
                for key in ('init', 'monitor'):
                    if record.get(key):witnesses.append(record[key])
                for witness in witnesses:
                    if witness:
                        try:pins.append(ProcessPin.open(witness['pid'], witness))
                        except RuntimeError:
                            # Namespace handles disappear for a dead child. An
                            # exact pidfd/start-token still permits reaping this
                            # test's recorded child; it is not a domain proof.
                            descriptor = -1
                            try:
                                descriptor = os.pidfd_open(witness['pid'])
                                fields = Path(f"/proc/{witness['pid']}/stat").read_text().rsplit(')', 1)[1].split()
                                if fields[19] == witness['start_token']:
                                    exited_handles.append(descriptor)
                                    descriptor = -1
                            except ProcessLookupError:pass
                            except FileNotFoundError:pass
                            except OSError as error:cleanup_errors.append(str(error))
                            finally:
                                if descriptor >= 0:os.close(descriptor)
            for child in children:
                try:
                    if child.poll() is None:child.kill()
                    child.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as error:cleanup_errors.append(str(error))
                finally:
                    if child.stdout:child.stdout.close()
                    if child.stderr:child.stderr.close()
            for pin in pins:
                try:
                    if pin.observe() != 'exited':pin.kill()
                    pin.wait_exit(5)
                    pin.reap()
                except (OSError, RuntimeError) as error:cleanup_errors.append(str(error))
                finally:pin.close()
            for descriptor in exited_handles:
                try:
                    observed = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
                    if observed is None:cleanup_errors.append('Recorded child did not reach reapable exit')
                except ChildProcessError:pass
                except OSError as error:cleanup_errors.append(str(error))
                finally:os.close(descriptor)
            if cleanup_errors:
                original = sys.exception()
                if original is not None:original.add_note('Owned foreground cleanup: ' + '; '.join(cleanup_errors))
                else:raise RuntimeError('Owned foreground cleanup: ' + '; '.join(cleanup_errors))
