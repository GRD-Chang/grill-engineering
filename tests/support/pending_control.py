"""观察原控制提交后再释放测试屏障；CLI 继续等待真实执行结果。"""
from contextlib import contextmanager
import json
import subprocess
import sys
import time

from support.process_scope import cleanup_owned_process
from support.worker_process_timing import wait_until


@contextmanager
def pending_public_control(*arguments, cwd, environment, timeout):
    """提交观察与 CLI 退出共用原超时；失败仅回收本次调用进程。"""
    deadline = time.monotonic() + timeout

    def remaining():
        return max(0, deadline - time.monotonic())

    with subprocess.Popen(
        [sys.executable, '-m', 'agent_run', *arguments,
         '--repo', 'example/project', '--json'],
        cwd=cwd, env=environment, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        try:
            yield process, remaining
        finally:
            if process.poll() is None:
                cleanup_owned_process(process)


def committed_control(kernel, process, kind, *, timeout):
    """只读等待本例唯一指定类型的原始 control_applied 回执。"""
    def recorded():
        with kernel.storage.transaction(write=False) as tx:
            rows = tx.rows(
                "SELECT a.*,h.subject AS material_owner FROM actions a JOIN history h "
                "ON h.run_id=a.run_id AND h.identity=a.id AND h.kind='control_applied' "
                "AND h.material_id=a.receipt_id WHERE a.run_id=? AND a.kind=?",
                (kernel.storage.run_id, kind),
            )
            if not rows:
                assert process.poll() is None, 'CLI exited before committing its control'
                return None
            action, = rows
            receipt = json.loads(tx.get(action['receipt_id'], action['material_owner']))
            assert receipt['action'] == action['id'] and receipt['kind'] == kind
            assert receipt['applied'] is True
            return dict(action)

    action = wait_until(recorded, timeout=timeout)
    assert process.poll() is None, 'CLI returned while the executor was still held'
    return action
