"""Fixed old artifacts drive native offline conversion without loading the old runtime."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.migration_active import convert_returned_publication_v6
from agent_run_kernel.model import Policy, Profile, RunSpec
from agent_run_kernel.offline import OfflinePort, Scenario


def test_fixed_4aba_v6_copy_converts_and_new_runtime_completes_ticket(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-observation"
    source = oracle / "kernel-initial-v6.sqlite3"
    bundle = oracle / "kernel-initial-v6.bundle"
    facts = json.loads((oracle / "kernel-initial-v6.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "64a39d083111ad3625b7da038c2904040da32eb0790415a06a029ff334af1203"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "cbc45212d4743008729c1662abdc253928352eb4e07f5045827a90ad646d4e71"
    snapshot = capture_v6(source, tmp_path / "source.sqlite3")
    root = tmp_path / "new-runtime"
    port = OfflinePort.create(root)
    run_branch = facts["root"]["value_run_branch"]
    base_sha = facts["refs"][run_branch][0]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run")
    assert port.git.commit("refs/remotes/v6/main").sha == base_sha
    assert port.git.commit("refs/remotes/v6/run").sha == base_sha
    port.git.run("reset", "--hard", base_sha)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base_sha)
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (base_sha,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", base_sha))
    checkout = root / "work" / "ticket-2"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), base_sha)
    source_ref = f"refs/heads/agent-run/{facts['run']['run_id']}/converted-ticket-2"
    kernel = convert_initial_v6(snapshot, root / "kernel.sqlite3", repository_root=port.git.root,
                                source_ref=source_ref, checkout=checkout,
                                final_checkout=root / "work" / "final",
                                development_profile=Profile("fixture-development", "high"),
                                review_profile=Profile("fixture-review", "high"),
                                publication_profile=Profile("fixture-publication", "high"))
    assert port.ref_commit(source_ref) is None
    assert kernel.status("ticket:2")["phase"] == "workspace"
    assert source.read_bytes() == snapshot.path.read_bytes()
    with kernel.storage.transaction(write=False) as tx:
        frozen = tx.one("SELECT subject,material_id FROM run_inputs")
        inputs = json.loads(tx.get(frozen["material_id"], frozen["subject"]))
        assert inputs["notifications"] == {"enabled": False}
        assert inputs["provenance"]["notifications"] == "old-not-configured"
        assert inputs["provenance"]["profiles"] == "explicit-migration-input"
        assert inputs["provenance"]["source_sha256"] == snapshot.sha256
        revision = tx.one("SELECT subject,material_id FROM profile_revisions WHERE id=1")
        profile = json.loads(tx.get(revision["material_id"], revision["subject"]))
        assert profile["preset"] is None
        assert profile["profiles"]["development"]["provenance"] == {
            "source": "explicit-migration-input"}
        assert profile["profiles"]["development"]["model"] == "fixture-development"
    delivery = kernel.delivery("ticket:2")
    initial = kernel.status(delivery.key)
    scenario = Scenario.__new__(Scenario)
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run"]["run_id"], facts["run"]["repository"],
                            facts["run"]["parent_number"], initial["parent_revision"],
                            initial["graph_revision"], (delivery,), Policy())
    scenario.delivery = delivery
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("fixed-oracle-executor")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    boundary = scenario.run()
    assert boundary.kind == "complete"
    assert kernel.status(delivery.key)["phase"] == "complete"
    assert any(write["operation"] == "merge" for write in port.writes())
    assert any(write["operation"] == "completion_comment" for write in port.writes())
    assert source.read_bytes() == snapshot.path.read_bytes()

def test_fixed_8d_returned_publication_consumes_old_raw_without_new_agent(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-publication-raw-profiled-observation"
    source = oracle / "kernel-publication-raw-v6.sqlite3"
    bundle = oracle / "kernel-publication-raw-v6.bundle"
    profile = oracle / "kernel-publication-raw-profile.json"
    github = oracle / "kernel-publication-raw-github.json"
    observation = oracle / "kernel-publication-raw-v6.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "756230af586aa5531db211a8b79210a8ab3fdda737124e97d30cc9254c2cdcc1"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "c29f456f5ce52f746f9ebead8a1a2384428d19ee661886ccedad09f6dd5479b3"
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == "0e3f7a6778f4e78a02d5d9ef45a1f6772c6ff12cda86cf3e2c3498b12622b655"
    assert hashlib.sha256(github.read_bytes()).hexdigest() == "4e9c9826fe0845aee426f301b7a8dfa89c33d8659185af2b7c9ad8e6f8e71ba1"
    facts = json.loads((oracle / "kernel-publication-raw-v6.json").read_text(encoding="utf-8"))
    external = json.loads(github.read_text(encoding="utf-8"))
    snapshot = capture_v6(source, tmp_path / "captured-v6.sqlite3")
    root = tmp_path / "continued"
    port = OfflinePort.create(root)
    run_branch = f"agent-run/{facts['run_id']}/run"
    ticket_branch = f"agent-run/{facts['run_id']}/ticket-3"
    remote_base = external["delivery"]["published_branches"][ticket_branch]
    target_base = external["default_head_sha"]
    candidate_sha = facts["ticket_checkout_head"]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{ticket_branch}:refs/remotes/v6/ticket")
    assert port.git.commit("refs/remotes/v6/main").sha == remote_base == target_base
    assert port.git.commit("refs/remotes/v6/ticket").sha == candidate_sha
    port.git.run("reset", "--hard", remote_base)
    port.git.run("update-ref", f"refs/heads/{run_branch}", remote_base)
    port.git.run("update-ref", f"refs/heads/{ticket_branch}", candidate_sha)
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (target_base,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", remote_base))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{ticket_branch}", remote_base))
    assert port.remote_refs()[f"refs/heads/{ticket_branch}"] == remote_base
    assert port.remote_refs()["refs/heads/main"] == target_base
    checkout = root / "work" / "ticket-3"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), candidate_sha)
    kernel = convert_returned_publication_v6(
        snapshot, root / "kernel.sqlite3", repository_root=port.git.root,
        checkout=checkout, final_checkout=root / "work" / "final", profile_snapshot=profile,
        observation_snapshot=observation,
        remote_source_sha=remote_base, remote_target_sha=target_base)
    subject = "ticket:3"
    assert kernel.status(subject)["pending_attempt"] == facts["pending_attempt"]["attempt_id"]
    assert kernel.status(subject)["phase"] == "publish"
    assert kernel.status(subject)["reviewed_checkout"] is None
    assert port.ref_commit(f"refs/heads/{ticket_branch}").sha == remote_base
    assert port.git.commit(f"refs/agent-run-kernel-migration/{facts['run_id']}/ticket-3").sha == candidate_sha
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 0
        assert tx.one("SELECT count(*) FROM attempts")[0] == 3
        assert tx.one("SELECT count(*) FROM history WHERE kind='imported_invocation'")[0] == 3
        review = tx.one("SELECT context_id FROM attempts WHERE role='review'")
        review_context = json.loads(tx.get(review["context_id"], subject))
        assert review_context["checkout"] is None
        imported = [json.loads(tx.get(row["material_id"], subject))
                    for row in tx.rows("SELECT material_id FROM history WHERE kind='imported_invocation'")]
        old_review = next(value for value in imported if value["invocation"]["role"] == "fresh_acceptance")
        assert review_context["bound_version"]["version_id"] == old_review["bound_version"]["version_id"]
        assert old_review["bound_version"]["version_id"] != old_review["current_version"]["version_id"]
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject=? AND purpose='requirements'",
                            (subject,))) == (1, 1)
    delivery = kernel.delivery(subject)
    current = kernel.status(subject)
    scenario = Scenario.__new__(Scenario)
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run_id"], facts["repository"], facts["parent_number"],
                            current["parent_revision"], current["graph_revision"], (delivery,), Policy())
    scenario.delivery = delivery
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("returned-publication-executor")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    boundary = scenario.run()
    assert boundary.kind == "complete"
    assert kernel.status(subject)["phase"] == "complete"
    assert scenario.role_calls == []
    assert any(write["operation"] == "merge" for write in port.writes())
    assert any(write["operation"] == "completion_comment" for write in port.writes())
    assert source.read_bytes() == snapshot.path.read_bytes()
