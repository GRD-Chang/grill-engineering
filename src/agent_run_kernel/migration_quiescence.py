"""Read physical v6 Task/Host facts without importing its control runtime.

This first source-transfer unit supports a reaped local Host. A systemd Runner
binding requires independent manager evidence and remains unsupported here.
No observation kills a process, reconciles an Action, or creates authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3

from agent_run.task_identity import TaskKey

from .migration import MigrationRefused


CONTROL_TABLES = frozenset({
    'control_tasks', 'control_actions', 'control_executors', 'control_workers',
    'control_action_history', 'control_payloads',
})


def _absent(pid: object, token: object) -> None:
    if (type(pid) is not int or pid <= 0 or not isinstance(token, str)
            or re.fullmatch(r'[0-9]+', token) is None):
        raise MigrationRefused('source_host_exit_unproved')
    try:
        raw = Path(f'/proc/{pid}/stat').read_text(encoding='ascii')
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise MigrationRefused('source_host_exit_unknown') from error
        raise MigrationRefused('source_host_exit_unknown')
    except (OSError, UnicodeError) as error:
        raise MigrationRefused('source_host_exit_unknown') from error
    closing = raw.rfind(')')
    fields = raw[closing + 2:].split() if closing >= 0 else []
    if len(fields) <= 19 or not fields[19].isdecimal():
        raise MigrationRefused('source_host_exit_unknown')
    if fields[19] == token:
        # Even a zombie has not supplied the required reaped/absent proof.
        raise MigrationRefused('source_host_still_live')


def read_v6_host_exit(db: sqlite3.Connection, run_id: str, *, required: bool = False) -> TaskKey | None:
    """Verify every retained control identity; return only the original TaskKey.

    The caller supplies a private read-only copy, outside any source SQLite
    transaction. The formal cutover repeats these observations while holding
    both Task locks. Historical failed/control targets are never discarded.
    """
    tasks = db.execute('SELECT * FROM control_tasks').fetchmany(2)
    if not tasks:
        if required or any(db.execute(f'SELECT 1 FROM {name} LIMIT 1').fetchone()
                           for name in CONTROL_TABLES - {'control_tasks'}):
            raise MigrationRefused('source_host_exit_unproved')
        return None
    if len(tasks) != 1:
        raise MigrationRefused('source_task_scope')
    task = tasks[0]
    run = db.execute('SELECT repository,parent_number FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None or not isinstance(task['workspace'], str):
        raise MigrationRefused('source_task_identity')
    workspace = Path(task['workspace'])
    if not workspace.is_absolute() or workspace.resolve() != workspace:
        raise MigrationRefused('source_task_identity')
    identity = TaskKey(workspace, run['repository'], run['parent_number'])
    if (task['protocol'] != 1 or task['task_id'] != identity.fingerprint
            or task['repository'] != identity.repository
            or task['parent_number'] != identity.parent_number or task['run_id'] != run_id
            or not isinstance(task['run_state_dir'], str)
            or not Path(task['run_state_dir']).is_absolute()
            or Path(task['run_state_dir']).resolve() != Path(task['run_state_dir'])):
        raise MigrationRefused('source_task_identity')
    actions = db.execute('SELECT * FROM control_actions').fetchall()
    executors = db.execute('SELECT * FROM control_executors').fetchall()
    if not actions or not executors:
        raise MigrationRefused('source_host_exit_unproved')
    action_ids = {row['action_id'] for row in actions}
    executor_ids = {(row['action_id'], row['generation']) for row in executors}
    if (task['current_action_id'] not in action_ids
            or (task['current_executor_action_id'], task['current_executor_generation']) not in executor_ids):
        raise MigrationRefused('source_task_identity')
    for action in actions:
        if (action['task_id'] != identity.fingerprint or action['run_id'] != run_id
                or action['status'] != 'completed' or not action['completed_at']
                or action['application_observed'] != 1
                or action['target_executor_action_id'] is not None
                or action['target_executor_generation'] is not None):
            raise MigrationRefused('source_control_unsettled')
    for executor in executors:
        if (executor['task_id'] != identity.fingerprint or executor['run_id'] != run_id
                or executor['action_id'] not in action_ids
                or executor['status'] not in {'exited', 'absent'} or not executor['exited_at']
                or not executor['handshake_at']
                or not isinstance(executor['binding_token'], str)
                or re.fullmatch(r'[0-9a-f]{32}', executor['binding_token']) is None):
            raise MigrationRefused('source_host_exit_unproved')
        if executor['runner_binding'] is not None:
            raise MigrationRefused('source_systemd_exit_unproved')
        _absent(executor['pid'], executor['process_start_token'])
    for worker in db.execute('SELECT * FROM control_workers'):
        if (worker['task_id'] != identity.fingerprint
                or (worker['action_id'], worker['generation']) not in executor_ids):
            raise MigrationRefused('source_task_identity')
        _absent(worker['pid'], worker['process_start_token'])
    for witness in db.execute('SELECT * FROM control_action_history'):
        if (witness['task_id'] != identity.fingerprint or witness['run_id'] != run_id
                or witness['state_dir'] != task['run_state_dir']):
            raise MigrationRefused('source_task_scope')
    if db.execute('SELECT 1 FROM control_payloads WHERE task_id!=? LIMIT 1',
                  (identity.fingerprint,)).fetchone():
        raise MigrationRefused('source_task_scope')
    return identity
