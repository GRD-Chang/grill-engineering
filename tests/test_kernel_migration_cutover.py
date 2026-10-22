"""One isolated copied-v6 cutover and effect-free return, using an actual local child."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import io
import tarfile
from typing import Any

import pytest

from agent_run.task_identity import TaskKey

from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.migration_cutover import (cutover_idle_copied_v6,
                                                 restore_idle_copied_v6_without_new_effects,
                                                 restore_copied_v6_after_workspace)
from agent_run_kernel.model import HostObservation, Profile
from agent_run_kernel.offline import OfflinePort
from agent_run_kernel.task_location import locked_task, read_task
from test_kernel_migration import original_v6


@pytest.mark.parametrize('mode', ['confirmed', 'sent'])
def test_current_workspace_child_uses_frozen_v6_source_and_real_runner(
    tmp_path: Path, git_repo: Path, original_v6: Any, mode: str,
) -> None:
    """An additional normal producer; never replaces a sealed fixed oracle."""
    private = tmp_path / 'private'
    private.mkdir()
    old_root = tmp_path / 'original-state'
    issue = {'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
             'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}
    fixture = Path(original_v6('fixture', root=old_root, fixture_path=git_repo / 'github.json',
                               issues={'2': issue}))
    state = original_v6('start', root=old_root, git=git_repo, fixture=fixture)['state']
    snapshot = capture_v6(old_root / 'state.sqlite3', tmp_path / 'sealed.sqlite3')
    # Source authority keeps its original UUID. The current copied transport
    # receives a new clone, not a borrowed legacy marker in the source clone.
    from agent_run_kernel.effects import LocalGit
    source_git = LocalGit(git_repo)
    copied = private / 'repository'
    source_git.run('clone', '--no-hardlinks', str(git_repo), str(copied))
    git_repo = copied
    git = LocalGit(git_repo)
    for name in ('user.name', 'user.email'):
        git.run('config', name, source_git.run('config', '--get', name).stdout.strip())
    git.run('update-ref', 'refs/heads/' + state['run_branch'], state['base']['sha'])
    assert not (copied / '.git/agent-run-checkout-id').exists()
    work = private / 'work'
    checkout = work / 'converted-ticket-2'
    work.mkdir(exist_ok=True)
    git.run('worktree', 'add', '--detach', str(checkout), state['base']['sha'])
    kernel = convert_initial_v6(snapshot, private / 'kernel.sqlite3', repository_root=git_repo,
        source_ref=f"refs/heads/agent-run/{state['run_id']}/converted-ticket-2", checkout=checkout,
        final_checkout=work / 'converted-final', development_profile=Profile('fixture-development', 'high'),
        review_profile=Profile('fixture-review', 'high'), publication_profile=Profile('fixture-publication', 'high'))
    carrier = private / 'carrier'
    carrier.write_text('synthetic local Host only')
    command = (sys.executable, '-u', '-c', _WORKSPACE_CHILD, str(kernel.storage.path),
               'normal-workspace-child', str(Path(__file__).resolve().parents[1] / 'src'), mode)
    old = private / 'old/state.sqlite3'
    nonce, launch = cutover_idle_copied_v6(snapshot, old, kernel, private_root=private,
        action_id='normal-workspace-child', command=command, environment_carrier=carrier, preflight=lambda: None)
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    try:
        stat = Path(f'/proc/{child.pid}/stat').read_text()
        token = stat[stat.rfind(')') + 2:].split()[19]
        cgroup = Path(f'/proc/{child.pid}/cgroup').read_text().splitlines()[0].split(':', 2)[2]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        invocation = f'normal-workspace-{child.pid}-{token}'
        assert kernel.observe_host(launch, HostObservation('running', launch.unit_name, invocation,
            cgroup, boot, child.pid, token, evidence='actual local child', usage_ready=True)) == 'running'
        child.stdin.write(b'go\n')
        child.stdin.flush()
        import select
        assert select.select([child.stdout], [], [], 10)[0], 'normal workspace child did not finish'
        line = child.stdout.readline()
        assert line, child.stderr.read().decode()
        observed = json.loads(line)
        assert observed['head'] == state['base']['sha']
        assert kernel.status('ticket:2')['phase'] == ('workspace' if mode == 'sent' else 'development')
        kernel.control('stop', 'stop-normal-workspace-child')
    finally:
        child.stdin.close()
        try:
            exit_code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            raise
    assert exit_code == 0, child.stderr.read().decode()
    assert kernel.observe_host(launch, HostObservation('exited', launch.unit_name, invocation,
        cgroup, boot, child.pid, token, 0, time.time(), 'local child reaped', usage_released=True)) == 'exited'
    if mode == 'sent':
        with kernel.storage.transaction(write=False) as tx:
            assert tuple(tx.one('SELECT id,status,attempts,receipt_id FROM effects')) == (observed['effect'], 'sent', 1, None)
        _readback_with_new_host(kernel, carrier, observed['effect'], Path(__file__).resolve().parents[1] / 'src')
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT id,status,attempts FROM effects')) == (observed['effect'], 'confirmed', 1)
        assert not tx.rows('SELECT id FROM attempts')
        assert not tx.rows('SELECT id FROM invocations')
    if mode == 'confirmed':
        assert restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce) == old
        _continue_with_original_02(tmp_path, git_repo, old, state['run_id'])
    else:
        # The existing fixed sent story proves readback, not inverse after a
        # new Resume action. That action must keep the original inverse guard.
        fenced = old.read_bytes()
        with pytest.raises(MigrationRefused, match='workspace_return_progress_unsupported'):
            restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce)
        assert old.read_bytes() == fenced


def test_fixed_4aba_private_copy_cutover_and_no_effect_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-observation"
    source = oracle / "kernel-initial-v6.sqlite3"
    bundle = oracle / "kernel-initial-v6.bundle"
    facts = json.loads((oracle / "kernel-initial-v6.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "64a39d083111ad3625b7da038c2904040da32eb0790415a06a029ff334af1203"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "cbc45212d4743008729c1662abdc253928352eb4e07f5045827a90ad646d4e71"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    snapshot = capture_v6(source, tmp_path / "sealed.sqlite3")
    private = tmp_path / "private"
    port = OfflinePort.create(private / "new-runtime")
    run_branch = facts["root"]["value_run_branch"]
    base_sha = facts["refs"][run_branch][0]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run")
    port.git.run("reset", "--hard", base_sha)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base_sha)
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (base_sha,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", base_sha))
    checkout = private / "new-runtime" / "work" / "ticket-2"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), base_sha)
    kernel = convert_initial_v6(
        snapshot, private / "new-runtime" / "kernel.sqlite3", repository_root=port.git.root,
        source_ref=f"refs/heads/agent-run/{facts['run']['run_id']}/converted-ticket-2",
        checkout=checkout, final_checkout=private / "new-runtime" / "work" / "final",
        development_profile=Profile("fixture-development", "high"),
        review_profile=Profile("fixture-review", "high"),
        publication_profile=Profile("fixture-publication", "high"))
    old_copy = private / "old-state" / "state.sqlite3"
    carrier = private / "local-host-carrier"
    carrier.write_text("private synthetic host only", encoding="utf-8")
    nonce, launch = cutover_idle_copied_v6(
        snapshot, old_copy, kernel, private_root=private, action_id="copy-cutover-run",
        command=(sys.executable, "-c", "import sys; sys.stdin.buffer.read()"),
        environment_carrier=carrier, preflight=lambda: None)
    with closing(sqlite3.connect(old_copy)) as db:
        protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    assert protocol.startswith("retired-to-native-copy/1:")
    task = TaskKey(port.git.root, facts["run"]["repository"], facts["run"]["parent_number"])
    with locked_task(task) as path:
        assert read_task(path) == (facts["run"]["run_id"], kernel.storage.path)

    # A local, reaped process supplies actual PID, start token, cgroup and boot
    # identity. It is a private test Host, not a systemd or userbus assertion.
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
        start_token = stat[stat.rfind(")") + 2:].split()[19]
        cgroup = Path(f"/proc/{child.pid}/cgroup").read_text(encoding="ascii").splitlines()[0].split(":", 2)[2]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        invocation = f"local-{child.pid}-{start_token}"
        assert kernel.observe_host(launch, HostObservation(
            "running", launch.unit_name, invocation, cgroup, boot_id, child.pid,
            start_token, evidence="actual local child process", usage_ready=True)) == "running"
    finally:
        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=10) == 0
    assert kernel.observe_host(launch, HostObservation(
        "exited", launch.unit_name, invocation, cgroup, boot_id, child.pid, start_token,
        0, time.time(), "actual child reaped; local usage released", usage_released=True)) == "exited"
    kernel.control("stop", "copy-cutover-stop")
    owner = sqlite3.connect(old_copy, isolation_level=None)
    try:
        assert owner.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        owner.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationRefused, match="source_not_offline"):
            restore_idle_copied_v6_without_new_effects(snapshot, old_copy, kernel, nonce=nonce)
        # A second SQLite descriptor opened and closed by the refusal path
        # would cancel this process's POSIX main-file lock.
        probe = subprocess.run([sys.executable, "-c", """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error), error
else:
    raise AssertionError('rollback refusal released the existing SQLite writer lock')
finally:
    db.close()
""", str(old_copy)], capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stdout + probe.stderr
    finally:
        owner.rollback()
        owner.close()
    foreign = old_copy.parent / (".restored-" + nonce + ".sqlite3")
    foreign.write_bytes(b"another attempt owns this name")
    assert restore_idle_copied_v6_without_new_effects(snapshot, old_copy, kernel, nonce=nonce) == old_copy
    assert foreign.read_bytes() == b"another attempt owns this name"
    assert hashlib.sha256(old_copy.read_bytes()).hexdigest() == snapshot.sha256
    assert hashlib.sha256(source.read_bytes()).hexdigest() == snapshot.sha256
    with closing(sqlite3.connect(old_copy)) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == "sqlite-work-records-v6"
    with locked_task(task) as path:
        assert read_task(path) == (facts["run"]["run_id"], old_copy)


_WORKSPACE_CHILD = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.model import Execution, Requirements
from agent_run_kernel.operation_runner import DeliveryFacts
sys.path.insert(0, str(Path(sys.argv[3]).parent / 'tests'))
from support.core_operations import CoreOperations, ReadOnlyPorts
if sys.stdin.buffer.readline() != b'go\n':
    raise SystemExit(0)
kernel = Kernel(Path(sys.argv[1]))
launch = kernel.host_request(sys.argv[2])
execution = Execution(launch.run_id, launch.owner, launch.epoch)
delivery = kernel.delivery('ticket:2')
mode = sys.argv[4] if len(sys.argv) > 4 else 'confirmed'
# The same literal external Issue story used by the frozen original
# continuation below; never reconstructed from native Core records.
source = Requirements.from_wire({
    'parent': {'number': 1, 'title': 'Parent spec', 'body': 'Deliver the ticket set.',
               'sub_issues': [2], 'sub_issue_order_reliable': True},
    'issues': {'2': {'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
                      'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}},
})
class Ports(ReadOnlyPorts):
    writes = 0
    def __init__(self, binding):
        self.local = LocalEffects(LocalGit(binding.repository_root), owned_root=Path(delivery.owned_root))
        super().__init__(source, delivery.source_ref, delivery.target_ref,
                         DeliveryFacts(self.head(delivery.target_ref), self.head(delivery.source_ref)))
    def head(self, ref):
        result = self.local.git.run('rev-parse', '--verify', ref, check=False)
        return None if result.returncode else self.local.git.commit(result.stdout.strip())
    def delivery(self, request):
        assert (request.source_ref, request.target_ref) == (self.source_ref, self.target_ref)
        return DeliveryFacts(self.head(request.target_ref), self.head(request.source_ref))
    def execute(self, effect, authorize):
        assert effect.operation == 'workspace' and mode != 'readback'
        self.effect = effect
        self.result = self.local.execute(effect, authorize)
        self.writes += 1
        if mode == 'sent':
            # A real authorized local write happened; only its response is
            # lost. Core must retain the original sent intent for readback.
            raise OSError('synthetic lost workspace response after actual write')
        return self.result
    def reconcile(self, effect):
        assert effect.operation == 'workspace'
        self.effect = effect
        self.result = self.local.reconcile(effect)
        return self.result
    def inspect(self, resources):
        return self.local.inspect_resources(resources)
ports = []
def make_ports(binding):
    port = Ports(binding)
    ports.append(port)
    return port
operations = CoreOperations(kernel, execution, make_ports)
step = operations.once(agent_capacity=0)
port = ports[0]
effect, result = port.effect, port.result
assert effect.operation == 'workspace'
assert result.disposition == 'target'
if mode == 'sent':
    assert (step.kind, step.reason) == ('waiting', 'original_write_readback_required')
else:
    assert step.kind == 'progress'
    assert operations.until(lambda step: kernel.status(delivery.key)['phase'] == 'development',
                            agent_capacity=0).kind == 'progress'
if mode == 'readback':
    assert effect.effect_id == sys.argv[5]
    assert port.writes == 0
else:
    assert port.writes == 1
assert kernel.status(delivery.key)['phase'] == ('workspace' if mode == 'sent' else 'development')
print(json.dumps({'effect': effect.effect_id, 'head': result.commit.sha}), flush=True)
sys.stdin.buffer.readline()
"""


def _continue_with_original_02(tmp_path: Path, repository: Path, old: Path, run_id: str) -> None:
    project = Path(__file__).resolve().parents[1]
    archive = subprocess.run([
        "git", "archive", "02b008e43386a73c4510250732a7216d7dba17a5", "project/src",
        "project/tests/support/cli_payloads.py",
    ], cwd=project.parent, capture_output=True, check=True).stdout
    legacy = tmp_path / "unchanged-02"
    legacy.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source:
            if not member.isfile():
                continue
            relative = Path(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            target = legacy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.extractfile(member).read())
    script = r"""
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[4], sys.argv[5]]
from agent_run.state import StateStore
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.delivery import TicketDeliveryEngine
from support.cli_payloads import passing_acceptance, publication
root, old, run_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
states = StateStore(old.parent)
before = states.load_current_run(run_id)
assert before['run_id'] == run_id and before['status'] == 'active'
assert before['active_ticket_job']['ticket_number'] == 2
git = GitRepository(root)
fixture = root / 'legacy-fixture.json'
fixture.write_text(json.dumps({
    'repository': 'example/project', 'default_branch': 'main', 'default_head_sha': git.resolve('main'),
    'parent': {'number': 1, 'title': 'Parent spec', 'body': 'Deliver the ticket set.',
               'sub_issues': [2], 'sub_issue_order_reliable': True},
    'issues': {'2': {'number': 2, 'title': 'Ticket 2', 'body': 'Deliver the selected ticket.',
                      'state': 'OPEN', 'labels': ['ready-for-agent'], 'blocked_by': []}},
}))
resumed, reused = Controller(FixtureGitHubReader(fixture), git, states).start(1)
assert reused and resumed['run_id'] == run_id
agents_file = root / 'legacy-agents.json'
agents_file.write_text(json.dumps({
    'developments': [{'thread_id': 'old-development', 'summary': 'Finish restored Ticket.',
                       'write_files': {'restored-ticket.txt': 'delivered\n'}}],
    'reviews': [passing_acceptance('old-review', 'Original runtime reviewed restored work.')],
    'publications': [{**publication(), 'thread_id': 'old-development'}],
}))
calls = []
class Agents(FixtureAgentBackend):
    def develop(self, request):
        calls.append('development'); return super().develop(request)
    def review(self, request):
        calls.append('review'); return super().review(request)
    def publication(self, request):
        calls.append('publication'); return super().publication(request)
TicketDeliveryEngine(git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
                     agents=Agents(agents_file)).deliver(run_id)
after = states.load_current_run(run_id)
job = after['ticket_jobs']['2']
assert after['status'] == 'ticket_completed'
assert job['phase'] == 'completed'
assert job['ticket_closed_by_run'] is True
assert git.resolve(after['run_branch']) == job['integrated_sha']
observed = json.loads(fixture.read_text())
assert observed['issues']['2']['state'] == 'CLOSED'
pulls = observed['delivery']['pull_requests']
assert len(pulls) == 1 and pulls[0]['state'] == 'MERGED'
assert pulls[0]['integrated_sha'] == job['integrated_sha']
assert observed['delivery']['ticket_close_ownership']['2']['event_id'] == run_id + ':ticket-2:closed'
assert job['review_budget']['development_attempts'] == 1
assert job['review_budget']['reviewer_invocations'] == 1
assert calls == ['development', 'review', 'publication']
print(json.dumps({'run_id': run_id, 'phase': job['phase'], 'calls': calls}))
"""
    result = subprocess.run([
        sys.executable, "-c", script, str(repository), str(old), run_id,
        str(legacy / "project" / "src"), str(legacy / "project" / "tests"),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["phase"] == "completed"


_INTERRUPTED_RETURN = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[7])
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration import capture_v6
import agent_run_kernel.migration_cutover as cutover
snapshot = capture_v6(Path(sys.argv[1]), Path(sys.argv[2]))
bind = cutover.bind_task
def interrupted(*args):
    if sys.argv[6] == 'after_git_crash':
        os._exit(73)
    bind(*args)
    os._exit(74)
cutover.bind_task = interrupted
cutover.restore_copied_v6_after_workspace(snapshot, Path(sys.argv[3]),
                                         Kernel(Path(sys.argv[4])), nonce=sys.argv[5])
raise AssertionError('return did not reach the exact interruption boundary')
"""


def _readback_with_new_host(kernel: Kernel, carrier: Path, original_effect: str, source_root: Path) -> None:
    command = (sys.executable, '-u', '-c', _WORKSPACE_CHILD, str(kernel.storage.path),
               'workspace-readback-resume', str(source_root), 'readback', original_effect)
    launch = kernel.admit_host('workspace-readback-resume', kind='resume', command=command,
                               environment_carrier=carrier, preflight=lambda: None)
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    try:
        stat = Path(f'/proc/{child.pid}/stat').read_text()
        token = stat[stat.rfind(')') + 2:].split()[19]
        cgroup = Path(f'/proc/{child.pid}/cgroup').read_text().splitlines()[0].split(':', 2)[2]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        invocation = f'local-readback-{child.pid}-{token}'
        assert kernel.observe_host(launch, HostObservation(
            'running', launch.unit_name, invocation, cgroup, boot, child.pid, token,
            evidence='actual resumed local child only reads original workspace intent', usage_ready=True)) == 'running'
        child.stdin.write(b'go\n')
        child.stdin.flush()
        import select
        assert select.select([child.stdout], [], [], 10)[0], 'readback child did not settle original effect'
        line = child.stdout.readline()
        assert line, child.stderr.read().decode()
        assert json.loads(line)['effect'] == original_effect
        assert kernel.status('ticket:2')['phase'] == 'development'
        kernel.control('stop', 'workspace-readback-stop')
    finally:
        child.stdin.close()
        try:
            exit_code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            raise
    assert exit_code == 0, child.stderr.read().decode()
    assert kernel.observe_host(launch, HostObservation(
        'exited', launch.unit_name, invocation, cgroup, boot, child.pid, token, 0, time.time(),
        'actual resumed child reaped; usage released', usage_released=True)) == 'exited'


@pytest.mark.parametrize("checkpoint", [
    "confirmed", "symbolic_ref", "symbolic_ref_race", "foreign_ref", "sent",
    "after_git_crash", "after_locator_crash", "identity_changed",
])
def test_real_workspace_effect_returns_to_unchanged_02_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str,
) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-observation"
    source, bundle = oracle / "kernel-initial-v6.sqlite3", oracle / "kernel-initial-v6.bundle"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "64a39d083111ad3625b7da038c2904040da32eb0790415a06a029ff334af1203"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "cbc45212d4743008729c1662abdc253928352eb4e07f5045827a90ad646d4e71"
    facts = json.loads((oracle / "kernel-initial-v6.json").read_text())
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    snapshot = capture_v6(source, tmp_path / "sealed.sqlite3")
    private = tmp_path / "private"
    port = OfflinePort.create(private / "new-runtime")
    run_branch = facts["root"]["value_run_branch"]
    base = facts["refs"][run_branch][0]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run")
    port.git.run("reset", "--hard", base)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base)
    # Keep the sealed source identity as the inverse expectation. The unique
    # installer creates this new private clone's distinct copy identity.
    with closing(sqlite3.connect(snapshot.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        checkout_identity = db.execute("SELECT value_checkout_identity FROM run_root").fetchone()[0]
    marker = port.git.root / ".git" / "agent-run-checkout-id"
    assert not marker.exists()
    checkout = private / "new-runtime" / "work" / "ticket-2"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), base)
    kernel = convert_initial_v6(
        snapshot, private / "new-runtime" / "kernel.sqlite3", repository_root=port.git.root,
        source_ref=f"refs/heads/agent-run/{facts['run']['run_id']}/converted-ticket-2",
        checkout=checkout, final_checkout=private / "new-runtime" / "work" / "final",
        development_profile=Profile("fixture-development", "high"), review_profile=Profile("fixture-review", "high"),
        publication_profile=Profile("fixture-publication", "high"))
    old = private / "old-state" / "state.sqlite3"
    carrier = private / "carrier"
    carrier.write_text("private local Host")
    command = (sys.executable, "-u", "-c", _WORKSPACE_CHILD, str(kernel.storage.path),
               "workspace-return-run", str(Path(__file__).resolve().parents[1] / "src"),
               "sent" if checkpoint == "sent" else "confirmed")
    nonce, launch = cutover_idle_copied_v6(
        snapshot, old, kernel, private_root=private, action_id="workspace-return-run", command=command,
        environment_carrier=carrier, preflight=lambda: None)
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    try:
        stat = Path(f"/proc/{child.pid}/stat").read_text()
        token = stat[stat.rfind(")") + 2:].split()[19]
        cgroup = Path(f"/proc/{child.pid}/cgroup").read_text().splitlines()[0].split(":", 2)[2]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        invocation = f"local-{child.pid}-{token}"
        assert kernel.observe_host(launch, HostObservation(
            "running", launch.unit_name, invocation, cgroup, boot, child.pid, token,
            evidence="actual local native workspace child", usage_ready=True)) == "running"
        child.stdin.write(b"go\n")
        child.stdin.flush()
        import select
        assert select.select([child.stdout], [], [], 10)[0], "native child did not reach workspace checkpoint"
        line = child.stdout.readline()
        assert line, child.stderr.read().decode()
        result = json.loads(line)
        assert result["head"] == base
        assert kernel.status("ticket:2")["phase"] == ("workspace" if checkpoint == "sent" else "development")
        kernel.control("stop", "workspace-return-stop")
        with pytest.raises(MigrationRefused):
            restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce)
    finally:
        child.stdin.close()
        try:
            exit_code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            raise
    assert exit_code == 0, child.stderr.read().decode()
    assert kernel.observe_host(launch, HostObservation(
        "exited", launch.unit_name, invocation, cgroup, boot, child.pid, token, 0, time.time(),
        "actual native workspace child reaped; local usage released", usage_released=True)) == "exited"
    delivery = kernel.delivery("ticket:2")
    if checkpoint == "identity_changed":
        fenced = old.read_bytes()
        marker.write_text("0" * 32 + "\n", encoding="ascii")
        assert marker.read_text().strip() != checkout_identity
        with pytest.raises(MigrationRefused):
            restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce)
        assert old.read_bytes() == fenced
        assert marker.read_text().strip() == "0" * 32
        assert port.git.run("rev-parse", delivery.source_ref).stdout.strip() == base
        with locked_task(TaskKey(port.git.root, facts["run"]["repository"], facts["run"]["parent_number"])) as location:
            assert read_task(location) == (facts["run"]["run_id"], kernel.storage.path)
        return
    task = TaskKey(port.git.root, facts["run"]["repository"], facts["run"]["parent_number"])
    fenced = old.read_bytes()
    if checkpoint in {"symbolic_ref", "symbolic_ref_race", "foreign_ref", "sent"}:
        run_ref = f"refs/heads/{run_branch}"
        if checkpoint == "symbolic_ref":
            port.git.run("symbolic-ref", delivery.source_ref, run_ref)
            assert port.git.run("symbolic-ref", delivery.source_ref).stdout.strip() == run_ref
        elif checkpoint == "symbolic_ref_race":
            import agent_run_kernel.migration_cutover as cutover
            checked = cutover._workspace_git
            changed: list[bool] = []

            def replace_after_read(state, *, created):
                present = checked(state, created=created)
                if created and not changed:
                    port.git.run("symbolic-ref", delivery.source_ref, run_ref)
                    changed.append(True)
                return present
            monkeypatch.setattr(cutover, "_workspace_git", replace_after_read)
        elif checkpoint == "foreign_ref":
            tree = port.git.run("rev-parse", f"{base}^{{tree}}").stdout.strip()
            foreign_sha = port.git.run("commit-tree", tree, "-p", base, "-m", "foreign advancement").stdout.strip()
            port.git.run("update-ref", delivery.source_ref, foreign_sha, base)
            assert port.git.run("rev-parse", delivery.source_ref).stdout.strip() == foreign_sha
        else:
            with kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.one("SELECT id,status,attempts,receipt_id FROM effects")) == (result["effect"], "sent", 1, None)
        assert port.git.run("rev-parse", run_ref).stdout.strip() == base
        with pytest.raises(MigrationRefused):
            restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce)
        assert old.read_bytes() == fenced
        if checkpoint in {"symbolic_ref", "symbolic_ref_race"}:
            assert port.git.run("symbolic-ref", delivery.source_ref).stdout.strip() == run_ref
            if checkpoint == "symbolic_ref_race":
                assert changed == [True]
        else:
            assert port.git.run("rev-parse", delivery.source_ref).stdout.strip() == (foreign_sha if checkpoint == "foreign_ref" else base)
        assert port.git.run("rev-parse", run_ref).stdout.strip() == base
        assert port.git.run("rev-parse", "refs/heads/main").stdout.strip() == base
        with locked_task(task) as location:
            assert read_task(location) == (facts["run"]["run_id"], kernel.storage.path)
        with closing(sqlite3.connect(old)) as db:
            assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0].startswith(
                "retired-to-native-copy/1:")
        if checkpoint == "sent":
            _readback_with_new_host(kernel, carrier, result["effect"], Path(__file__).resolve().parents[1] / "src")
            with kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.one("SELECT id,status,attempts FROM effects")) == (result["effect"], "confirmed", 1)
                assert tx.one("SELECT count(*) FROM effects")[0] == 1
                assert tx.one("SELECT count(*) FROM invocations")[0] == 0
            assert old.read_bytes() == fenced
            assert port.git.run("rev-parse", delivery.source_ref).stdout.strip() == base
            with locked_task(task) as location:
                assert read_task(location) == (facts["run"]["run_id"], kernel.storage.path)
        return
    if checkpoint in {"after_git_crash", "after_locator_crash"}:
        interrupted = tmp_path / "interrupted-capture"
        interrupted.mkdir()
        child = subprocess.run([
            sys.executable, "-c", _INTERRUPTED_RETURN, str(source), str(interrupted / "sealed.sqlite3"),
            str(old), str(kernel.storage.path), nonce, checkpoint,
            str(Path(__file__).resolve().parents[1] / "src"),
        ], capture_output=True, text=True, timeout=15)
        assert child.returncode == (73 if checkpoint == "after_git_crash" else 74), child.stdout + child.stderr
        assert old.read_bytes() == fenced
        assert port.git.run("for-each-ref", "--format=%(refname)", delivery.source_ref).stdout == ""
        with locked_task(task) as location:
            assert read_task(location) == (facts["run"]["run_id"], kernel.storage.path if checkpoint == "after_git_crash" else old)
        with pytest.raises(MigrationRefused, match="copied_v6_fence_identity"):
            restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce + "-foreign")
        assert old.read_bytes() == fenced
    assert restore_copied_v6_after_workspace(snapshot, old, kernel, nonce=nonce) == old
    assert old.read_bytes() == snapshot.path.read_bytes()
    assert marker.read_text().strip() == checkout_identity
    assert port.git.run("for-each-ref", "--format=%(refname)", delivery.source_ref).stdout == ""
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM effects WHERE status='confirmed'")[0] == 1
        assert tx.one("SELECT count(*) FROM attempts")[0] == 0
        assert tx.one("SELECT count(*) FROM invocations")[0] == 0
    _continue_with_original_02(tmp_path, port.git.root, old, facts["run"]["run_id"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == snapshot.sha256
