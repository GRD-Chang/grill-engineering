"""Controlled real CLI processes exercise the Codex transport, never a service."""

from dataclasses import replace
from contextlib import closing
import json
import os
from collections.abc import Mapping
from pathlib import Path
import sys
import sqlite3
import time
from typing import Any

import pytest

from agent_run.model import AgentCall, AgentPreparation, Commit, Delivery, DeliveryKind, EffectResult, Policy, Profile, RunSpec
from agent_run.adapters.git_objects import LocalGit
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.runtime.codex import execute_codex
from support.offline import fixture_inputs, review_result
from agent_run.contracts.agent_profiles import resolve_profiles
from support.core_operations import CoreOperations, ReadOnlyPorts, final_scenario, native_agent_fixture, observed_call
from support.native_runtime import local_codex


RAW = b'{"result_kind":"development","summary":"Implemented by the local CLI process.","human_blockers":null}'


def allocated_codex(tmp_path: Path, *, prior_developments: int = 0,
                    checkout: Path | None = None) -> tuple[Kernel, AgentPreparation, ReadOnlyPorts]:
    if checkout is None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
    state = tmp_path / "runner-state"
    state.mkdir()
    base = Commit("base", "tree") if prior_developments == 0 else LocalGit(checkout).commit('HEAD')
    spec = RunSpec("codex-boundaries", "example/repository", 1, "parent-v1", "graph-v1", (
        Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                 "refs/heads/ticket", "refs/heads/run", checkout=str(checkout)),
    ), Policy(development_profile=Profile("bound-model", "high"),
              review_profile=Profile("bound-review-model", "high"),
              publication_profile=Profile("bound-publication-model", "high")))
    inputs = replace(fixture_inputs(), profiles=resolve_profiles(preset="economy", overrides={
        "development_model": "bound-model", "development_effort": "high",
        "review_model": "bound-review-model", "review_effort": "high",
        "publication_model": "bound-publication-model", "publication_effort": "high"}))
    kernel, ports = native_agent_fixture(state / "kernel.sqlite3", replace(spec, inputs=inputs))
    if prior_developments:
        class CandidateFailures(ReadOnlyPorts):
            """Explicit external failure input; never a physical candidate proof."""
            def execute(self, effect, authorize):
                assert effect.operation == 'candidate'
                authorize.prepare()()
                return EffectResult('failure', base, raw={'head': base.sha, 'reason': 'candidate_integrity'})
        ports = CandidateFailures(ports.requirements, ports.source_ref, ports.target_ref,
            replace(ports.facts, target=base))
    execution = kernel.claim_executor("local-cli-executor")
    operations = CoreOperations(kernel, execution, lambda binding: ports, prepare_agents=False)
    for ordinal in range(prior_developments + 1):
        call = operations.until(lambda step: step.preparation is not None).preparation
        assert call is not None
        if ordinal == prior_developments:
            return kernel, call, ports
        # The backend binding comes from the real CLI event parser, not bind_thread().
        result = local_codex(kernel, call, tmp_path, f"""\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({RAW!r})
""")
        assert result is not None and result.returncode == 0 and result.raw == RAW
        assert kernel.status(call.subject)['budgets']['requirements']['developments_used'] == ordinal + 1
    raise AssertionError('No allocated Development after bounded completed prefix')


@pytest.mark.parametrize("raw", [RAW, b'{"result_kind":"development","summary":42}'], ids=["valid", "invalid"])
def test_nonzero_exit_preserves_owned_final_raw_for_kernel_decision(tmp_path: Path, raw: bytes) -> None:
    kernel, call, ports = allocated_codex(tmp_path)
    result = local_codex(kernel, call, tmp_path, f"""\
print(json.dumps({{"type": "thread.started", "thread_id": "original-thread"}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({raw!r})
raise SystemExit(7)
""")
    assert result is not None and result.returncode == 7 and result.raw == raw
    with pytest.raises(ChildProcessError):
        os.waitpid(int(result.identity["pid"]), os.WNOHANG)
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    before = reopened.status(call.subject)
    assert before["pending_return"] is True and before["gates"] == []
    assert before["phase"] == "development" and before["pending_attempt"] == call.attempt_id
    assert before["budgets"]["requirements"]["developments_used"] == 1
    with reopened.storage.transaction(write=False) as tx:
        failures = [json.loads(row['body']) for row in tx.rows("SELECT body FROM material WHERE subject=? AND purpose='worker_failure'", (call.subject,))]
        diagnostic = failures[-1]['diagnostic']
    assert diagnostic['final_output_exists'] is True and diagnostic['final_output_kind'] == 'regular'
    assert diagnostic['final_output_bytes'] == len(raw)
    assert diagnostic['final_output_json_shape'] == 'json_object'  # Shape never accepts an invalid Artifact.
    assert reopened.operation_state().active == ()
    step = CoreOperations(reopened, call.execution, lambda binding: ports, prepare_agents=False).once(agent_capacity=0)
    assert step.kind == ("progress" if raw == RAW else "needs_agent")
    assert reopened.status(call.subject)["gates"] == []
    if raw != RAW:
        step = CoreOperations(reopened, call.execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None)
        assert step.preparation is not None and step.preparation.mode == "resume"
        assert step.preparation.reservation_id != call.reservation_id and step.preparation.thread_id == call.thread_id
        with reopened.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM invocations")[0] == 1  # Credentials not ready yet.


@pytest.mark.parametrize("events,prior_developments", [
    (("original-thread",), 0), (("different-thread",), 0), (("original-thread", "different-thread"), 0),
    (("original-thread",), 3),
], ids=["same-thread", "drift", "two-ids", "last-budgeted-attempt-retains-dirty-work"])
def test_real_resume_preserves_original_thread_and_rejects_changed_events(
    tmp_path: Path, request: pytest.FixtureRequest, events: tuple[str, ...], prior_developments: int,
) -> None:
    checkout = None
    if prior_developments:
        git_repo = request.getfixturevalue('git_repo')
        checkout = tmp_path / 'checkout'
        git = LocalGit(git_repo)
        git.run('worktree', 'add', '-b', 'ticket', str(checkout), 'HEAD')
        git.run('update-ref', 'refs/heads/run', git.commit('HEAD').sha)
    kernel, first, ports = allocated_codex(tmp_path, prior_developments=prior_developments,
        checkout=checkout)
    now = [kernel.clock()]
    kernel.clock = lambda: now[0]
    original_head = None if checkout is None else LocalGit(checkout).commit('HEAD').sha
    original_index = None if checkout is None else LocalGit(checkout).run('ls-files', '--stage', '-z').stdout
    tracked, untracked = b'Fourth Development tracked work\n', b'Fourth Development untracked work\n'
    work = (f"Path('README.md').write_bytes({tracked!r}); Path('unfinished.txt').write_bytes({untracked!r})\n"
        if prior_developments else '')
    before = kernel.status(first.subject)['budgets']['requirements']
    prior_exits = [event['identity'] for event in kernel.history(first.subject) if event['kind'] == 'worker_exited']
    if prior_developments:
        assert (before['window'], before['development_limit'], before['developments_used']) == (1, 4, 3)
        with kernel.storage.transaction(write=False) as tx:
            attempts = tx.all("SELECT id,ordinal,status,accounted FROM attempts WHERE subject=? AND role='development' ORDER BY ordinal",
                (first.subject,))
        assert [(row['ordinal'], row['status'], row['accounted']) for row in attempts] == [
            (1, 'consumed', 1), (2, 'consumed', 1), (3, 'consumed', 1), (4, 'allocated', 0)]
        assert attempts[-1]['id'] == first.attempt_id and len(prior_exits) == 3

    def retained_work():
        if prior_developments:
            assert (checkout / 'README.md').read_bytes() == tracked
            assert (checkout / 'unfinished.txt').read_bytes() == untracked
            git = LocalGit(checkout)
            assert git.commit('HEAD').sha == original_head
            assert git.commit('refs/heads/ticket').sha == original_head
            assert git.run('ls-files', '--stage', '-z').stdout == original_index
            assert git.run('diff', '--name-only').stdout.splitlines() == ['README.md']
            assert 'unfinished.txt' in git.run('ls-files', '--others', '--exclude-standard').stdout.splitlines()
            assert f'worktree {checkout}' in git.run('worktree', 'list', '--porcelain', '-z').stdout.split('\0')

    first_admitted_at = now[0]
    failed = local_codex(kernel, first, tmp_path, f"""\
assert sys.argv[1:{4 if prior_developments else 3}] == {['exec', 'resume', '--json'] if prior_developments else ['exec', '--json']!r}
{work}\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
raise SystemExit(1)
""")
    assert failed is not None and failed.returncode == 1 and failed.raw == b""
    retained_work()
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    reopened.clock = lambda: now[0]
    assert reopened.status(first.subject)["gates"] == []
    assert reopened.operation_state().active == ()
    charged = {**before, 'developments_used': prior_developments + 1}
    assert reopened.status(first.subject)['budgets']['requirements'] == charged
    now[0] += 1.25
    resumed = CoreOperations(reopened, first.execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None).preparation
    assert resumed is not None and resumed.mode == "resume"
    assert resumed.reported_thread == "original-thread" and resumed.thread_id == first.thread_id
    assert resumed.attempt_id == first.attempt_id and resumed.reservation_id != first.reservation_id
    assert resumed.profile == first.profile
    retained_work()
    preservation = (f"""\
import subprocess
assert Path('README.md').read_bytes() == {tracked!r}
assert Path('unfinished.txt').read_bytes() == {untracked!r}
assert subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip() == {original_head!r}
""" if prior_developments else '')
    program = f"""\
assert sys.argv[1:4] == ['exec', 'resume', '--json']
assert sys.argv[5] == 'original-thread'
assert sys.argv[sys.argv.index('--model') + 1] == 'bound-model'
assert sys.argv[sys.argv.index('--config') + 1] == 'model_reasoning_effort="high"'
assert sys.stdin.read() == 'Continue the allocated task.'
{preservation}\
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({RAW!r})
for thread in {events!r}:
    print(json.dumps({{"type": "thread.started", "thread_id": thread}}), flush=True)
"""
    now[0] += .5  # Ordinary recovery starts its period at admission, not preparation.
    resumed_admitted_at = now[0]
    if events == ("original-thread",):
        result = local_codex(reopened, resumed, tmp_path, program)
        assert result is not None and result.returncode == 0 and result.raw == RAW
    else:
        with pytest.raises(ValueError, match="different Thread ID"):
            local_codex(reopened, resumed, tmp_path, program)
    original_call, resumed_call = observed_call(kernel, first), observed_call(kernel, resumed)
    assert resumed_call.invocation_id != original_call.invocation_id
    assert original_call.deadline_at == first_admitted_at + original_call.deadline_seconds
    assert resumed_call.deadline_seconds == original_call.deadline_seconds == resumed.deadline_seconds
    assert resumed_call.deadline_at == resumed_admitted_at + original_call.deadline_seconds
    assert resumed_call.deadline_at > original_call.deadline_at
    after = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    after.clock = lambda: now[0]
    status = after.status(first.subject)
    assert status["pending_return"] is (events == ("original-thread",))
    assert status["gates"] == ([] if events == ("original-thread",) else ["execution_failed"])
    assert status['budgets']['requirements'] == charged
    assert after.operation_state().active == ()
    exits = [event["identity"] for event in after.history(first.subject) if event["kind"] == "worker_exited"]
    assert exits == [*prior_exits, original_call.invocation_id, resumed_call.invocation_id]
    after.bind_thread(resumed_call, "original-thread")
    with pytest.raises(KernelError, match="thread_identity_changed"):
        after.bind_thread(resumed_call, "different-thread")
    retained_work()
    if prior_developments:
        operations = CoreOperations(after, first.execution, lambda binding: ports, prepare_agents=False)
        for _ in range(8):
            step = operations.once()
            assert step.preparation is None and step.call is None
            if step.kind == 'paused':
                break
        else:
            raise AssertionError('No original budget boundary after the fourth returned Development')
        assert 'modification_budget_exhausted' in after.status(first.subject)['gates']
        assert after.status(first.subject)['budgets']['requirements'] == charged
        with after.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM attempts WHERE subject=? AND role='development'", (first.subject,))[0] == 4
            assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref')", (first.subject,))
        retained_work()


@pytest.mark.parametrize("replacement", ["missing", "symlink"])
def test_stdout_artifact_cannot_replace_missing_or_symlinked_final(tmp_path: Path, replacement: str) -> None:
    kernel, call, _ = allocated_codex(tmp_path)
    with pytest.raises(OSError) as failed:
        local_codex(kernel, call, tmp_path, f"""\
print(json.dumps({{"type": "thread.started", "thread_id": "original-thread"}}), flush=True)
print({RAW.decode()!r}, flush=True)
print('private raw stderr secret-fixture-value', file=sys.stderr, flush=True)
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
output.unlink()
if {replacement!r} == 'symlink':
    target = Path.cwd() / 'unowned-final'
    target.write_bytes({RAW!r})
    output.symlink_to(target)
""")
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    status = reopened.status(call.subject)
    assert status["pending_return"] is False and status["gates"] == ["execution_failed"]
    assert status["pending_attempt"] == call.attempt_id and status["candidate"] is None
    assert status["budgets"]["requirements"]["developments_used"] == 1
    assert reopened.operation_state().active == ()
    assert any(event["kind"] == "worker_exited" and event["identity"] == observed_call(kernel, call).invocation_id
               for event in reopened.history(call.subject))
    assert not any(event["kind"] == "raw_returned" for event in reopened.history(call.subject))
    from agent_run.queries.run import read
    document = read(kernel.storage.path, 'status', storage=kernel.storage).data
    action = next(item for item in document['operator_actions'] if item['status_code'] == 'execution_failed')
    proof = action['evidence']['failure_evidence']
    assert proof['returncode'] == 0
    diagnostic = proof['diagnostic']
    assert diagnostic['final_output_exists'] is (replacement != 'missing')
    assert diagnostic['final_output_kind'] == replacement
    assert diagnostic['final_output_json_shape'] == 'not_read'
    assert diagnostic['thread_observed'] is True and diagnostic['stdout_bytes'] > 0
    assert diagnostic['stderr_tail_present'] is True and diagnostic['stderr_tail_bytes'] > 0
    assert 'private raw stderr' not in json.dumps(document) and 'secret-fixture-value' not in json.dumps(document)
    assert all('private raw stderr' not in note for note in getattr(failed.value, '__notes__', ()))


def test_kernel_allocated_reviewer_cannot_write_checkout_but_can_return_final(tmp_path: Path) -> None:
    scenario = final_scenario(tmp_path / "scenario")
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    def review_ready(step):
        if step.preparation is None:
            assert step.kind in {"effect", "progress"}, step
        return step.preparation is not None
    call = operations.until(review_ready).preparation
    assert call is not None and call.role.value == "review" and call.context["readonly"] is True
    checkout = Path(call.context["checkout"])
    original = (checkout / "README.md").read_bytes()
    raw = json.dumps(review_result("pass")).encode()
    result = local_codex(scenario.kernel, call, tmp_path, f"""\
import errno
assert Path.cwd() == Path({str(checkout)!r})
assert Path('README.md').read_bytes() == {original!r}
for name in ('README.md', 'forbidden-new-file'):
    try:
        Path(name).write_text('Unauthorized checkout mutation')
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        raise AssertionError('The allocated read-only checkout was writable')
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
scratch = output.parent / 'review-runtime-output'
scratch.write_text('Runtime output outside the reviewed checkout is writable')
assert scratch.read_text().startswith('Runtime output')
scratch.unlink()
print(json.dumps({{"type": "thread.started", "thread_id": "review-thread"}}), flush=True)
output.write_bytes({raw!r})
""")
    assert result is not None and result.returncode == 0 and result.raw == raw
    assert (checkout / "README.md").read_bytes() == original
    assert not (checkout / "forbidden-new-file").exists()
    assert scenario.kernel.status(call.subject)["pending_return"] is True
    assert scenario.kernel.operation_state().active == ()


@pytest.mark.parametrize('binding_reply_lost', [False, True], ids=['normal', 'binding-reply-lost'])
def test_codex_jsonl_binds_real_thread_and_only_owned_final_file_is_the_return(
    tmp_path: Path, binding_reply_lost: bool,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = tmp_path / "runner-state"
    state.mkdir()
    path = state / "kernel.sqlite3"
    base = Commit("base", "tree")
    spec = RunSpec("codex-process", "example/repository", 1, "parent-v1", "graph-v1", (
        Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                 "refs/heads/ticket", "refs/heads/run", checkout=str(checkout)),
    ), Policy(development_profile=Profile("bound-model", "high")))
    inputs = replace(fixture_inputs(), profiles=resolve_profiles(preset="economy", overrides={
        "development_model": "bound-model", "development_effort": "high"}))
    created, ports = native_agent_fixture(path, replace(spec, inputs=inputs))
    execution = created.claim_executor("local-cli-executor")
    call = CoreOperations(created, execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None).preparation
    assert call is not None and call.mode == "fresh" and call.reported_thread is None
    runner = tmp_path / "runner-private"
    runner.mkdir()
    secret = runner / "private-key"
    secret.write_text("Runner-only-secret-must-stay-hidden")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    inherited_key = ssh / "id_ed25519"
    inherited_key.write_text("Unselected host SSH identity must be hidden")
    credential = tmp_path / "selected-hosts.yml"
    credential.write_text(json.dumps({"github.com": {"oauth_token": "copied-test-credential"}}))
    credential.chmod(0o600)
    home = tmp_path / "copied-codex-home"
    home.mkdir()
    (home / "auth.json").write_text("Copied test configuration, never an online credential.")
    started, bound = checkout / "cli-started", checkout / "thread-bound"
    executable = tmp_path / "local-codex"
    executable.write_text(f"#!{sys.executable}\n" + f"""\
import json, os, stat, sys, time
from pathlib import Path
arguments = sys.argv[1:]
assert arguments[:4] == ['exec', '--json', '--color', 'never']
assert arguments[arguments.index('--model') + 1] == 'bound-model'
assert arguments[arguments.index('--config') + 1] == 'model_reasoning_effort="high"'
assert Path.cwd() == Path({str(checkout)!r})
assert os.environ.get('CUSTOM_RUNNER_PAT') is None
assert os.environ.get('AGENT_RUN_INTERNAL_CONTROL_TOKEN') is None
assert not any(name in os.environ for name in ('GH_TOKEN', 'GITHUB_TOKEN'))
for hidden in ({str(secret)!r}, {str(path)!r}, {str(credential)!r}, {str(inherited_key)!r}):
    try:
        observed = Path(hidden).read_bytes()
    except OSError:
        pass
    else:
        assert observed == b'', 'Runner data was visible to the Worker'
hosts = json.loads((Path(os.environ['GH_CONFIG_DIR']) / 'hosts.yml').read_text())
assert hosts == {{'github.com': {{'oauth_token': 'copied-test-credential'}}}}
assert (Path(os.environ['CODEX_HOME']) / 'auth.json').read_text().startswith('Copied test')
schema = Path(arguments[arguments.index('--output-schema') + 1])
assert stat.S_IMODE(schema.parent.stat().st_mode) == 0o700
assert stat.S_IMODE(schema.stat().st_mode) == 0o600
assert set(json.loads(schema.read_text())['required']) == {{'result_kind', 'summary', 'human_blockers'}}
assert sys.stdin.read() == 'Perform this one allocated development task.'
Path({str(started)!r}).write_text(str(os.getpid()))
# A discarded oversized event must not bind a conflicting Thread. A valid
# event immediately after it must survive fragmented multibyte decoding.
oversized = json.dumps({{'type': 'thread.started', 'thread_id': 'discarded-thread', 'padding': 'x' * 70000}})
sys.stdout.buffer.write(oversized.encode() + b'\\nnot-json\\n')
event = (json.dumps({{'type': 'thread.started', 'thread_id': 'local-cli-thread', 'note': '界'}}, ensure_ascii=False) + '\\n').encode()
split = event.index('界'.encode()) + 1
sys.stdout.buffer.write(event[:17]); sys.stdout.buffer.flush()
sys.stdout.buffer.write(event[17:split]); sys.stdout.buffer.flush()
sys.stdout.buffer.write(event[split:]); sys.stdout.buffer.flush()
deadline = time.monotonic() + 3
while not Path({str(bound)!r}).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('The real Thread event was not durably bound')
    time.sleep(0.01)
sys.stdout.write('{{"result_kind":"development","summary":"This is only stdout telemetry","human_blockers":null}}\\n')
output = Path(arguments[arguments.index('--output-last-message') + 1])
assert output.parent == schema.parent
output.write_bytes({RAW!r})
""")
    executable.chmod(0o700)

    now = [created.clock()]
    created.clock = lambda: now[0]
    binding_error = OSError('The backend Thread commit reply was lost')
    bound_calls = []

    class ObserveDurableThread(Kernel):
        def bind_thread(self, exact_call: AgentCall, reported_thread: str) -> None:
            super().bind_thread(exact_call, reported_thread)
            bound_calls.append(exact_call)
            # Read the committed Thread independently, before auxiliary failure
            # settlement or letting the child create its final output file.
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                assert db.execute('SELECT backend_id FROM threads WHERE run_id=? AND id=?',
                    (exact_call.execution.run_id, exact_call.thread_id)).fetchone() == ('local-cli-thread',)
                assert db.execute('SELECT dispatch_state,raw_id,exit_code FROM invocation_outputs '
                    'WHERE run_id=? AND invocation=? AND ordinal=?',
                    (exact_call.execution.run_id, exact_call.invocation_id, exact_call.output_ordinal)).fetchone() == (
                        'started', None, None)
            if binding_reply_lost:
                raise binding_error
            bound.write_text("The original Kernel accepted this exact Thread.")

    checks = 0

    def authorize_source() -> None:
        nonlocal checks
        checks += 1
        assert not started.exists()

    original_budget = created.status(call.subject)['budgets']['requirements']

    def run():
        worker_kernel = ObserveDurableThread(path, run_id=call.execution.run_id)
        worker_kernel.clock = lambda: now[0]
        return execute_codex(
            worker_kernel, call, executable=str(executable),
            prompt="Perform this one allocated development task.",
            environment={"PATH": os.defpath, "HOME": str(tmp_path), "CODEX_HOME": str(home),
                         "GH_TOKEN": "Wrong-inherited-identity", "GITHUB_TOKEN": "Wrong-fallback",
                         "CUSTOM_RUNNER_PAT": "Runner-private-token", "AGENT_RUN_INTERNAL_CONTROL_TOKEN": "Runner-control"},
            credential_file=credential, hidden_paths=(runner,), hidden_environment=("CUSTOM_RUNNER_PAT",),
            timeout=5, authorize_source=lambda admitted: authorize_source(),
        )

    if binding_reply_lost:
        with pytest.raises(OSError) as caught:
            run()
        assert caught.value is binding_error and not bound.exists()
    else:
        result = run()
        assert result is not None and result.returncode == 0 and result.raw == RAW
    assert checks == 2 and int(started.read_text()) != os.getpid()
    assert len(bound_calls) == 1
    reopened = Kernel(path, run_id=call.execution.run_id)
    reopened.clock = lambda: now[0]
    assert reopened.status(call.subject)["pending_return"] is (not binding_reply_lost)
    charged_budget = {**original_budget, 'developments_used': 1}
    assert reopened.status(call.subject)['budgets']['requirements'] == charged_budget
    assert reopened.operation_state().active == ()
    with pytest.raises(KernelError, match="thread_identity_changed"):
        reopened.bind_thread(observed_call(created, call), "another-thread")
    if binding_reply_lost:
        original = bound_calls[0]
        assert [event['identity'] for event in reopened.history(call.subject)
                if event['kind'] == 'worker_exited'] == [original.invocation_id]
        with reopened.storage.transaction(write=False) as tx:
            output = tx.one('SELECT dispatch_state,raw_id,exit_code FROM invocation_outputs '
                'WHERE invocation=? AND ordinal=?', (original.invocation_id, original.output_ordinal))
            assert output['dispatch_state'] == 'exited' and output['raw_id'] is None
            assert output['exit_code'] is not None
        now[0] += 1.25
        reopened.control('resume', 'resume-after-backend-binding-reply-loss', subject=call.subject)
        resumed = CoreOperations(reopened, execution, lambda binding: ports, prepare_agents=False).until(
            lambda step: step.preparation is not None).preparation
        assert resumed is not None and resumed.mode == 'resume'
        assert (resumed.attempt_id, resumed.thread_id, resumed.reported_thread) == (
            call.attempt_id, call.thread_id, 'local-cli-thread')
        now[0] += .5
        resumed_admitted_at = now[0]
        result = local_codex(reopened, resumed, tmp_path, f"""\
assert sys.argv[1:4] == ['exec', 'resume', '--json']
assert sys.argv[5] == 'local-cli-thread'
assert sys.stdin.read() == 'Continue the allocated task.'
print(json.dumps({{'type': 'thread.started', 'thread_id': 'local-cli-thread'}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({RAW!r})
""")
        assert result is not None and result.returncode == 0 and result.raw == RAW
        resumed_call = observed_call(reopened, resumed)
        assert resumed_call.invocation_id != original.invocation_id
        assert resumed_call.deadline_seconds == original.deadline_seconds == resumed.deadline_seconds
        assert resumed_call.deadline_at == resumed_admitted_at + original.deadline_seconds
        assert resumed_call.deadline_at > original.deadline_at
        assert reopened.status(call.subject)['pending_return'] is True
        assert reopened.status(call.subject)['budgets']['requirements'] == charged_budget
        assert reopened.operation_state().active == ()
    assert CoreOperations(reopened, call.execution, lambda binding: ports, prepare_agents=False).once(agent_capacity=0).kind == "progress"


@pytest.mark.parametrize("error,capacity", [
    ({"codex_error_info": "server_overloaded", "message": "Temporarily overloaded"}, True),
    ({"codex_error_info": "usage_limit", "message": "Selected model is at capacity. Please try a different model."}, False),
    ({"codex_error_info": {"unknown": "machine"}, "message": "Selected model is at capacity. Please try a different model."}, False),
    ({"message": "Selected model is at capacity. Please try a different model."}, True),
    (None, False),
], ids=["machine-capacity", "machine-precedes-message", "unknown-machine", "legacy-message", "stderr-only"])
def test_real_terminal_facts_control_only_kernel_recovery(tmp_path: Path, error: object, capacity: bool) -> None:
    kernel, first, ports = allocated_codex(tmp_path)
    now = [kernel.clock()]
    kernel.clock = lambda: now[0]
    program = f"""\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
print(json.dumps({{'type': 'turn.started'}}), flush=True)
if {error!r} is not None:
    print(json.dumps({{'type': 'turn.failed', 'error': {error!r}}}), flush=True)
sys.stderr.write('Selected model is at capacity. Please try a different model.')
raise SystemExit(7)
"""
    result = local_codex(kernel, first, tmp_path, program)
    assert result is not None and result.returncode == 7 and result.raw == b""
    assert kernel.operation_state().active == ()
    with kernel.storage.transaction(write=False) as tx:
        output = tx.one("SELECT * FROM invocation_outputs WHERE invocation=? AND ordinal=?", (observed_call(kernel, first).invocation_id, first.output_ordinal))
        assert output["failure_kind"] == ("capacity" if capacity else "ordinary")
        assert tx.get(output["raw_id"], first.subject) == b""
        failure = json.loads(tx.get(output["start_evidence"], first.subject))
        if error is None:
            diagnostic = failure["diagnostic"]
            assert diagnostic["source"] == "codex-output"
            assert diagnostic["final_output_exists"] is True and diagnostic["final_output_kind"] == "regular"
            assert diagnostic["final_output_bytes"] == 0 and diagnostic["final_output_json_shape"] == "empty"
            assert diagnostic["thread_observed"] is True and diagnostic["thread_bound"] is True
            assert diagnostic["stdout_bytes"] > 0 and diagnostic["stderr_tail_present"] is True
            assert not {"message", "machine", "machine_present"} & diagnostic.keys()
        else:
            assert failure["diagnostic"]["message"] == error["message"]
            assert failure["diagnostic"]["machine_present"] is ("codex_error_info" in error)
        rows = tx.rows("SELECT active FROM worker_activity WHERE invocation=? ORDER BY identity", (observed_call(kernel, first).invocation_id,))
        assert [row[0] for row in rows] == ([1] if error is None else [1, 0])
    kernel.configure_profiles("later-model", overrides={"development_model": "new-model"})
    exited_at = now[0]
    now[0] = exited_at + (29.999 if capacity else 2)
    if capacity:
        assert CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).once().kind == "waiting"
        assert kernel.operation_state().active == ()
        now[0] = exited_at + 30
    recovered = CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None).preparation
    assert recovered is not None and recovered.mode == "resume"
    assert recovered.attempt_id == first.attempt_id and recovered.thread_id == first.thread_id
    assert recovered.reservation_id != first.reservation_id and recovered.output_ordinal == 0
    assert recovered.deadline_seconds == first.deadline_seconds
    assert recovered.profile == first.profile and recovered.profile_revision == first.profile_revision
    assert recovered.profile_reference == first.profile_reference
    second = local_codex(kernel, recovered, tmp_path, program)
    assert second is not None and second.returncode == 7
    assert observed_call(kernel, recovered).invocation_id != observed_call(kernel, first).invocation_id
    assert observed_call(kernel, recovered).deadline_at == now[0] + first.deadline_seconds
    next_step = CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).once()
    assert next_step.kind == ("waiting" if capacity else "paused")
    assert kernel.status(first.subject)["budgets"]["requirements"]["developments_used"] == 1
    assert kernel.operation_state().active == ()


@pytest.mark.parametrize("raw", [b"", b'{"result_kind":"development","summary":42}'], ids=["empty", "invalid"])
def test_real_format_repair_keeps_exact_output_identity_deadline_and_profile(tmp_path: Path, raw: bytes) -> None:
    kernel, first, ports = allocated_codex(tmp_path)
    now = [kernel.clock()]
    kernel.clock = lambda: now[0]
    result = local_codex(kernel, first, tmp_path, f"""\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({raw!r})
""")
    assert result is not None and result.raw == raw and result.returncode == 0
    kernel.configure_profiles("later-model", overrides={"development_model": "new-model"})
    now[0] += 2
    repair = CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None).preparation
    assert repair is not None and repair.mode == "output-repair"
    assert repair.output_ordinal == first.output_ordinal + 1
    assert repair.preparation_deadline == min(now[0] + 600, observed_call(kernel, first).deadline_at)
    assert repair.deadline_seconds == first.deadline_seconds
    assert repair.thread_id == first.thread_id and repair.reported_thread == "original-thread"
    assert repair.profile == first.profile and repair.profile_revision == first.profile_revision
    assert repair.profile_reference == first.profile_reference and repair.context["readonly"] is True
    repaired = local_codex(kernel, repair, tmp_path, f"""\
import errno
assert sys.argv[1:4] == ['exec', 'resume', '--json'] and sys.argv[5] == 'original-thread'
assert sys.argv[sys.argv.index('--model') + 1] == 'bound-model'
try:
    Path('illegal-repair-write').write_text('No second development mutation')
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('Output repair checkout was writable')
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({RAW!r})
""")
    assert repaired is not None and repaired.raw == RAW and repaired.returncode == 0
    assert observed_call(kernel, repair).invocation_id == observed_call(kernel, first).invocation_id
    assert observed_call(kernel, repair).deadline_at == observed_call(kernel, first).deadline_at
    with kernel.storage.transaction(write=False) as tx:
        outputs = tx.rows("SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (observed_call(kernel, first).invocation_id,))
        assert [row["ordinal"] for row in outputs] == [0, 1]
        assert [tx.get(row["raw_id"], first.subject) for row in outputs] == [raw, RAW]
        assert all(row["exit_code"] == 0 and row["dispatch_state"] == "exited" for row in outputs)
        assert outputs[0]["process_identity"] != outputs[1]["process_identity"]
    assert CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).once().kind == "progress"
    assert kernel.status(first.subject)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize("events", [(), ("turn.started",), ("turn.started", "turn.completed")], ids=["unknown", "missing-end", "complete"])
def test_real_activity_never_infers_a_turn_from_process_lifetime(tmp_path: Path, events: tuple[str, ...]) -> None:
    kernel, call, _ = allocated_codex(tmp_path)
    result = local_codex(kernel, call, tmp_path, f"""\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
for event in {events!r}:
    print(json.dumps({{'type': event, 'tool_output': 'private tool output'}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({RAW!r})
""")
    assert result is not None and result.returncode == 0
    with kernel.storage.transaction(write=False) as tx:
        rows = tx.rows("SELECT * FROM worker_activity WHERE invocation=? ORDER BY identity", (observed_call(kernel, call).invocation_id,))
        assert [row["active"] for row in rows] == [int(event == "turn.started") for event in events]
        assert all(row["ordinal"] == call.output_ordinal for row in rows)
        evidence = [json.loads(tx.get(row["evidence"], call.subject)) for row in rows]
        assert [item["type"] for item in evidence] == list(events)
        assert "private tool output" not in repr(evidence)
        assert tx.one("SELECT exit_code FROM invocation_outputs WHERE invocation=?", (observed_call(kernel, call).invocation_id,))[0] == 0


def test_real_failure_diagnostic_projects_redacts_and_bounds_before_persistence(tmp_path: Path) -> None:
    kernel, call, _ = allocated_codex(tmp_path)
    message = "API request failed: " + json.dumps({
        "request_id": "private-request", "status": 429,
        "error": {"type": "invalid_request_error", "code": "invalid_api_key",
                  "message": "Authorization: Bearer sk-persisted-secret " + "界" * 4000,
                  "headers": {"authorization": "Bearer private-header"},
                  "response_body": {"customer": "private-customer"}},
    })
    result = local_codex(kernel, call, tmp_path, f"""\
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
print(json.dumps({{'type': 'turn.failed', 'error': {{'codex_error_info': 'usage_limit', 'message': {message!r}}}}}), flush=True)
raise SystemExit(7)
""")
    assert result is not None and result.returncode == 7
    with kernel.storage.transaction(write=False) as tx:
        output = tx.one("SELECT start_evidence FROM invocation_outputs WHERE invocation=?", (observed_call(kernel, call).invocation_id,))
        stored = tx.get(output[0], call.subject)
        diagnostic = json.loads(stored)["diagnostic"]
        assert diagnostic["machine"] == '"usage_limit"'
        assert diagnostic["sanitized"] is True and diagnostic["truncated"] is True
        assert len(json.dumps(diagnostic, ensure_ascii=False).encode()) <= 8192
        assert "[REDACTED]" in diagnostic["message"]
        assert all(secret.encode() not in stored for secret in (
            "sk-persisted-secret", "private-header", "private-customer", "private-request",
        ))


def test_real_capacity_event_cannot_authorize_recovery_after_corrupt_capture(tmp_path: Path) -> None:
    kernel, call, ports = allocated_codex(tmp_path)
    observed = Path(call.context["checkout"]) / "terminal-observed"

    class ObserveTerminal(Kernel):
        def observe_worker_activity(self, exact_call: AgentCall, observation_id: str, *, active: bool,
                                    observed_at: float, evidence: Mapping[str, Any]) -> None:
            super().observe_worker_activity(exact_call, observation_id, active=active,
                                            observed_at=observed_at, evidence=evidence)
            if not active:
                observed.write_text("The terminal event reached the Kernel")

    kernel = ObserveTerminal(kernel.storage.path, run_id=call.execution.run_id)
    with pytest.raises(UnicodeDecodeError):
        local_codex(kernel, call, tmp_path, f"""\
import time
from dataclasses import replace
print(json.dumps({{'type': 'thread.started', 'thread_id': 'original-thread'}}), flush=True)
print(json.dumps({{'type': 'turn.failed', 'error': {{'codex_error_info': 'server_overloaded'}}}}), flush=True)
deadline = time.monotonic() + 3
while not Path({str(observed)!r}).exists():
    assert time.monotonic() < deadline, 'The actual terminal event was not observed'
    time.sleep(0.01)
sys.stdout.buffer.write(b'\\xff'); sys.stdout.buffer.flush()
raise SystemExit(7)
""")
    assert observed.exists()
    assert kernel.status(call.subject)["gates"] == ["execution_failed"]
    # The already recorded fatal gate leaves no public runnable operation.
    assert CoreOperations(kernel, call.execution, lambda binding: ports, prepare_agents=False).once().kind == "quiescent"
    assert kernel.operation_state().active == ()
    with kernel.storage.transaction(write=False) as tx:
        output = tx.one("SELECT * FROM invocation_outputs WHERE invocation=?", (observed_call(kernel, call).invocation_id,))
        assert output["failure_kind"] == "fatal" and output["raw_id"] is None
        failure = json.loads(tx.get(output["start_evidence"], call.subject))
        assert failure["diagnostic"]["machine"] == '"server_overloaded"'
        assert tx.one("SELECT count(*) FROM invocations WHERE attempt=?", (call.attempt_id,))[0] == 1


def test_real_format_repair_process_uses_only_the_original_remaining_deadline(tmp_path: Path) -> None:
    kernel, first, ports = allocated_codex(tmp_path)
    result = local_codex(kernel, first, tmp_path, """\
print(json.dumps({'type': 'thread.started', 'thread_id': 'original-thread'}), flush=True)
""")
    assert result is not None and result.raw == b""
    remaining = observed_call(kernel, first).deadline_at - 0.5
    kernel.clock = lambda: remaining
    repair = CoreOperations(kernel, first.execution, lambda binding: ports, prepare_agents=False).until(lambda step: step.preparation is not None).preparation
    assert repair is not None and repair.output_ordinal == 1
    assert repair.preparation_deadline == observed_call(kernel, first).deadline_at
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        local_codex(kernel, repair, tmp_path, """\
import time
from dataclasses import replace
print(json.dumps({'type': 'thread.started', 'thread_id': 'original-thread'}), flush=True)
time.sleep(10)
""")
    assert time.monotonic() - started < 2, "Repair reset the original remaining deadline"
    assert observed_call(kernel, repair).invocation_id == observed_call(kernel, first).invocation_id
    assert observed_call(kernel, repair).deadline_at == observed_call(kernel, first).deadline_at
    assert kernel.operation_state().active == ()
    with kernel.storage.transaction(write=False) as tx:
        outputs = tx.rows("SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (observed_call(kernel, first).invocation_id,))
        assert len(outputs) == 2 and outputs[1]["dispatch_state"] == "exited"
        identity = json.loads(tx.get(outputs[1]["process_identity"], first.subject))
        with pytest.raises(ChildProcessError):
            os.waitpid(identity["pid"], os.WNOHANG)
    assert kernel.status(first.subject)["budgets"]["requirements"]["developments_used"] == 1
