"""共享测试支撑：executor；每次调用保持独立状态。"""

from __future__ import annotations


_GATED_ACTION = """
import os, sys
from agent_run.cli import main
from agent_run.run_driver import RunDriver
from agent_run.task_control import TaskControlStore
ready, release = int(sys.argv[1]), int(sys.argv[2])
def barrier():
    os.write(ready, b'1')
    os.close(ready)
    assert os.read(release, 1) == b'1'
    os.close(release)
if sys.argv[4] == 'approve':
    original = TaskControlStore.complete_action
    def complete(self, *args, **kwargs):
        if sys.argv[3] == 'unresolved':
            barrier()
        result = original(self, *args, **kwargs)
        if sys.argv[3] != 'unresolved':
            barrier()
        return result
    TaskControlStore.complete_action = complete
elif sys.argv[3] == 'unresolved':
    original = TaskControlStore.complete_action
    def complete(self, *args, **kwargs):
        barrier()
        return original(self, *args, **kwargs)
    TaskControlStore.complete_action = complete
else:
    original = RunDriver.advance
    def advance(self, *args, **kwargs):
        barrier()
        return original(self, *args, **kwargs)
    RunDriver.advance = advance
raise SystemExit(main(sys.argv[4:]))
"""
