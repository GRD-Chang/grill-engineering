"""A v6 Task owner cannot be bypassed by native Task admission."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import select
import subprocess
import sys
import tarfile
from typing import Iterator

import pytest

from agent_run.task_identity import TaskKey
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Requirements, RunSpec
from agent_run_kernel.task_location import locked_task, read_task
from test_kernel_requirements import independent_source


@pytest.fixture(scope="module")
def unchanged_v6_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("unchanged-v6-admission")
    archive = subprocess.run(
        ["git", "archive", "02b008e43386a73c4510250732a7216d7dba17a5", "project/src"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as contents:
        for member in contents:
            if not member.isfile():
                continue
            relative = Path(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = contents.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
    return root / "project" / "src"


_OLD_OWNER = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_run.task_control import TaskControlStore
from agent_run.task_identity import TaskKey
from agent_run.run_locator import RunLocatorIndex
root, state = Path(sys.argv[2]), Path(sys.argv[3])
task = TaskKey(root, 'example/project', 1)
control = TaskControlStore(state)
claim = control.claim_action(task, kind='run', payload={'parent': 1})
reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
pid = os.getpid()
stat = Path('/proc/self/stat').read_text()
token = stat[stat.rfind(')') + 2:].split()[19]
record = control.mark_handshake(task, action_id=claim.action_id,
    generation=reservation.generation, pid=pid, process_start_token=token)
RunLocatorIndex.default().register(run_id='legacy-original', repository_root=root,
    state_dir=state, repository='example/project', parent_number=1)
assert record['executor']['status'] == 'running'
print(json.dumps({'pid': pid, 'action': claim.action_id, 'generation': reservation.generation}), flush=True)
if sys.argv[4] == 'live':
    sys.stdin.buffer.read()
# An abrupt owner exit deliberately leaves its original durable ownership
# unresolved. The native caller may not turn process absence into an exit fact.
"""


@contextmanager
def old_owner(source: Path, workspace: Path, state: Path, disposition: str) -> Iterator[dict]:
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _OLD_OWNER, str(source), str(workspace), str(state), disposition],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 15)[0], "Original v6 owner did not become ready"
        line = child.stdout.readline()
        if not line:
            _, error = child.communicate(timeout=5)
            raise AssertionError(error)
        facts = json.loads(line)
        if disposition == "unknown":
            assert child.wait(timeout=5) == 0
        else:
            assert child.poll() is None
        yield facts
    finally:
        if child.stdin is not None:
            child.stdin.close()
            child.stdin = None
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def native_spec(workspace: Path) -> tuple[RunSpec, Requirements]:
    requirements = independent_source()
    base = Commit("base", "base-tree")
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "untrusted", base,
        f"refs/heads/native-ticket-{number}", "refs/heads/native-run")
        for number in requirements.parent.sub_issue_numbers)
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "untrusted", base,
                     "refs/heads/native-run", "refs/heads/main")
    return RunSpec("new-native", "example/project", 1, "untrusted", "untrusted", (*deliveries, final),
                   repository_root=str(workspace)), requirements


@pytest.mark.parametrize("disposition", ["live", "unknown"])
@pytest.mark.parametrize("entry", ["open", "create", "admit"])
def test_native_admission_refuses_the_original_v6_task_owner(
    tmp_path: Path, unchanged_v6_source: Path, disposition: str, entry: str,
) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    state = tmp_path / "legacy-state"
    spec, requirements = native_spec(workspace)
    destination = tmp_path / "native.sqlite3"
    kernel = Kernel.create(destination, spec, requirements=requirements) if entry == "admit" else None
    task = TaskKey(workspace, spec.repository, spec.parent_issue)
    with old_owner(unchanged_v6_source, workspace, state, disposition) as original:
        before = hashlib.sha256((state / "state.sqlite3").read_bytes()).hexdigest()
        with pytest.raises(KernelError, match="unsupported_state") as refused:
            if entry == "open":
                Kernel.open_task(task)
            elif entry == "create":
                Kernel.create_task(destination, spec, requirements=requirements)
            else:
                assert kernel is not None
                kernel.admit_host("native-start", kind="run", command=("native-host",),
                    environment_carrier=tmp_path / "carrier", preflight=lambda: None)
        assert refused.value.code == "unsupported_state"
        assert refused.value.protocol == "sqlite-work-records-v6"
        assert refused.value.path == (state / "state.sqlite3").absolute()
        assert hashlib.sha256((state / "state.sqlite3").read_bytes()).hexdigest() == before
        with locked_task(task) as location:
            assert read_task(location) is None
        if kernel is not None:
            assert kernel.active_host_action() is None
            assert kernel.source_status()["stopped"] is False
        else:
            assert not destination.exists()
        assert original["pid"] > 0 and original["generation"] == 1
