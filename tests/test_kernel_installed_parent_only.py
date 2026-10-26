"""One installed public Parent-only consumer using the original real Host fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_run.runtime.package_lease import default_runner_lock_path, runner_management_lease
import agent_run.cli.lifecycle as lifecycle_cli
from agent_run.core.kernel import Kernel
from support.systemd_manager import (
    _CODEX, _approval_read_barrier, _control_environment, _git, _local_manager,
    _hold_host_operation_return,
)


def test_installed_public_parent_only_reaches_approval_close_cleanup_and_normal_host_exit(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    # The installed runner separately pins the wheel's source commit. Reject
    # source fallback here too; this node must exercise its installed package.
    assert Path(lifecycle_cli.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())

    def awaiting_parent_approval(kernel: Kernel):
        with kernel.storage.transaction(write=False) as tx:
            return tx.rows("SELECT id FROM subjects WHERE kind='parent_only' AND phase='approval'")

    with _approval_read_barrier(boundary=awaiting_parent_approval) as (address, databases, reached, release):
        _hold_host_operation_return(tmp_path, monkeypatch)
        monkeypatch.setenv("ENTRY_APPROVAL_CONTROL", address)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=())
        original = _git(git_repo, "rev-parse", "HEAD")
        assert json.loads(state.read_text())["source_graph"]["parent"]["sub_issues"] == []
        with _local_manager(tmp_path / "manager", environment, host_timeout=120) as (
            manager, serving, errors, children, trace, commands, output,
        ):
            deadline = time.monotonic() + 120
            environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

            def remaining():
                return max(0.01, deadline - time.monotonic())

            def public(*arguments):
                result = subprocess.run([sys.executable, "-m", 'agent_run', *arguments,
                    "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                    text=True, capture_output=True, timeout=min(20, remaining()))
                assert result.returncode == 0, (result.stdout, result.stderr)
                document = json.loads(result.stdout)
                with (tmp_path / "public-results.jsonl").open("a") as log:
                    log.write(json.dumps({"arguments": arguments, "result": document}) + "\n")
                return document

            try:
                accepted = public("run", "1", "--no-notifications")
                assert accepted["result"] == "accepted"
                command = commands[0]
                assert command[command.index("-m") + 1] == 'agent_run.runtime.executor'
                kernel = Kernel(Path(command[command.index("--database") + 1]))
                databases.append(kernel)
                # Only the original Host's actual external reads wait here.
                # The public approval reads the fresh world without this delay.
                environment.pop("ENTRY_APPROVAL_CONTROL")
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < deadline, (kernel.source_status(), errors, output)
                before = kernel.status("parent:1")
                assert before["phase"] == "approval"
                delivery = kernel.delivery("parent:1")
                assert delivery.target_ref == "refs/heads/main" and delivery.source_ref.endswith("/parent")
                head = _git(tmp_path / "origin.git", "rev-parse", delivery.source_ref)
                request = kernel.host_request(accepted["action_id"])
                with kernel.storage.transaction(write=False) as tx:
                    assert [row[0] for row in tx.all("SELECT kind FROM subjects")] == ["parent_only"]
                    assert not tx.rows("SELECT id FROM final_target")
                approved = public("approve", accepted["run_id"])
                assert approved["status"] == "action_pending" and approved["host"] == "running", approved
                assert kernel.host_request(accepted["action_id"]) == request
                with kernel.storage.transaction(write=False) as tx:
                    grant = tx.one("SELECT action,pr_number,head_sha FROM grants WHERE subject='parent:1'")
                    assert (grant["action"], grant["pr_number"], grant["head_sha"]) == (
                        approved["action_id"], before["pr_number"], head)
                release.set()
                serving.join(timeout=remaining())
                assert not serving.is_alive() and not errors, errors
                assert len(children) == len(commands) == 1 and children[0].returncode == 0, output
                assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
                status = kernel.status("parent:1")
                assert status["phase"] == "complete" and status["resource_cleanup"]["state"] == "complete"
                completed = public("status", accepted["run_id"])
                assert completed["status"] == "completed"
                observed = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
                assert [row["role"] for row in observed] == ["development", "review", "publication"]
                assert observed[0]["head"] == original and observed[1]["checkout"] != observed[0]["checkout"]
                history = kernel.history("parent:1")
                assert sum(event["kind"] == "worker_started" for event in history) == 3
                assert sum(event["kind"] == "worker_exited" for event in history) == 3
                ready = next(event["position"] for event in history if event["kind"] == "host_usage_ready")
                assert all(event["position"] > ready for event in history if event["kind"] == "worker_started")
                with kernel.storage.transaction(write=False) as tx:
                    rows = tx.all("SELECT a.role,a.result_id,o.*,t.backend_id FROM attempts a "
                        "JOIN invocations i ON i.id=a.current_invocation JOIN invocation_outputs o ON o.invocation=i.id "
                        "JOIN threads t ON t.id=i.thread WHERE a.subject='parent:1' ORDER BY a.ordinal")
                    assert [row["role"] for row in rows] == ["development", "review", "publication"]
                    for row, role in zip(rows, observed, strict=True):
                        assert row["dispatch_state"] == "exited" and row["exit_code"] == 0
                        assert row["backend_id"] == role["thread"] and row["raw_id"] and row["result_id"]
                        assert json.loads(tx.get(row["process_identity"], "parent:1"))["host_pid"] == children[0].pid
                    # Parent completion is its confirmed close receipt. The
                    # completions table indexes Ticket-to-Final witnesses only.
                    closed = tx.one("SELECT receipt_id FROM effects WHERE subject='parent:1' AND operation='close' AND status='confirmed'")
                    assert json.loads(tx.get(closed["receipt_id"], "parent:1"))["disposition"] == "target"
                    assert not tx.rows("SELECT c.subject FROM completions c JOIN subjects s ON s.id=c.subject WHERE s.kind='ticket'")
                    assert not tx.rows("SELECT subject FROM final_barriers")
                document = json.loads(state.read_text())
                assert len(document["prs"]) == 1
                pr = document["prs"][0]
                assert pr["merged"] and document["source_graph"]["parent"]["state"] == "CLOSED"
                assert pr["head"]["sha"] == head and pr["base"]["ref"] == "main"
                merged = pr["merge_commit_sha"]
                remote = tmp_path / "origin.git"
                assert _git(remote, "show", "-s", "--format=%P", merged).split() == [original, head]
                assert _git(remote, "show", "-s", "--format=%T", merged) == _git(remote, "show", "-s", "--format=%T", head)
                assert _git(remote, "show", f"{merged}:managed-delivery.txt") == "Implementation from the managed native Worker."
                assert "validation.tmp" not in _git(remote, "ls-tree", "-r", "--name-only", merged).splitlines()
                authority = Path(delivery.common_git_dir).parent
                refs = {}
                for repository, expected_main in ((authority, original), (remote, merged)):
                    assert _git(repository, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads") == f"refs/heads/main {expected_main}"
                    refs[str(repository)] = _git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
                    assert all(row.split()[0].rsplit("/", 1)[-1] != "run" for row in refs[str(repository)].splitlines())
                registry = _git(authority, "worktree", "list", "--porcelain")
                assert len(registry.split("worktree ")) == 2
                assert all(not Path(row["checkout"]).exists() for row in observed)
                writes = [row for row in (json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()) if "--method" in row]
                merges = [i for i, row in enumerate(writes) if row[1].endswith("/merge")]
                comments = [i for i, row in enumerate(writes) if row[1].endswith("/comments")]
                closes = [i for i, row in enumerate(writes) if row[1] == "repos/example/project/issues/1"]
                assert len(merges) == len(comments) == len(closes) == 1
                assert merges[0] < comments[0] < closes[0]
                with runner_management_lease(default_runner_lock_path(environment)):
                    pass
                assert _git(git_repo, "status", "--porcelain=v1") == ""
                (tmp_path / "parent-only-evidence.json").write_text(json.dumps({
                    "approval_head": head, "merged": merged, "all_refs": refs, "worktree_registry": registry,
                    "completed_status": completed, "resource_cleanup": status["resource_cleanup"],
                    "manager_trace": trace, "host_returncode": children[0].returncode,
                }, indent=2))
            finally:
                release.set()
