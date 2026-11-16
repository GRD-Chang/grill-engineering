"""Public Abandon assertions over the original run, GitHub state and remote refs."""
import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path

from agent_run.queries.status_comment import project_status
from agent_run.storage.notification_outbox import read_notifications
from support.managed_abandon_transport import assert_completed_pr_preserved
from support.host_close_trace import raise_with_trace
from support.systemd_manager import _diagnostic_tail, _git, _managed_failure_note


@contextmanager
def report_abandon_failure(stage, kernel, directory, children):
    try:
        yield
    except AssertionError as error:
        try:
            _managed_failure_note(error, stage=stage, database=kernel.storage.path,
                                 run_id=kernel.storage.run_id, roles=directory / 'roles.jsonl')
            error.add_note('Host stderr: ' + _diagnostic_tail(directory / 'manager' / 'host.stderr'))
        finally:
            raise_with_trace(error, directory, children)


@contextmanager
def final_approval_hold(reached, release, serving, kernel, directory, children, deadline):
    try:
        with report_abandon_failure('abandon-before-first-final-approval', kernel, directory, children):
            while not reached.wait(0.1):
                assert serving.is_alive() and time.monotonic() < deadline, kernel.source_status()
        yield
    finally:
        release.set()


def _writes(directory):
    return [row for row in (json.loads(line) for line in (directory / "gh-calls.jsonl").read_text().splitlines())
            if "--method" in row]


def _remote_snapshot(state, deadline):
    # One read snapshot of the existing fake service and its calls;
    # hold no Core/notification lock and issue no remote request.
    with Path(str(state) + '.lock').open('r') as lock:
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'Fake service snapshot exceeded the original deadline'
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.01, remaining))
        return json.loads(state.read_text()), _writes(state.parent)


def _assert_recovery_receipts(kernel, abandoned, final, final_pr, ticket_pr, document, completions):
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
    assert retirement_comments[0]["created_at"] == retirement_receipt["raw"]["close_intent"]["comment_created_at"]
    marker = "<!-- agent-run-kernel:" + recovered["id"] + " -->"
    comments = [comment for comment in document["comments_by_issue"]["2"] if marker in comment["body"]]
    assert len(comments) == 1
    comment = comments[0]
    assert comment["user"]["login"] == "publisher"
    assert comment["body"] == marker + "\n" + payload["comment_body"]
    assert kernel.storage.run_id in comment["body"] and "#2" in comment["body"]
    assert f"PR #{ticket_pr['number']}" in comment["body"] and completions[0]["merge_sha"] in comment["body"]
    assert "abandon" in comment["body"].lower() and "default branch" in comment["body"].lower()
    assert receipt["raw"]["comment"]["id"] == comment["id"]
    assert receipt["raw"]["comment"]["created_at"] == comment["created_at"]
    assert document["source_graph"]["issues"]["2"]["updated_at"] == comment["created_at"]
    return comment, receipt


@contextmanager
def abandon_at_approval(public, kernel, accepted, state, original_main, deadline):
    """Apply Abandon while the Host is held; verify recovery after its actual exit."""
    tmp_path = state.parent
    remote = tmp_path / "origin.git"
    assert kernel.status("ticket:2")["phase"] == "complete"
    completions = kernel.source_status()["completions"]
    assert len(completions) == 1 and completions[0]["subject"] == "ticket:2"
    final = kernel.source_status()["final_subject"]
    assert final is not None and kernel.status(final)["phase"] == "approval"
    delivery = kernel.delivery(final)
    run_head = _git(remote, "rev-parse", delivery.source_ref)
    before, original_writes = _remote_snapshot(state, deadline)
    assert before["source_graph"]["issues"]["2"]["state"] == "CLOSED"
    final_status = kernel.status(final)
    repair_records = final_status["repair_history"]
    expected_prs = {row["pr_number"] for row in completions} | {final_status["pr_number"]}
    expected_prs.update(row["pr_number"] for row in repair_records)
    assert len(before["prs"]) == len(expected_prs)
    assert {pr["number"] for pr in before["prs"]} == expected_prs
    preserved_repairs = []
    for record in repair_records:
        repair_pr, = [pr for pr in before["prs"] if pr["number"] == record["pr_number"]]
        assert record["state"] == "merged" and repair_pr["merged"] and repair_pr["state"] == "closed"
        assert "refs/heads/" + repair_pr["base"]["ref"] == record["target_ref"] == delivery.source_ref
        assert "refs/heads/" + repair_pr["head"]["ref"] == record["source_ref"]
        assert repair_pr["head"]["sha"] == record["integrated_sha"] == record["published_sha"]
        assert repair_pr["merge_commit_sha"] == record["merge_sha"]
        _git(remote, "merge-base", "--is-ancestor", record["merge_sha"], run_head)
        preserved_repairs.append(repair_pr)
    ticket_pr = next(pr for pr in before["prs"] if pr["number"] == completions[0]["pr_number"])
    final_pr = next(pr for pr in before["prs"] if pr["number"] == kernel.status(final)["pr_number"])
    assert ticket_pr["merged"] and not final_pr["merged"] and final_pr["state"] == "open"
    assert _git(remote, "rev-parse", "refs/heads/main") == original_main
    role_trace = (tmp_path / "roles.jsonl").read_bytes()
    assert [json.loads(line)["role"] for line in role_trace.splitlines()] == [
        "development", "review", "publication", "development", "development", "review", "publication"]
    request = kernel.host_request(accepted["action_id"])
    abandoned = public("abandon", accepted["run_id"])
    assert abandoned["status"] == "action_pending" and abandoned["host"] == "running", abandoned
    assert kernel.host_request(accepted["action_id"]) == request
    yield
    assert kernel.source_status()["abandonment"] == {"action": abandoned["action_id"], "status": "complete"}
    assert kernel.source_status()["completions"] == completions
    completed = public("status", accepted["run_id"])
    assert completed["status"] == "abandoned"
    document, current_writes = _remote_snapshot(state, deadline)
    assert document["source_graph"]["issues"]["2"]["state"] == "OPEN"
    assert document["source_graph"]["parent"].get("state", "OPEN") == "OPEN"
    projection = project_status(kernel.storage.path, 'ticket:2', storage=kernel.storage)
    journal = read_notifications(kernel.storage.path.parent, accepted['run_id'])
    assert_completed_pr_preserved(before, document, original_writes, current_writes, projection, journal)
    assert kernel.status(final)["repair_history"] == repair_records
    assert kernel.status(final)["budgets"] == final_status["budgets"]
    for repair_pr in preserved_repairs:
        assert next(pr for pr in document["prs"] if pr["number"] == repair_pr["number"]) == repair_pr
        forbidden = {f"repos/example/project/{kind}/{repair_pr['number']}" for kind in ("issues", "pulls")}
        forbidden.add(f"repos/example/project/pulls/{repair_pr['number']}/merge")
        assert not any(row[1].split("?", 1)[0] in forbidden for row in current_writes[len(original_writes):])
    retired = next(pr for pr in document["prs"] if pr["number"] == final_pr["number"])
    assert retired["state"] == "closed" and not retired["merged"]
    assert _git(remote, "rev-parse", "refs/heads/main") == original_main
    assert _git(remote, "rev-parse", delivery.source_ref) == run_head
    assert (tmp_path / "roles.jsonl").read_bytes() == role_trace
    comment, receipt = _assert_recovery_receipts(kernel, abandoned, final, final_pr, ticket_pr, document, completions)
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
    assert _writes(tmp_path) == current_writes
    (tmp_path / "completed-ticket-abandon-evidence.json").write_text(json.dumps({
        "abandon_action": abandoned, "completed_status": completed,
        "original_completion": completions[0], "recovery_comment": comment, "recovery_receipt": receipt,
        "all_local_refs": _git(authority, "for-each-ref", "--format=%(refname) %(objectname)"),
        "all_remote_refs": _git(remote, "for-each-ref", "--format=%(refname) %(objectname)"),
        "worktree_registry": registry,
    }, indent=2))
