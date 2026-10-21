"""Fixed old artifacts drive native offline conversion without loading the old runtime."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import agent_run_kernel.migration_completed as completed_module
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.migration_active import convert_returned_publication_v6
from agent_run_kernel.migration_completed import convert_completed_ticket_v6
from agent_run_kernel.model import Commit, Policy, Profile, RunSpec, World
from agent_run_kernel.offline import OfflinePort, Scenario


def test_fixed_c696_unknown_display_refuses_initial_conversion_without_replay(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-unknown-display-fixed-observation"
    manifest = oracle.parent / "kernel-c696-unknown-display-oracle-green-v1.manifest.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == "f9d07d2a7fb7af0b68a6a5f2be457146db0113ccc4dd9aa52842fd74308dbbdf"
    source = oracle / "unknown-display-v6.sqlite3"
    github = oracle / "unknown-display-github.json"
    observed = oracle / "unknown-display-v6.json"
    original = oracle / "unknown-display-original-actual.json"
    expected = {
        source: "cc2ba5863d2bb2e243334a77bd5f4ad69b8d0e2a185906bc8ce29553a28c26d3",
        github: "e44d4c8860b5e12007a290645afb89077f0e41a6848f6b8e0cfc7835780e80c5",
        observed: "9a5f9d496bd4a5a95e7690f1c29b3f48275a9b3f0d57c7f284c921f434cc5f1c",
        original: "198e5a12b0de7aae64f8cd9da087da54428080a65d0f611378c6040349ec0487",
    }
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in expected} == expected
    actual = json.loads(observed.read_text(encoding="utf-8"))
    assert actual["durable_outcome"] == {"display_attempted": True, "status": "indeterminate"}
    assert actual["external_call_count"] == actual["external_attempt_count"] == 1
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        display_id = db.execute("SELECT value_linked_branch_display FROM work_jobs").fetchone()[0]
        row = db.execute("SELECT owner_id,kind,value_digest,material_id FROM work_evidence "
                         "WHERE evidence_id=?", (display_id,)).fetchone()
        assert row[0].startswith("job:ticket:3:") and row[1] == "linked_branch_display"
        assert row[2] == row[3]
        body = db.execute("SELECT body FROM work_evidence_materials WHERE material_id=?", (row[3],)).fetchone()[0]
        assert hashlib.sha256(body.encode()).hexdigest() == row[2]
        assert json.loads(body) == actual["durable_outcome"]
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    destination = tmp_path / "new" / "kernel.sqlite3"
    destination.parent.mkdir()
    with pytest.raises(MigrationRefused, match="unsupported_source_relations"):
        convert_initial_v6(snapshot, destination, repository_root=tmp_path / "unused-git",
                           source_ref="refs/heads/new-ticket-3", checkout=tmp_path / "unused-checkout",
                           final_checkout=tmp_path / "unused-final",
                           development_profile=Profile("fixture-development", "high"),
                           review_profile=Profile("fixture-review", "high"),
                           publication_profile=Profile("fixture-publication", "high"))
    assert not destination.exists()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in expected} == expected


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


def test_fixed_3f_completed_ticket_imports_completion_without_rewriting_effects(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-completed-ticket-observation"
    source = oracle / "completed-ticket-v6.sqlite3"
    profile = oracle / "completed-ticket-profile.json"
    github = oracle / "completed-ticket-github.json"
    observation = oracle / "completed-ticket-v6.json"
    bundle = oracle / "completed-ticket-v6.bundle"
    report = oracle / "completed-ticket-original-actual.json"
    git_store = oracle / "completed-ticket.git"
    manifest = oracle.parent / "kernel-3f033-completed-ticket-oracle-green-v1.manifest.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == "641f87252c5b7f7b70f876d08124dea1058efbd7939b01fd743ce8e56ea27a51"
    inventory = json.loads(manifest.read_text(encoding="utf-8"))["git_directory_inventory"]
    assert {str(path.relative_to(git_store)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in git_store.rglob("*") if path.is_file()} == {
                item["path"]: item["sha256"] for item in inventory if item["type"] == "file"}
    expected = {
        source: "8acb67257401b081626734ee26c25c50e590847a701c5defd1127df7f3aed3be",
        profile: "c8cc312a3acce860994c5657b15ff74a7a5fbf75c975f9d0868e2cf89060e455",
        github: "6c86e31f7126a75fa1e079c7dad1c1708ae9a007b91c0ea9a97c5c6aede9ce0f",
        observation: "031456d1eb9868e7b23ea39f68ad8491270a980f36acc9d877b385b47e274758",
        bundle: "4812291fe74a711d1d9a2e4934f7dbad77bc3792e34282b92ffd95c8e41484f7",
        report: "0449baf74b8ff0aa03d4d0fd225b737716dd8cf147142dbb5b0c1ea6245fab0a",
    }
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in expected} == expected

    facts = json.loads(observation.read_text(encoding="utf-8"))
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    root = tmp_path / "continued"
    root.mkdir()
    kernel = convert_completed_ticket_v6(
        snapshot, root / "kernel.sqlite3", git_store=git_store,
        repository_root=root / "authority", final_checkout=root / "work" / "final",
        profile_snapshot=profile, github_snapshot=github,
        observation_snapshot=observation, original_report=report)
    subject = "ticket:3"
    status = kernel.status(subject)
    assert status["phase"] == "complete"
    assert status["pending_write"] is None and status["pending_attempt"] is None
    assert status["candidate"] == facts["candidate_sha"]
    assert status["reviewed_checkout"] is None
    summary = kernel.source_status()
    assert summary["frontier"] == [] and len(summary["completions"]) == 1
    assert summary["completions"][0]["merge_sha"] == facts["integrated_sha"]
    assert summary["completions"][0]["pr_number"] == facts["pull_request"]["number"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts")[0] == 3
        assert tx.one("SELECT count(*) FROM invocations")[0] == 0
        assert tx.one("SELECT count(*) FROM threads")[0] == 0
        assert tx.one("SELECT count(*) FROM effects WHERE status='confirmed'")[0] == 2
        assert tx.one("SELECT count(*) FROM effects WHERE status!='confirmed'")[0] == 0
        assert tx.one("SELECT count(*) FROM history WHERE kind='v6_ticket_phase'")[0] >= 1
        profile_row = tx.one("SELECT subject,material_id FROM profile_revisions WHERE id=1")
        original_profile = json.loads(tx.get(profile_row["material_id"], profile_row["subject"]))
        assert original_profile["preset"] == "economy"
        assert original_profile["profiles"]["development"]["model"] == "gpt-5.6-luna"
    execution = kernel.claim_executor("completed-ticket-executor")
    base = "c33c71b1cc4c536dbeca9a8a5fb2b73bc805cb31"
    tree = "9f71c93390154d6f94bbc99ecddeb154857acde7"
    world = World(status["parent_revision"], status["graph_revision"], status["revision"],
                  Commit(base, "fcc880f6a960cd1c72005bd82499544c16884646"),
                  source=Commit(facts["integrated_sha"], tree))
    assert kernel.advance(subject, execution, world).kind == "complete"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM effects")[0] == 2
    final = kernel.activate_final(execution, Commit(facts["integrated_sha"], tree),
                                  world.target, ancestry=((base, facts["integrated_sha"]),))
    assert kernel.status(final)["phase"] == "workspace"
    assert kernel.status(subject)["phase"] == "complete"
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in expected} == expected


def test_completed_import_keeps_foreign_repository_created_after_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-completed-ticket-observation"
    source = oracle / "completed-ticket-v6.sqlite3"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "8acb67257401b081626734ee26c25c50e590847a701c5defd1127df7f3aed3be"
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    output = tmp_path / "continued"
    output.mkdir()
    repository = output / "foreign-repository"
    sentinel = repository / "belongs-to-other-converter"
    original = completed_module._read_profile

    def foreign_creator(path: Path) -> tuple[dict[str, object], str]:
        repository.mkdir()
        sentinel.write_text("foreign ownership", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(completed_module, "_read_profile", foreign_creator)
    with pytest.raises(MigrationRefused, match="private_destination_exists"):
        convert_completed_ticket_v6(
            snapshot, output / "kernel.sqlite3", git_store=oracle / "completed-ticket.git",
            repository_root=repository, final_checkout=output / "work" / "final",
            profile_snapshot=oracle / "completed-ticket-profile.json",
            github_snapshot=oracle / "completed-ticket-github.json",
            observation_snapshot=oracle / "completed-ticket-v6.json",
            original_report=oracle / "completed-ticket-original-actual.json")
    assert sentinel.read_text(encoding="utf-8") == "foreign ownership"
    assert not (output / "kernel.sqlite3").exists()
