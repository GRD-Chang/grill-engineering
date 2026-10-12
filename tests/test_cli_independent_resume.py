"""首次 resume 必须从持久请求定位 Run，不能继承观察者的内存闭包。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from support.cli import load_only_run_state, run_cli
from support.seeding import write_fixture
from support.workspace import managed_repo, managed_state

from support.state import snapshot_run, write_run_fixture


# 只替换外部进程 Host。子解释器执行生产 Executor 和完整 CLI，既不 fork
# 当前 Run 闭包，也不预填正式绑定；fixture Agent/GitHub 仍为产品既有边界。
_REENTER = r'''
import json, os, subprocess, sys
from dataclasses import replace
from pathlib import Path
from agent_run import cli
from agent_run.executor_environment import capture_executor_environment, write_environment_carrier
from agent_run.executor_host import FakeExecutorHost, BoundExecutorHost

class IndependentHost(FakeExecutorHost):
    def ensure(self, spec, control, execute=None, recover=False):
        reservation = control.begin_executor(spec.task, action_id=spec.action_id,
                                             run_id=spec.run_id, reclaim=recover)
        assert reservation.created
        record = control.load(spec.task)
        Path(os.environ['RESUME_SNAPSHOT']).write_text(json.dumps(record))
        spec = replace(spec, generation=reservation.generation)
        carrier = write_environment_carrier(Path(os.environ['RESUME_RUNTIME']), spec,
            capture_executor_environment(os.environ, command=spec.command))
        child = (
            'import os, sys; from agent_run import cli, executor; '
            'from agent_run.executor_host import BoundExecutorHost; '
            'cli.FakeExecutorHost = lambda **kwargs: BoundExecutorHost('
            'action_id=os.environ["AGENT_RUN_EXECUTOR_ACTION_ID"], '
            'generation=int(os.environ["AGENT_RUN_EXECUTOR_GENERATION"])); '
            'raise SystemExit(executor.main(sys.argv[1:]))'
        )
        result = subprocess.run([sys.executable, '-c', child,
            '--workspace', str(spec.task.workspace), '--repository', spec.task.repository,
            '--parent', str(spec.task.parent_number), '--action-id', spec.action_id,
            '--generation', str(spec.generation), '--state-root', str(spec.state_root),
            '--carrier', str(carrier), '--runner-lock', str(carrier.parent / 'runner.lock')],
            capture_output=True, text=True, timeout=30)
        assert not carrier.exists()
        assert result.returncode == 2, result.stdout + result.stderr
        return self.inspect(spec, control)

cli.FakeExecutorHost = IndependentHost
raise SystemExit(cli.main(sys.argv[1:]))
'''


@pytest.mark.parametrize("unknown_reason,new_thread,missing_material", [
    (False, False, None),
    (True, False, None),
    (True, True, None),
    (False, False, "timeline"),
    (True, True, "checkout"),
])
def test_first_public_resume_reenters_real_cli_in_independent_executor(
    git_repo: Path, tmp_path: Path, unknown_reason: bool, new_thread: bool,
    missing_material: str | None,
) -> None:
    fixture = write_fixture(git_repo / 'github.json', issues={})
    agents = git_repo / 'agents.json'
    agents.write_text(json.dumps({'developments': [
        {'thread_id': 'resume-worker', 'write_files': {'progress.txt': 'first\n'},
         'error_after_writes': 'controlled initial failure'},
        {'expected_thread_id': None if new_thread else 'resume-worker', 'thread_id': 'resumed-fresh' if new_thread else 'resume-worker',
         'write_files': {'resumed.txt': 'second worker started\n'},
         'error_after_writes': 'controlled resumed worker boundary'},
    ]}), encoding='utf-8')
    initial = run_cli(git_repo, fixture, 'run', '1', '--agent-fixture', str(agents))
    assert initial.returncode == 2, initial.stdout + initial.stderr
    script = json.loads(agents.read_text())
    script['developments'].pop(0)
    agents.write_text(json.dumps(script), encoding='utf-8')
    before = load_only_run_state(git_repo)
    assert before['status'] == 'execution_failed'
    run_id = before['run_id']
    if unknown_reason:
        before['status'] = 'blocked'
        before['parent_job'].update(phase='blocked', blocked_reason='unclassified_runtime_failure',
                                    execution_resume_phase='developing')
        from agent_run.state import StateStore
        StateStore(managed_state(git_repo)).save_run(run_id, before)
    history_before = before['agent_invocation_history']
    assert len(history_before) == 1
    control_file, = (managed_state(git_repo) / 'task-control').glob('*.json')
    old_control = json.loads(control_file.read_text())
    checkout = managed_state(git_repo) / 'worktrees' / run_id / 'parent'
    if missing_material is not None:
        # 缺损只来自本例故障注入；修复必须使用准确备份和真实 Git 身份，
        # 不能由 Controller 猜测历史或通过重置状态伪造后继启动。
        state_ref = (managed_state(git_repo), str(run_id))
        trusted_state = snapshot_run(state_ref)
        progress = (checkout / 'progress.txt').read_bytes()
        branch = subprocess.run(
            ['git', 'branch', '--show-current'], cwd=checkout,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=checkout,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if missing_material == 'timeline':
            damaged = json.loads(trusted_state)
            del damaged['timeline']
            write_run_fixture(state_ref, damaged)
        else:
            subprocess.run(['git', 'worktree', 'remove', '--force', str(checkout)],
                           cwd=managed_repo(git_repo), check=True, capture_output=True)
            assert not checkout.exists()
        state_bytes = snapshot_run(state_ref)
        control_bytes = {path.name: path.read_bytes()
                         for path in control_file.parent.glob('*.json')}
        rejected = run_cli(git_repo, fixture, 'resume', run_id,
                           '--agent-fixture', str(agents))
        assert rejected.returncode == 2, rejected.stdout + rejected.stderr
        diagnostic = rejected.stdout + rejected.stderr
        assert 'recovery_materials_missing' in diagnostic
        assert run_id in diagnostic
        assert any(word in diagnostic.lower() for word in ('恢复', '补', 'restore'))
        if missing_material == 'timeline':
            assert 'timeline' in diagnostic
        else:
            assert str(checkout) in diagnostic
            assert branch in diagnostic
            assert head in diagnostic
            assert not checkout.exists()
        assert snapshot_run(state_ref) == state_bytes
        assert {path.name: path.read_bytes()
                for path in control_file.parent.glob('*.json')} == control_bytes
        assert json.loads(agents.read_text()) == script
        if missing_material == 'timeline':
            write_run_fixture(state_ref, json.loads(trusted_state))
        else:
            subprocess.run(['git', 'worktree', 'add', str(checkout), branch],
                           cwd=managed_repo(git_repo), check=True, capture_output=True)
            (checkout / 'progress.txt').write_bytes(progress)
            assert subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=checkout,
                check=True, capture_output=True, text=True,
            ).stdout.strip() == head
            # 同名分支不足以证明补建身份：Agent 启动前拒绝错误 HEAD，
            # 保全人工补足内容，不让后续完整性修复 reset/clean 此现场。
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'controlled wrong reconstruction head'],
                           cwd=checkout, check=True, capture_output=True)
            wrong_head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                        check=True, capture_output=True, text=True).stdout.strip()
            wrong = run_cli(git_repo, fixture, 'resume', run_id, '--agent-fixture', str(agents))
            assert wrong.returncode == 2, wrong.stdout + wrong.stderr
            assert 'recovery_materials_missing' in wrong.stdout
            assert head in wrong.stdout
            assert snapshot_run(state_ref) == state_bytes
            assert {path.name: path.read_bytes()
                    for path in control_file.parent.glob('*.json')} == control_bytes
            assert json.loads(agents.read_text()) == script
            assert (checkout / 'progress.txt').read_bytes() == progress
            assert subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                  check=True, capture_output=True, text=True).stdout.strip() == wrong_head
            # 操作者在准确备份已保全的隔离测试现场修正提交边界。
            subprocess.run(['git', 'reset', '--hard', head], cwd=checkout,
                           check=True, capture_output=True)
    snapshot = tmp_path / 'resume-admitted.json'
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(Path(__file__).parents[1] / 'src'),
                       RESUME_SNAPSHOT=str(snapshot), RESUME_RUNTIME=str(tmp_path / 'runtime'))
    resumed = subprocess.run([sys.executable, '-c', _REENTER, 'resume', run_id,
                              '--github-fixture', str(fixture), '--agent-fixture', str(agents), '--json',
                              *(['--new-thread'] if new_thread else []),
                              *(['--message', '已修复环境，继续原工作。'] if unknown_reason else [])],
                             cwd=git_repo, env=environment, capture_output=True, text=True, timeout=40)
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert snapshot.exists(), resumed.stdout + resumed.stderr
    admitted = json.loads(snapshot.read_text())
    assert admitted['run_id'] is None
    assert admitted['action']['run_id'] is None
    assert admitted['executor']['run_id'] is None
    assert admitted['action']['payload']['run_id'] == run_id
    assert admitted['action']['action_id'] != old_control['action']['action_id']
    after = load_only_run_state(git_repo)
    control = json.loads(control_file.read_text())
    assert control['run_id'] == control['action']['run_id'] == control['executor']['run_id'] == run_id
    assert control['action']['application_observed'] is True
    assert control['executor']['generation'] > old_control['executor']['generation']
    assert control['executor']['status'] == 'exited'
    assert control['executor']['pid'] != old_control['executor']['pid']
    receipt = after['action_application_receipt']
    assert receipt['action_id'] == control['action']['action_id']
    assert receipt['kind'] == 'resume'
    assert receipt['executor_generation'] == control['executor']['generation']
    assert receipt['payload_digest'] == control['action']['payload_digest']
    assert not list((tmp_path / 'runtime').glob('environment-*'))
    assert not Path(f"/proc/{control['executor']['pid']}").exists()
    history = after['agent_invocation_history']
    assert len(history) == 2
    assert history[0] == history_before[0]
    assert after['timeline'][:len(before['timeline'])] == before['timeline']
    assert history[-1]['error'] == 'controlled resumed worker boundary'
    assert history[-1]['semantic_attempt']['attempt_id'] == history_before[0]['semantic_attempt']['attempt_id']
    assert history[-1]['work_subject'] == history_before[0]['work_subject']
    assert history[-1]['generation'] == history_before[0]['generation']
    assert history[-1]['requested_thread_id'] == (None if new_thread else 'resume-worker')
    if unknown_reason:
        assert after['parent_job']['human_response_history'][-1]['response'] == '已修复环境，继续原工作。'
        assert after['resume_audit']['history'][-1]['successor_invocation_started_at'] == history[-1]['started_at']
        assert after['parent_job']['pause_history'][-1]['blocked_reason'] == 'unclassified_runtime_failure'
    assert (checkout / 'progress.txt').read_text() == 'first\n'
    assert (checkout / 'resumed.txt').read_text() == 'second worker started\n'
    assert after['parent_job']['modification_attempts'] == before['parent_job']['modification_attempts']
    assert after['parent_job']['review_budget'] == before['parent_job']['review_budget']
