"""A smaller current tree is an ordinary repair of an immutable Candidate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tests.test_kernel_managed_entry_end_to_end import _CODEX, _control_environment, _git, _local_manager


FINDING = "Remove obsolete-extra.txt from the final diff; keep the requested implementation."
EXTRA = "This earlier Candidate file is outside the requested scope.\n"


def test_public_ticket_repair_deletes_an_earlier_candidate_file_without_a_human_blocker(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    # The fake CLI edits files and returns raw role artifacts. The installed
    # Controller still owns staging, commits, selection, Review and publication.
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    codex = codex.replace("'head': head}", "'head': head, 'prompt': prompt}")
    codex = codex.replace("if role == 'development':\n", f"""if role == 'development':
    extra = Path('obsolete-extra.txt')
    if Path('managed-delivery.txt').exists():
        assert {FINDING!r} in prompt, 'The original Finding did not reach the repair Worker'
        assert extra.read_text() == {EXTRA!r}
        extra.unlink()
    else:
        extra.write_text({EXTRA!r})
""")
    codex = codex.replace("'status': 'pass', 'evidence':",
        "'status': 'fail' if lane == 'spec' and Path('obsolete-extra.txt').exists() else 'pass', 'evidence':")
    codex = codex.replace("'findings': []}",
        f"'findings': [{FINDING!r}] if lane == 'spec' and Path('obsolete-extra.txt').exists() else []}}")
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
    origin = tmp_path / "origin.git"
    base = _git(origin, "rev-parse", "refs/heads/main")
    deadline = time.monotonic() + 120
    with _local_manager(tmp_path / "manager", environment, host_timeout=120) as (
        manager, serving, errors, children, trace, commands, output,
    ):
        environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

        def public(*arguments: str):
            result = subprocess.run([sys.executable, "-m", "agent_run", *arguments,
                "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                text=True, capture_output=True, timeout=min(20, max(0.1, deadline - time.monotonic())))
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        accepted = public("run", "1", "--no-notifications")
        assert accepted["result"] == "accepted"
        serving.join(timeout=max(0, deadline - time.monotonic()))
        assert not serving.is_alive() and not errors, (errors, output)
        assert len(commands) == len(children) == 1 and children[0].returncode == 0
        assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
        status = public("status", accepted["run_id"])
        history = public("history", accepted["run_id"])["history"]
        ticket, = (job for job in status["subjects"] if job["kind"] == "ticket")
        assert ticket["issue"] == 2 and ticket["phase"] == "complete" and ticket["generation"] == 1
        assert ticket["gates"] == [] and ticket["pending_attempt"] is None and ticket["pending_writes"] == []
        assert ticket["delivery_cleanup"]["state"] == "complete"
        budget, = (row for row in ticket["budgets"] if row["purpose"] == "requirements")
        assert (budget["window"], budget["developments_used"], budget["reviews_used"]) == (1, 2, 2)
        reviews = [row for row in history["attempts"] if row["subject"] == ticket["subject"] and row["role"] == "review"]
        reviews.sort(key=lambda row: row["ordinal"])
        assert [row["outcome"] for row in reviews] == ["fail", "pass"]
        assert reviews[0]["result"]["checks"]["spec"]["findings"] == [FINDING]
        assert reviews[1]["result"]["checks"]["spec"]["findings"] == []
        first, repaired = (row["candidate_sha"] for row in reviews)
        assert first != repaired
        root = Path(status["repository_root"])
        assert _git(root, "rev-list", "--parents", "-n", "1", repaired).split() == [repaired, first]
        assert _git(root, "show", first + ":obsolete-extra.txt") == EXTRA.strip()
        assert _git(root, "ls-tree", "--name-only", repaired, "--", "obsolete-extra.txt") == ""
        assert set(_git(root, "diff", "--name-only", base, first).splitlines()) == {
            "managed-delivery.txt", "obsolete-extra.txt"}
        assert _git(root, "diff", "--name-only", base, repaired).splitlines() == ["managed-delivery.txt"]
        roles = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
        developers = [row for row in roles if row["role"] == "development"]
        reviewers = [row for row in roles if row["role"] == "review"]
        assert len(developers) == 2 and developers[1]["head"] == first
        assert FINDING in developers[1]["prompt"]
        assert [row["head"] for row in reviewers[:2]] == [first, repaired]
        assert reviewers[0]["thread"] != reviewers[1]["thread"]
        remote = json.loads(state.read_text())
        ticket_pr, = (pr for pr in remote["prs"] if pr["number"] == ticket["pr_number"])
        assert ticket_pr["merged"] is True
        assert _git(origin, "diff", "--name-only", base, ticket_pr["merge_commit_sha"]).splitlines() == ["managed-delivery.txt"]
        assert remote["source_graph"]["issues"]["2"]["state"] == "CLOSED"
        assert _git(origin, "rev-parse", "refs/heads/main") == base
