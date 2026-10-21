"""Public checkpoint renewal in the same real, still-live managed Host.

The test-only barrier delays the original scheduling read after a committed
checkpoint. It never changes that read's result or a Host/Run authority fact.
Restart after a proved old Host exit remains a separate acceptance boundary.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_run_kernel import Kernel
from tests.test_kernel_managed_entry_end_to_end import (
    _CODEX, _approval_read_barrier, _control_environment, _git, _local_manager,
)


FINDING = "The compatibility evidence file must document the exact supported input."


def _checkpoint_read_hook(directory: Path, address: str, ready: Path) -> Path:
    """Install only a child-process read barrier; public CLI and Workers bypass it."""
    directory.mkdir()
    (directory / "sitecustomize.py").write_text(f'''import os, sys
if '--database' in sys.argv and os.environ.get('ENTRY_BUDGET_CONTROL'):
    import socket, time
    from pathlib import Path
    from agent_run_kernel import Kernel
    original_scheduling = Kernel.scheduling
    selected = Path(sys.argv[sys.argv.index('--database') + 1])
    reached = False
    def scheduling(self):
        global reached
        if self.storage.path == selected and not reached:
            with self.storage.transaction(write=False) as tx:
                checkpoint = tx.rows("SELECT subject FROM gates WHERE subject='parent:1' AND code='review_budget_exhausted'")
            if checkpoint:
                reached = True
                deadline = time.monotonic() + 15
                while not Path({str(ready)!r}).exists():
                    assert time.monotonic() < deadline, 'The checkpoint observer was not ready'
                    time.sleep(0.01)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                    channel.settimeout(25)
                    channel.connect(chr(0) + {address!r}[1:])
                    channel.sendall(b'1'); channel.shutdown(socket.SHUT_WR)
                    assert channel.recv(1) == b'1'
        return original_scheduling(self)
    Kernel.scheduling = scheduling
''')
    return directory


def test_public_parent_checkpoint_resume_repairs_original_finding_in_a_new_window(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    codex = codex.replace("'head': head}", "'head': head, 'prompt': prompt}")
    codex = codex.replace("if role == 'development':\n", f"""if Path('budget-repair.txt').is_file():
    content += 'Compatibility evidence implemented.' + chr(10)
if role == 'development':
    if Path('managed-delivery.txt').is_file():
        assert {FINDING!r} in prompt, 'The original Reviewer finding did not reach the successor Worker'
        Path('budget-repair.txt').write_text({FINDING!r} + chr(10))
        content += 'Compatibility evidence implemented.' + chr(10)
""")
    codex = codex.replace("'status': 'pass', 'evidence':",
        "'status': 'fail' if lane == 'spec' and not Path('budget-repair.txt').is_file() else 'pass', 'evidence':")
    codex = codex.replace("'findings': []}",
        f"'findings': [{FINDING!r}] if lane == 'spec' and not Path('budget-repair.txt').is_file() else []}}")
    ready = tmp_path / "checkpoint-observer-ready"
    boundary = lambda kernel: "review_budget_exhausted" in kernel.status("parent:1")["gates"]
    with _approval_read_barrier(boundary=boundary) as (address, databases, reached, release):
        hook = _checkpoint_read_hook(tmp_path / "checkpoint-hook", address, ready)
        monkeypatch.setenv("ENTRY_BUDGET_CONTROL", address)
        monkeypatch.setenv("PYTHONPATH", str(hook) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=())
        base = _git(tmp_path / "origin.git", "rev-parse", "refs/heads/main")
        started = time.monotonic()
        deadline = started + 120
        with _local_manager(tmp_path / "manager", environment, host_timeout=120) as (
            manager, serving, errors, children, trace, commands, output,
        ):
            try:
                environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

                def public(*arguments: str):
                    result = subprocess.run([sys.executable, "-m", "agent_run_kernel", *arguments,
                        "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                        text=True, capture_output=True, timeout=min(20, max(0.1, deadline - time.monotonic())))
                    assert result.returncode == 0, (result.stdout, result.stderr)
                    return json.loads(result.stdout)

                accepted = public("run", "1", "--parent-only-paired-round", "1", "--no-notifications")
                assert accepted["result"] == "accepted"
                command = commands[0]
                kernel = Kernel(Path(command[command.index("--database") + 1]))
                databases.append(kernel)
                ready.touch()
                assert reached.wait(min(75, max(0, deadline - time.monotonic()))), (output, errors)
                assert serving.is_alive() and children[0].poll() is None
                before = kernel.status("parent:1")
                assert before["gates"] == ["review_budget_exhausted"] and before["generation"] == 1
                assert before["candidate"] is not None and before["pending_attempt"] is None
                old_budget = before["budgets"]["requirements"]
                assert (old_budget["window"], old_budget["developments_used"], old_budget["reviews_used"]) == (1, 1, 1)
                original = kernel.host_request(accepted["action_id"])
                with kernel.storage.transaction(write=False) as tx:
                    old_invocations = [dict(row) for row in tx.all("SELECT * FROM invocations ORDER BY id")]
                    old_policy = tx.one("SELECT policy FROM runs")[0]
                    prior_window = dict(tx.one("SELECT * FROM budgets WHERE subject='parent:1' AND purpose='requirements' AND window=1"))
                    reviewer = tx.one("SELECT result_id FROM attempts WHERE subject='parent:1' AND role='review'")
                    artifact = json.loads(tx.get(reviewer["result_id"], "parent:1"))
                    assert artifact["checks"]["spec"]["findings"] == [FINDING]
                assert json.loads(state.read_text())["prs"] == []
                checkpoint_seconds = time.monotonic() - started
                resumed = public("resume", accepted["run_id"], "--parent-only-paired-round", "2",
                                 "--development-deadline", "90s")
                assert resumed["result"] == "accepted" and resumed["host"] == "running"
                assert kernel.host_request(accepted["action_id"]) == original
                renewed = kernel.status("parent:1")
                assert renewed["generation"] == before["generation"] and renewed["candidate"] == before["candidate"]
                budget = renewed["budgets"]["requirements"]
                assert (budget["window"], budget["developments_used"], budget["reviews_used"],
                        budget["development_limit"], budget["review_limit"]) == (2, 0, 0, 2, 2)
                with kernel.storage.transaction(write=False) as tx:
                    assert [dict(row) for row in tx.all("SELECT * FROM invocations ORDER BY id")] == old_invocations
                    assert tx.one("SELECT policy FROM runs")[0] == old_policy
                    assert dict(tx.one("SELECT * FROM budgets WHERE subject='parent:1' AND purpose='requirements' AND window=1")) == dict(prior_window, current=0)
                    assert tx.one("SELECT count(*) FROM hosts")[0] == 1
                release.set()
                serving.join(timeout=max(0, deadline - time.monotonic()))
                assert not serving.is_alive() and not errors, (errors, output)
                assert len(commands) == len(children) == 1 and children[0].returncode == 0
                final = kernel.status("parent:1")
                assert final["phase"] == "approval" and final["gates"] == []
                assert final["candidate"] != before["candidate"] and final["generation"] == before["generation"]
                assert (final["budgets"]["requirements"]["window"],
                        final["budgets"]["requirements"]["developments_used"],
                        final["budgets"]["requirements"]["reviews_used"]) == (2, 1, 1)
                roles = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
                assert [row["role"] for row in roles] == ["development", "review", "development", "review", "publication"]
                assert FINDING in roles[2]["prompt"] and roles[2]["head"] == before["candidate"]
                assert roles[3]["head"] == final["candidate"] and roles[3]["head"] != roles[1]["head"]
                assert roles[3]["thread"] != roles[1]["thread"]
                repair = Path(roles[2]["checkout"]) / "budget-repair.txt"
                assert repair.read_text() == FINDING + "\n"
                with kernel.storage.transaction(write=False) as tx:
                    successors = tx.rows("SELECT i.deadline_seconds FROM invocations i JOIN attempts a ON a.id=i.attempt "
                        "WHERE a.subject='parent:1' AND a.role='development' ORDER BY a.ordinal")
                    assert len(successors) == 2 and successors[1][0] == 90.0
                history = kernel.history("parent:1")
                assert sum(event["kind"] == "worker_started" for event in history) == 5
                assert sum(event["kind"] == "worker_exited" for event in history) == 5
                document = json.loads(state.read_text())
                assert len(document["prs"]) == 1 and not document["prs"][0]["merged"]
                assert _git(tmp_path / "origin.git", "rev-parse", "refs/heads/main") == base
                assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
                (tmp_path / "budget-control-timing.json").write_text(json.dumps({
                    "run_to_checkpoint_seconds": checkpoint_seconds,
                    "resume_to_approval_seconds": time.monotonic() - started - checkpoint_seconds,
                    "total_seconds": time.monotonic() - started}))
            finally:
                ready.touch()
                release.set()
