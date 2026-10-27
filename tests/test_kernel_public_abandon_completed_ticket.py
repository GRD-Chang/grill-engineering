"""Public Abandon after a real Ticket closes and an unapproved Final PR exists."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_run.runtime.package_lease import default_runner_lock_path, runner_management_lease
from agent_run.core.kernel import Kernel
from support.managed_abandon_transport import install_currentness
from support.systemd_manager import (
    _CODEX, _approval_read_barrier, _control_environment, _git, _hold_host_operation_return, _local_manager,
)


def test_public_abandon_recovers_its_completed_ticket_and_closes_unapproved_final_pr(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    # Reuse the original whole-Run backend adapter: roles come from their
    # allocated schemas; fresh backend sessions have distinct physical IDs.
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    with _approval_read_barrier() as (address, databases, reached, release):
        _hold_host_operation_return(tmp_path, monkeypatch)
        monkeypatch.setenv("ENTRY_APPROVAL_CONTROL", address)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
        install_currentness(tmp_path, state)
        remote = tmp_path / "origin.git"
        original_main = _git(remote, "rev-parse", "refs/heads/main")
        with _local_manager(tmp_path / "manager", environment, host_timeout=150) as (
            manager, serving, errors, children, trace, commands, output,
        ):
            deadline = time.monotonic() + 150
            environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

            def public(*arguments):
                result = subprocess.run([sys.executable, "-m", 'agent_run', *arguments,
                    "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                    text=True, capture_output=True, timeout=min(20, max(0.01, deadline - time.monotonic())))
                assert result.returncode == 0, (result.stdout, result.stderr)
                return json.loads(result.stdout)

            def writes():
                return [row for row in (json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines())
                        if "--method" in row]

            try:
                accepted = public("run", "1", "--no-notifications")
                assert accepted["result"] == "accepted"
                command = commands[0]
                kernel = Kernel(Path(command[command.index('--database') + 1]), run_id=next((item.partition('=')[2] for item in command if item.startswith('--run-id='))))
                databases.append(kernel)
                environment.pop("ENTRY_APPROVAL_CONTROL")
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < deadline, (kernel.source_status(), errors, output)
                assert kernel.status("ticket:2")["phase"] == "complete"
                completions = kernel.source_status()["completions"]
                assert len(completions) == 1 and completions[0]["subject"] == "ticket:2"
                final = kernel.source_status()["final_subject"]
                assert final is not None and kernel.status(final)["phase"] == "approval"
                delivery = kernel.delivery(final)
                run_head = _git(remote, "rev-parse", delivery.source_ref)
                before = json.loads(state.read_text())
                assert before["source_graph"]["issues"]["2"]["state"] == "CLOSED"
                assert len(before["prs"]) == 2
                ticket_pr = next(pr for pr in before["prs"] if pr["number"] == completions[0]["pr_number"])
                final_pr = next(pr for pr in before["prs"] if pr["number"] == kernel.status(final)["pr_number"])
                assert ticket_pr["merged"] and not final_pr["merged"] and final_pr["state"] == "open"
                assert _git(remote, "rev-parse", "refs/heads/main") == original_main
                role_trace = (tmp_path / "roles.jsonl").read_bytes()
                assert [json.loads(line)["role"] for line in role_trace.splitlines()] == [
                    "development", "review", "publication", "review", "publication"]
                original_writes = writes()
                request = kernel.host_request(accepted["action_id"])
                abandoned = public("abandon", accepted["run_id"])
                assert abandoned["status"] == "action_pending" and abandoned["host"] == "running", abandoned
                assert kernel.host_request(accepted["action_id"]) == request
                release.set()
                serving.join(timeout=max(0.01, deadline - time.monotonic()))
                assert not serving.is_alive() and not errors, errors
                assert len(children) == len(commands) == 1 and children[0].returncode == 0, output
                assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
                assert kernel.source_status()["abandonment"] == {"action": abandoned["action_id"], "status": "complete"}
                assert kernel.source_status()["completions"] == completions
                completed = public("status", accepted["run_id"])
                assert completed["status"] == "abandoned"
                document = json.loads(state.read_text())
                assert document["source_graph"]["issues"]["2"]["state"] == "OPEN"
                assert document["source_graph"]["parent"].get("state", "OPEN") == "OPEN"
                assert next(pr for pr in document["prs"] if pr["number"] == ticket_pr["number"]) == ticket_pr
                retired = next(pr for pr in document["prs"] if pr["number"] == final_pr["number"])
                assert retired["state"] == "closed" and not retired["merged"]
                assert _git(remote, "rev-parse", "refs/heads/main") == original_main
                assert _git(remote, "rev-parse", delivery.source_ref) == run_head
                assert (tmp_path / "roles.jsonl").read_bytes() == role_trace
                with kernel.storage.transaction(write=False) as tx:
                    reopened = tx.one("SELECT * FROM effects WHERE operation='reopen_ticket' AND control_action=?",
                                      (abandoned["action_id"],))
                    recovered = tx.one("SELECT * FROM effects WHERE operation='recovery_comment' AND control_action=?",
                                       (abandoned["action_id"],))
                    assert reopened["status"] == recovered["status"] == "confirmed"
                    payload = json.loads(tx.get(recovered["payload_id"], recovered["subject"]))
                    receipt = json.loads(tx.get(recovered["receipt_id"], recovered["subject"]))
                    assert payload["reopen_effect"] == reopened["id"]
                    retirement = tx.one("SELECT * FROM effects WHERE operation='retire_pr' AND control_action=? AND subject=?",
                                        (abandoned["action_id"], final))
                    assert retirement["status"] == "confirmed"
                    retirement_intent = json.loads(tx.get(retirement["payload_id"], final))["retirement_intent"]
                    retirement_receipt = json.loads(tx.get(retirement["receipt_id"], final))
                retirement_marker = "<!-- agent-run-retirement:" + retirement["id"] + " -->"
                retirement_comments = [comment for comment in document["comments_by_issue"][str(final_pr["number"])]
                                       if retirement_marker in comment["body"]]
                assert len(retirement_comments) == 1
                assert retirement_comments[0]["user"]["login"] == "publisher"
                assert retirement_comments[0]["body"] == retirement_intent["comment_body"]
                assert retirement_comments[0]["id"] == retirement_receipt["raw"]["close_intent"]["comment_id"]
                marker = "<!-- agent-run-kernel:" + recovered["id"] + " -->"
                comments = [comment for comment in document["comments_by_issue"]["2"] if marker in comment["body"]]
                assert len(comments) == 1
                comment = comments[0]
                assert comment["user"]["login"] == "publisher"
                assert comment["body"] == marker + "\n" + payload["comment_body"]
                assert accepted["run_id"] in comment["body"] and "#2" in comment["body"]
                assert f"PR #{ticket_pr['number']}" in comment["body"] and completions[0]["merge_sha"] in comment["body"]
                assert "abandon" in comment["body"].lower() and "default branch" in comment["body"].lower()
                assert receipt["raw"]["comment"]["id"] == comment["id"]
                assert document["source_graph"]["issues"]["2"]["updated_at"] == comment["created_at"]
                current_writes = writes()
                recovery_writes = current_writes[len(original_writes):]
                reopen = [i for i, row in enumerate(recovery_writes) if row[1] == "repos/example/project/issues/2" and "PATCH" in row]
                recovery = [i for i, row in enumerate(recovery_writes) if row[1].split("?", 1)[0] == "repos/example/project/issues/2/comments" and "POST" in row]
                close_final = [i for i, row in enumerate(recovery_writes) if row[1] == f"repos/example/project/pulls/{final_pr['number']}" and "PATCH" in row]
                assert len(reopen) == len(recovery) == len(close_final) == 1
                assert reopen[0] < recovery[0] < close_final[0]
                assert not any(row[1].endswith("/merge") for row in recovery_writes)
                observed = [json.loads(line) for line in role_trace.splitlines()]
                assert all(not Path(row["checkout"]).exists() for row in observed)
                authority = Path(delivery.common_git_dir).parent
                registry = _git(authority, "worktree", "list", "--porcelain")
                assert len(registry.split("worktree ")) == 2
                repeated = public("abandon", accepted["run_id"])
                assert repeated["action_id"] == abandoned["action_id"] and repeated["status"] == "action_complete"
                assert writes() == current_writes
                with runner_management_lease(default_runner_lock_path(environment)):
                    pass
                assert _git(git_repo, "status", "--porcelain=v1") == ""
                (tmp_path / "completed-ticket-abandon-evidence.json").write_text(json.dumps({
                    "abandon_action": abandoned, "completed_status": completed,
                    "original_completion": completions[0], "recovery_comment": comment, "recovery_receipt": receipt,
                    "all_local_refs": _git(authority, "for-each-ref", "--format=%(refname) %(objectname)"),
                    "all_remote_refs": _git(remote, "for-each-ref", "--format=%(refname) %(objectname)"),
                    "worktree_registry": registry, "manager_trace": trace,
                }, indent=2))
            finally:
                release.set()
