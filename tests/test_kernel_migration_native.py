"""Fixed old artifacts drive native offline conversion without loading the old runtime."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import pytest

import agent_run_kernel.migration_completed as completed_module
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6, read_initial_import
from agent_run_kernel.migration_active import convert_returned_role_v6
from agent_run_kernel.migration_completed import convert_completed_ticket_v6
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import Commit, CreationInputs, Policy, Profile, Requirements, RunSpec
from agent_run_kernel.offline import GraphScenario, OfflinePort, Scenario
from agent_run_kernel.operation_runner import DeliveryFacts
from support.core_operations import CoreOperations, ReadOnlyPorts


def _imported_inputs(kernel: Kernel) -> CreationInputs:
    """Use the converter's frozen native material, never the current user defaults."""
    with kernel.storage.transaction(write=False) as tx:
        inputs = tx.one("SELECT subject,material_id FROM run_inputs")
        body = json.loads(tx.get(inputs["material_id"], inputs["subject"]))
        revision = tx.one("SELECT subject,material_id FROM profile_revisions WHERE id=1")
        profiles = json.loads(tx.get(revision["material_id"], revision["subject"]))
    return CreationInputs(profiles, body["prompt_resources"], body["worker_identity"],
                          body["runner_identity"], body["notifications"], body["provenance"])


def _restore_returned_checkout(
    root: Path, paths: dict[str, Path], facts: dict[str, Any], external: dict[str, Any],
) -> tuple[OfflinePort, Path]:
    port = OfflinePort.create(root)
    run_branch = f"agent-run/{facts['run_id']}/run"
    ticket_branch = f"agent-run/{facts['run_id']}/ticket-3"
    base = external["default_head_sha"]
    assert external["delivery"]["published_branches"][ticket_branch] == base
    port.git.run("fetch", str(paths[".bundle"]), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run",
                 f"refs/heads/{ticket_branch}:refs/remotes/v6/ticket")
    assert port.git.commit("refs/remotes/v6/main").sha == base
    assert port.git.commit("refs/remotes/v6/run").sha == base
    assert port.git.commit("refs/remotes/v6/ticket").sha == facts["ticket_checkout_head"]
    port.git.run("reset", "--hard", base)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base)
    port.git.run("update-ref", f"refs/heads/{ticket_branch}", facts["ticket_checkout_head"])
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (base,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", base))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{ticket_branch}", base))
    checkout = root / "work" / "ticket-3"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), facts["ticket_checkout_head"])
    with tarfile.open(paths["-checkout.tar"], "r:") as archive:
        for member in archive:
            assert member.isfile() and not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            stream = archive.extractfile(member)
            assert stream is not None
            destination = checkout / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(stream.read())
    assert port.git.run("status", "--porcelain", cwd=checkout).stdout.strip().splitlines() == facts["ticket_checkout_status"]
    return port, checkout


def test_disabled_v6_notifications_still_refuse_an_existing_old_journal(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-observation"
    original = oracle / "kernel-initial-v6.sqlite3"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == "64a39d083111ad3625b7da038c2904040da32eb0790415a06a029ff334af1203"
    old_root = tmp_path / "old"
    old_root.mkdir()
    source = old_root / "state.sqlite3"
    source.write_bytes(original.read_bytes())
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    run_id = json.loads((oracle / "kernel-initial-v6.json").read_text(encoding="utf-8"))["run"]["run_id"]
    journal = old_root / "notifications" / (hashlib.sha256(run_id.encode()).hexdigest()[:32] + ".json")
    journal.parent.mkdir()
    journal.write_text('{"seen":[]}', encoding="utf-8")
    destination = tmp_path / "new.sqlite3"
    with pytest.raises(MigrationRefused, match="unsupported_source_notification_journal"):
        convert_initial_v6(snapshot, destination, repository_root=tmp_path / "new-repo",
                           source_ref="refs/heads/converted-ticket", checkout=tmp_path / "checkout",
                           final_checkout=tmp_path / "final",
                           development_profile=Profile("fixture-development", "high"),
                           review_profile=Profile("fixture-review", "high"),
                           publication_profile=Profile("fixture-publication", "high"))
    assert not destination.exists()
    assert journal.read_text(encoding="utf-8") == '{"seen":[]}'


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


def test_returned_development_refuses_unknown_external_write_without_consuming_return(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-returned-development-bound-observation"
    original = oracle / "kernel-returned-development.sqlite3"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == "357705ded49a1a6cba7feb9dc89920f97642db65e603e0b5a42ae3068a918728"
    source = tmp_path / "synthetic.sqlite3"
    source.write_bytes(original.read_bytes())
    observed = json.loads((oracle / "kernel-returned-development.json").read_text(encoding="utf-8"))
    paths = {suffix: oracle / ("kernel-returned-development" + suffix) for suffix in
             (".sqlite3", ".json", "-profile.json", "-github.json", ".bundle", "-checkout.tar")}
    original_hashes = {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()}
    external = json.loads(paths["-github.json"].read_text(encoding="utf-8"))
    root = tmp_path / "continued"
    port, checkout = _restore_returned_checkout(root, paths, observed, external)
    body = '{"display_attempted":true,"status":"indeterminate"}'
    digest = hashlib.sha256(body.encode()).hexdigest()
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO work_evidence_materials VALUES (?,?,?)", (digest, body, len(body)))
        db.execute("INSERT INTO work_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (observed["run_id"], "synthetic-unknown-display", "job:ticket:3:generation:1",
                    "linked_branch_display", 1, digest, len(body), digest, None, None))
        db.execute("UPDATE work_jobs SET value_linked_branch_display=? WHERE stage='developing'",
                   ("synthetic-unknown-display",))
        db.commit()
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    observed["source_sha256"] = snapshot.sha256
    observation = tmp_path / "synthetic-observation.json"
    observation.write_text(json.dumps(observed), encoding="utf-8")
    destination = root / "kernel.sqlite3"
    with pytest.raises(MigrationRefused):
        convert_returned_role_v6(
            snapshot, destination, repository_root=port.git.root,
            checkout=checkout, final_checkout=root / "work" / "final",
            profile_snapshot=oracle / "kernel-returned-development-profile.json",
            github_snapshot=oracle / "kernel-returned-development-github.json",
            observation_snapshot=observation, remote_source_sha=observed["ticket_checkout_head"],
            remote_target_sha=observed["ticket_checkout_head"], checkout_archive=paths["-checkout.tar"])
    assert not destination.exists()
    assert port.writes() == []
    assert source.read_bytes() == snapshot.path.read_bytes()
    assert {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()} == original_hashes


def test_returned_review_refuses_current_context_drift_from_bound_input(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-returned-reviewer-bound-observation"
    paths = {suffix: oracle / ("kernel-returned-reviewer" + suffix) for suffix in
             (".sqlite3", ".json", "-profile.json", "-github.json", ".bundle", "-checkout.tar")}
    assert hashlib.sha256(paths[".sqlite3"].read_bytes()).hexdigest() == "0fc6daa20c4147a4fda55d8edd8eb69eb71bb98a40369adb3c618b68dca57ee3"
    hashes = {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()}
    observed = json.loads(paths[".json"].read_text(encoding="utf-8"))
    external = json.loads(paths["-github.json"].read_text(encoding="utf-8"))
    root = tmp_path / "continued"
    port, checkout = _restore_returned_checkout(root, paths, observed, external)
    source = tmp_path / "synthetic.sqlite3"
    source.write_bytes(paths[".sqlite3"].read_bytes())
    # Append a later CURRENT context using the frozen v6 content identities.
    # The completed Invocation, its startup BOUND version and raw stay intact.
    with closing(sqlite3.connect(source)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        invocation = dict(db.execute("SELECT * FROM work_invocations WHERE role='fresh_acceptance'").fetchone())
        attempt_id = invocation["attempt_id"]
        current = dict(db.execute("SELECT v.* FROM work_attempt_current c JOIN work_attempt_versions v "
                                 "USING(run_id,attempt_id,version_id) WHERE c.attempt_id=?",
                                 (attempt_id,)).fetchone())
        bound = dict(db.execute("SELECT * FROM work_attempt_versions WHERE attempt_id=? AND version_id=?",
                               (attempt_id, invocation["attempt_version_id"])).fetchone())
        evidence = dict(db.execute("SELECT * FROM work_evidence WHERE evidence_id=?",
                                  (current["review_context_evidence_id"],)).fetchone())
        old_body = db.execute("SELECT body FROM work_evidence_materials WHERE material_id=?",
                              (evidence["material_id"],)).fetchone()[0]
        context = json.loads(old_body)
        context["facts"]["human_response_history"] = [{"message": "Later operator clarification."}]
        body = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()
        evidence_id = hashlib.sha256(json.dumps(
            (evidence["owner_id"], "review_context", digest, None, None),
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.execute("INSERT INTO work_evidence_materials VALUES (?,?,?)", (digest, body, len(body.encode())))
        db.execute("INSERT INTO work_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (evidence["run_id"], evidence_id, evidence["owner_id"], "review_context",
                    evidence["version"] + 1, digest, len(body.encode()), digest, None, None))
        current["review_context_evidence_id"] = evidence_id
        current["logical_bytes"] += len(body.encode()) - len(old_body.encode())
        evidence_fields = ("budget_snapshot", "review_context", "history_facts", "review_identity",
                           "development_summary", "publication", "publication_operation_retry",
                           "publication_commit_observation", "returned_result")
        identity = (attempt_id, current["presence"].hex(),
                    tuple(current[key] for key in ("status", "outcome", "budget_consumed")),
                    tuple(current[key + "_evidence_id"] for key in evidence_fields))
        current["version_id"] = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.execute("INSERT INTO work_attempt_versions (" + ",".join(current) + ") VALUES (" +
                   ",".join("?" for _ in current) + ")", tuple(current.values()))
        db.execute("UPDATE work_attempt_current SET version_id=? WHERE attempt_id=?",
                   (current["version_id"], attempt_id))
        db.commit()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert dict(db.execute("SELECT * FROM work_invocations WHERE role='fresh_acceptance'").fetchone()) == invocation
        assert dict(db.execute("SELECT * FROM work_attempt_versions WHERE attempt_id=? AND version_id=?",
                               (attempt_id, invocation["attempt_version_id"])).fetchone()) == bound
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    observed["source_sha256"] = snapshot.sha256
    observed["pending_attempt"]["review_context"] = context
    observation = tmp_path / "synthetic-observation.json"
    observation.write_text(json.dumps(observed), encoding="utf-8")
    destination = root / "kernel.sqlite3"
    with pytest.raises(MigrationRefused):
        convert_returned_role_v6(
            snapshot, destination, repository_root=port.git.root,
            checkout=checkout, final_checkout=root / "work" / "final",
            profile_snapshot=paths["-profile.json"], github_snapshot=paths["-github.json"],
            observation_snapshot=observation, remote_source_sha=external["default_head_sha"],
            remote_target_sha=external["default_head_sha"], checkout_archive=paths["-checkout.tar"])
    assert not destination.exists()
    assert port.writes() == []
    assert source.read_bytes() == snapshot.path.read_bytes()
    assert {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()} == hashes


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
    options = dict(repository_root=port.git.root, source_ref=source_ref, checkout=checkout,
                   final_checkout=root / "work" / "final",
                   development_profile=Profile("fixture-development", "high"),
                   review_profile=Profile("fixture-review", "high"),
                   publication_profile=Profile("fixture-publication", "high"))
    decoded = read_initial_import(snapshot, **options)
    kernel = convert_initial_v6(snapshot, root / "kernel.sqlite3", **options)
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
    scenario.requirements = decoded.observed_requirements
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run"]["run_id"], facts["run"]["repository"],
                            facts["run"]["parent_number"], initial["parent_revision"],
                            initial["graph_revision"], (delivery,), Policy(),
                            inputs=_imported_inputs(kernel))
    scenario.delivery = delivery
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("fixed-oracle-executor")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.check_heads = {}
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    boundary = scenario.run()
    assert boundary.kind == "complete", boundary
    assert kernel.status(delivery.key)["phase"] == "complete"
    assert any(write["operation"] == "merge" for write in port.writes())
    assert any(write["operation"] == "completion_comment" for write in port.writes())
    assert source.read_bytes() == snapshot.path.read_bytes()


def test_fixed_13e_idle_graph_converts_then_completes_dependencies_and_final(tmp_path: Path) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-graph-observation-v2"
    source = oracle / "kernel-initial-graph-v6.sqlite3"
    bundle = oracle / "kernel-initial-graph-v6.bundle"
    github = oracle / "kernel-initial-graph-github.json"
    observed = oracle / "kernel-initial-graph-v6.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "eb74936b5b71b194ca657a617b5ba078c31e689bed55e040d9f22fd438e827b8"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "5f4f9374d94a96c915d39862e1c491d72f74e3c0a65eec58bcc8cb72dd7043fb"
    assert hashlib.sha256(github.read_bytes()).hexdigest() == "9dccc4637568d2b597e2dd0292b4a504260d022967bf65962723de1e22c05ab4"
    assert hashlib.sha256(observed.read_bytes()).hexdigest() == "426ceb76f15040f674fb04df2e1f112c86fc41e562f68d2687541d3abb4fd2f9"
    facts = json.loads(observed.read_text(encoding="utf-8"))
    github_facts = json.loads(github.read_text(encoding="utf-8"))
    requirements = Requirements.from_wire(github_facts)
    assert facts["order"] == [2, 3] and facts["layout"]["active_job_id"] == "ticket:2:selection"
    snapshot = capture_v6(source, tmp_path / "captured.sqlite3")
    root = tmp_path / "new-runtime"
    port = OfflinePort.create(root)
    run_branch = facts["root"]["value_run_branch"]
    base_sha = facts["refs"][run_branch][0]
    assert github_facts["default_branch"] == "main"
    assert facts["refs"][run_branch][0] == github_facts["default_head_sha"]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run")
    assert port.git.commit("refs/remotes/v6/main").sha == base_sha
    assert port.git.commit("refs/remotes/v6/run").sha == base_sha
    port.git.run("reset", "--hard", base_sha)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base_sha)
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (base_sha,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", base_sha))
    work = root / "work"
    work.mkdir()
    for number in (2, 3):
        port.git.run("worktree", "add", "--detach", str(work / f"ticket-{number}"), base_sha)
    refs = {number: f"refs/heads/agent-run/{facts['run']['run_id']}/converted-ticket-{number}"
            for number in (2, 3)}
    kernel = convert_initial_v6(
        snapshot, root / "kernel.sqlite3", repository_root=port.git.root,
        source_ref=refs[2], checkout=work / "ticket-2", final_checkout=work / "final",
        development_profile=Profile("fixture-development", "high"),
        review_profile=Profile("fixture-review", "high"),
        publication_profile=Profile("fixture-publication", "high"),
        additional_workspaces={3: (refs[3], work / "ticket-3")})
    assert all(port.ref_commit(ref) is None for ref in refs.values())
    assert [kernel.status(f"ticket:{number}")["phase"] for number in (2, 3)] == ["workspace", "workspace"]
    assert kernel.source_status()["remaining"] == {"ticket:3": "blocked_by_incomplete_tickets"}
    scenario = GraphScenario.__new__(GraphScenario)
    scenario.requirements = requirements
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run"]["run_id"], facts["run"]["repository"],
                            facts["run"]["parent_number"], kernel.status("ticket:2")["parent_revision"],
                            kernel.status("ticket:2")["graph_revision"],
                            tuple(kernel.delivery(f"ticket:{number}") for number in (2, 3)), Policy(),
                            inputs=_imported_inputs(kernel))
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("fixed-graph-executor")
    scenario.delivery = kernel.delivery("ticket:2")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.check_heads = {}
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    base = port.remote_commit("refs/heads/main")
    assert base is not None
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    assert operations.once(agent_capacity=0).call is None
    assert kernel.source_status()["final_subject"] is None
    assert scenario.run().kind == "complete"
    assert [entry["subject"] for entry in kernel.source_status()["completions"]] == ["ticket:2"]
    scenario.select("ticket:3")
    assert scenario.run().kind == "complete"
    assert [entry["subject"] for entry in kernel.source_status()["completions"]] == ["ticket:2", "ticket:3"]
    scenario.refresh()
    head = port.remote_commit(f"refs/heads/{run_branch}")
    default = port.remote_commit("refs/heads/main")
    assert head is not None and default is not None
    ancestry = tuple((entry["merge_sha"], head.sha) for entry in kernel.source_status()["completions"]
                     if port.git.is_ancestor(entry["merge_sha"], head.sha))
    assert len(ancestry) == len(kernel.source_status()["completions"])
    operations.until(lambda _step: kernel.source_status()["final_subject"] is not None,
                     limit=10, agent_capacity=0)
    final = kernel.source_status()["final_subject"]
    scenario.select(final)
    assert scenario.run().kind == "approval"
    kernel.control("approve", "fixed-graph-final-approval", subject=final, world=scenario.world())
    assert scenario.run().kind == "complete"
    assert kernel.status(final)["phase"] == "complete"
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
    kernel = convert_returned_role_v6(
        snapshot, root / "kernel.sqlite3", repository_root=port.git.root,
        checkout=checkout, final_checkout=root / "work" / "final", profile_snapshot=profile,
        observation_snapshot=observation, github_snapshot=github,
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
    scenario.requirements = Requirements.from_wire(external)
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run_id"], facts["repository"], facts["parent_number"],
                            current["parent_revision"], current["graph_revision"], (delivery,), Policy(),
                            inputs=_imported_inputs(kernel))
    scenario.delivery = delivery
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("returned-publication-executor")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.check_heads = {}
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    boundary = scenario.run()
    assert boundary.kind == "complete", boundary
    assert kernel.status(subject)["phase"] == "complete"
    assert scenario.role_calls == []
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject=? AND purpose='requirements'",
                            (subject,))) == (1, 1)
    assert any(write["operation"] == "merge" for write in port.writes())
    assert any(write["operation"] == "completion_comment" for write in port.writes())
    assert source.read_bytes() == snapshot.path.read_bytes()


@pytest.mark.parametrize("role", ["development", "reviewer"])
def test_bound_returned_role_consumes_exact_old_raw(tmp_path: Path, role: str) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / f"kernel-returned-{role}-bound-observation"
    name = "kernel-returned-" + role
    paths = {suffix: oracle / (name + suffix) for suffix in
             (".sqlite3", ".json", "-profile.json", "-github.json", ".bundle", "-checkout.tar")}
    known = {
      "development": {
        ".sqlite3": "357705ded49a1a6cba7feb9dc89920f97642db65e603e0b5a42ae3068a918728",
        ".json": "ff1f972f6996a9829423cbd96488c112f84c7a2507f43f8d3d3bda9d89e4aba9",
        "-profile.json": "c17bf3aa1553dc3aaa0914771d045efffab6019620213af4d45ec584d7d51898",
        "-github.json": "88e4c45ee0865c5543666fb5ace720c447bce0f6a7854e473413944882385bd7",
        ".bundle": "b051561c7fe23eb11dbf0194e7c29f428aa5b9d035f2ece7bef173993856a8c4",
        "-checkout.tar": "66fa44450a0034bcbcb1c0ae06f82c40baf8a280ff4f086fb64fab39a6c06e6c",
      },
      "reviewer": {
        ".sqlite3": "0fc6daa20c4147a4fda55d8edd8eb69eb71bb98a40369adb3c618b68dca57ee3",
        ".json": "d4f24c6a2b77a597d8ff47a46c2b81b0f6942f3bb9874234a2b19e4098179f77",
        "-profile.json": "89f94880b3ea883d3bab7539869df133e90bd8b410f46dd6460a9c048d2520d3",
        "-github.json": "8587bb9b673b51a578f8bf233bf14b43b13c6a6799e08441f891db6b004a230f",
        ".bundle": "8e1df5d237c2837e1c1ccb92d683129d208d19c264befa7c2ab46a98c72116ac",
        "-checkout.tar": "d60bbea4d74d72c47e344e1af4cfc19cd3776ae3ccc2a33b85934a3d240d56ec",
      },
    }
    expected = known[role]
    assert {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()} == expected
    facts = json.loads(paths[".json"].read_text(encoding="utf-8"))
    external = json.loads(paths["-github.json"].read_text(encoding="utf-8"))
    assert facts["checkout_archive_sha256"] == expected["-checkout.tar"]
    assert facts["role"] == role
    assert facts["ticket_checkout_status"] == (["?? delivered.txt"] if role == "development" else [])
    snapshot = capture_v6(paths[".sqlite3"], tmp_path / "captured.sqlite3")
    root = tmp_path / "continued"
    port, checkout = _restore_returned_checkout(root, paths, facts, external)
    ticket_branch = f"agent-run/{facts['run_id']}/ticket-3"
    base = external["default_head_sha"]
    kernel = convert_returned_role_v6(
        snapshot, root / "kernel.sqlite3", repository_root=port.git.root,
        checkout=checkout, final_checkout=root / "work" / "final",
        profile_snapshot=paths["-profile.json"], observation_snapshot=paths[".json"],
        github_snapshot=paths["-github.json"],
        checkout_archive=paths["-checkout.tar"], remote_source_sha=base, remote_target_sha=base)
    subject = "ticket:3"
    assert kernel.status(subject)["phase"] == ("development" if role == "development" else "review")
    assert kernel.status(subject)["pending_attempt"] == facts["pending_attempt"]["attempt_id"]
    assert port.ref_commit(f"refs/heads/{ticket_branch}").sha == base
    if role == "reviewer":
        assert port.git.commit(f"refs/agent-run-kernel-migration/{facts['run_id']}/ticket-3").sha == facts["ticket_checkout_head"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 0
        assert tx.one("SELECT count(*) FROM attempts")[0] == (1 if role == "development" else 2)
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject=? AND purpose='requirements'",
                            (subject,))) == (1, 0)
        witnesses = [json.loads(tx.get(row[0], subject)) for row in
                     tx.rows("SELECT material_id FROM history WHERE kind='imported_invocation' ORDER BY position")]
        witness = witnesses[-1]
        assert witness["bound_version"]["version_id"] != witness["current_version"]["version_id"]
        assert witness["invocation"]["input_fingerprint"] == facts["completed_invocation"]["input_fingerprint"]
        if role == "development":
            intent = tx.one("SELECT id FROM material WHERE subject=? AND purpose='v6_candidate_commit_intent'", (subject,))
            assert json.loads(tx.get(intent[0], subject)) == {
                "run_id": "run-1-337b38adb81b2187", "job_id": "ticket:3:generation:1",
                "presence_hex": "0f", "nulls_hex": "00", "value_kind": "candidate_commit",
                "value_expected_head": "82bce20949af5a8edcd11278407738529f0d8a4b",
                "value_attempt": 1, "value_token": "3aba31594f394d4d990be548de2182ce",
            }
    assert port.writes() == []
    delivery = kernel.delivery(subject)
    current = kernel.status(subject)
    scenario = Scenario.__new__(Scenario)
    scenario.requirements = Requirements.from_wire(external)
    scenario.script = None
    scenario.port = port
    scenario.initial = port.git.commit("refs/heads/main")
    scenario.spec = RunSpec(facts["run_id"], facts["repository"], facts["parent_number"],
                            current["parent_revision"], current["graph_revision"], (delivery,), Policy(),
                            inputs=_imported_inputs(kernel))
    scenario.delivery = delivery
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor("returned-role-executor")
    scenario.reviews = ["pass"]
    scenario.checks = ["none"]
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads = []
    scenario.check_heads = {}
    scenario.role_calls = []
    scenario.checkpoints = []
    scenario.edits = None
    scenario.reviewed_heads = []
    boundary = scenario.run()
    assert boundary.kind == "complete", boundary
    assert kernel.status(subject)["phase"] == "complete"
    assert [call["role"] for call in scenario.role_calls] == (
        ["review", "publication"] if role == "development" else ["publication"])
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject=? AND purpose='requirements'",
                            (subject,))) == (1, 1)
    assert any(write["operation"] == "candidate" for write in port.writes()) == (role == "development")
    assert any(write["operation"] == "merge" for write in port.writes())
    assert paths[".sqlite3"].read_bytes() == snapshot.path.read_bytes()
    assert {suffix: hashlib.sha256(path.read_bytes()).hexdigest() for suffix, path in paths.items()} == expected


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
    github_facts = json.loads(github.read_text(encoding="utf-8"))
    source_ref = kernel.delivery(subject).target_ref
    target_ref = "refs/heads/" + github_facts["default_branch"]
    git = LocalGit(root / "authority")
    source_head, target_head = git.commit(source_ref), git.commit(target_ref)
    assert source_head == Commit(facts["integrated_sha"], tree)
    assert target_head == Commit(base, "fcc880f6a960cd1c72005bd82499544c16884646")
    assert git.is_ancestor(base, facts["integrated_sha"])
    ports = ReadOnlyPorts(Requirements.from_wire(github_facts), source_ref, target_ref,
                          DeliveryFacts(target_head, source_head,
                                        ancestry=((base, facts["integrated_sha"]),)))
    operations = CoreOperations(kernel, execution, lambda _binding: ports)
    assert operations.once(agent_capacity=0).call is None
    assert kernel.status(subject)["phase"] == "complete"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM effects")[0] == 2
    final = kernel.source_status()["final_subject"]
    assert final is not None
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
