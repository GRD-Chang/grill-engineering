"""Strict offline import of an idle selected v6 Ticket into the new kernel.

Only copied, accepted source facts cross this seam. A selected Ticket with no
Attempt, Invocation, control action, or external write may receive a new
explicit worktree identity. Later v6 states require separate mappings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import os
import sqlite3
import subprocess
import tempfile
from typing import Any, Callable, Mapping, TypeVar, cast

from .effects import LocalGit
from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _created_at_epoch,
                        _evidence_value, _require_no_v6_notification_journal, capture_v6, validate_snapshot)
from .migration_facts import (ImportSource, ImportedHistoryEvent, ImportedResource,
                              ImportedRunFacts, ImportedSubject, SourceRecord)
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue, ParentIssue,
                    KernelError, Policy, Profile, Requirements, RunSpec, wire_json)


_POPULATED_INITIAL = frozenset({
    "schema_meta", "runs", "run_root", "run_condition_base", "run_parent_versions",
    "run_graph_versions", "run_graph_snapshots", "run_graph_scope_order",
    "run_graph_ticket_slots", "run_graph_blocker_slots", "run_graph_edges",
    "run_graph_members", "run_frontier", "run_query_roots",
    "run_resume_roots", "run_timeline_events", "run_timeline_slots", "work_jobs",
    "work_job_slots", "work_job_layout", "work_invocation_roots", "work_evidence",
    "work_evidence_materials",
})
_POLICY_KEYS = frozenset({
    "ticket_development_workers", "parent_only_paired_rounds", "run_repair_rounds",
    "ticket_review_rounds", "integration_resync_limit",
    "ticket_integration_development_limit", "ticket_ci_development_limit",
    "ticket_ci_review", "parent_ci_development_limit", "development_thread_policy",
    "invocation_deadlines",
})


@dataclass(frozen=True)
class _InitialFacts:
    run_id: str
    repository: str
    parent_number: int
    parent_revision: str
    graph_revision: str
    created_at: float
    language: str
    run_branch: str
    base_branch: str
    base_sha: str
    ticket_number: int
    requirements: Requirements
    policy: Policy
    frozen: dict[str, Any]
    frozen_ids: dict[str, str]
    notifications: dict[str, Any]
    source_evidence: tuple[dict[str, Any], ...]
    timeline: tuple[dict[str, Any], ...]


def _one(db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    rows = db.execute(sql, params).fetchmany(2)
    if len(rows) != 1:
        raise MigrationRefused("source_fact_missing_or_ambiguous")
    return cast(sqlite3.Row, rows[0])


def _policy(value: object) -> Policy:
    if not isinstance(value, dict) or set(value) != _POLICY_KEYS:
        raise MigrationRefused("source_policy")
    deadlines = value["invocation_deadlines"]
    if not isinstance(deadlines, dict) or set(deadlines) != {"development", "review", "publication"}:
        raise MigrationRefused("source_policy")
    thread = value["development_thread_policy"]
    if thread not in {"reuse", "new-per-attempt"}:
        raise MigrationRefused("source_policy")
    try:
        return Policy(ticket_rounds=value["ticket_review_rounds"],
                      parent_rounds=value["parent_only_paired_rounds"],
                      run_repairs=value["run_repair_rounds"],
                      integration_developments=value["ticket_integration_development_limit"],
                      ticket_ci_developments=value["ticket_ci_development_limit"],
                      parent_ci_developments=value["parent_ci_development_limit"],
                      resync_limit=value["integration_resync_limit"],
                      ticket_ci_review=value["ticket_ci_review"],
                      concurrency=value["ticket_development_workers"],
                      new_thread_per_development=thread == "new-per-attempt",
                      development_deadline_seconds=deadlines["development"],
                      review_deadline_seconds=deadlines["review"],
                      publication_deadline_seconds=deadlines["publication"])
    except (TypeError, ValueError) as error:
        raise MigrationRefused("source_policy") from error


_Decoded = TypeVar("_Decoded")


def _read_initial(path: Path, emit: Callable[..., _Decoded]) -> _Decoded:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if any(count and name not in _POPULATED_INITIAL for name, count in counts.items()):
            raise MigrationRefused("unsupported_source_relations")
        expected = {"runs": 1, "run_root": 1, "run_condition_base": 1,
                    "work_jobs": 1, "work_job_slots": 1, "work_job_layout": 1,
                    "run_timeline_events": 2, "run_timeline_slots": 2}
        if any(counts.get(name) != count for name, count in expected.items()):
            raise MigrationRefused("unsupported_source_stage")
        run = _one(db, "SELECT * FROM runs")
        root = _one(db, "SELECT * FROM run_root WHERE run_id=?", (run["run_id"],))
        if (run["status"] != "active" or run["phase"] is not None
                or root["value_language"] not in {"zh", "en"}
                or not root["value_accepted_parent_spec_revision"]
                or not root["value_accepted_ticket_graph_revision"]):
            raise MigrationRefused("unsupported_source_stage")
        job = _one(db, "SELECT * FROM work_jobs WHERE run_id=?", (run["run_id"],))
        slot = _one(db, "SELECT * FROM work_job_slots WHERE run_id=?", (run["run_id"],))
        layout = _one(db, "SELECT * FROM work_job_layout WHERE run_id=?", (run["run_id"],))
        ticket = job["value_ticket_number"]
        if (job["job_id"] != f"ticket:{ticket}:selection" or job["owner_kind"] != "ticket"
                or job["owner_key"] != str(ticket) or job["stage"] is not None
                or slot["job_id"] != job["job_id"] or slot["container"] != "ticket_jobs"
                or slot["slot"] != str(ticket) or layout["active_job_id"] != job["job_id"]
                or counts.get("work_attempts", 0) or counts.get("work_invocations", 0)):
            raise MigrationRefused("unsupported_source_stage")
        parent = _one(db, "SELECT * FROM run_parent_versions WHERE run_id=? AND version_id=?",
                      (run["run_id"], root["value_parent_version_id"]))
        title = _evidence_value(db, run["run_id"], parent["title_id"], "parent.title")
        body = _evidence_value(db, run["run_id"], parent["body_id"], "parent.body")
        graph = _one(db, "SELECT g.version_id,g.revision,s.evidence_id FROM run_graph_snapshots s "
                     "JOIN run_graph_versions g ON g.run_id=s.run_id AND g.version_id=s.version_id "
                     "WHERE s.run_id=? AND s.snapshot_id=?",
                     (run["run_id"], root["value_graph_snapshot_id"]))
        observation = _evidence_value(db, run["run_id"], graph["evidence_id"], "graph.observation")
        scope = db.execute("SELECT position,ticket_number FROM run_graph_scope_order "
                           "WHERE run_id=? AND snapshot_id=? ORDER BY position",
                           (run["run_id"], root["value_graph_snapshot_id"])).fetchall()
        ticket_slots = db.execute("SELECT position,ticket_number,number_present FROM run_graph_ticket_slots "
                                  "WHERE run_id=? AND snapshot_id=? ORDER BY position",
                                  (run["run_id"], root["value_graph_snapshot_id"])).fetchall()
        order = [row["ticket_number"] for row in scope]
        if (not isinstance(title, str) or not isinstance(body, str)
                or not isinstance(observation, dict) or not order or ticket not in order
                or len(set(order)) != len(order)
                or [row["position"] for row in scope] != list(range(len(scope)))
                or [(row["position"], row["ticket_number"], row["number_present"])
                    for row in ticket_slots] != [(index, number, 1) for index, number in enumerate(order)]
                or not isinstance(observation.get("tickets"), list)
                or len(observation["tickets"]) != len(order)
                or observation.get("fields", {}).get("order_source") != "github_sub_issues"):
            raise MigrationRefused("source_requirements")
        member_numbers = [row[0] for row in db.execute(
            "SELECT ticket_number FROM run_graph_members WHERE run_id=? AND version_id=? ORDER BY ticket_number",
            (run["run_id"], graph["version_id"]))]
        blocker_slots = db.execute(
            "SELECT ticket_position,position,edge_id,number_present FROM run_graph_blocker_slots "
            "WHERE run_id=? AND snapshot_id=? ORDER BY ticket_position,position",
            (run["run_id"], root["value_graph_snapshot_id"])).fetchall()
        edges = db.execute(
            "SELECT edge_id,ticket_number,blocker_number FROM run_graph_edges "
            "WHERE run_id=? AND version_id=?", (run["run_id"], graph["version_id"])).fetchall()
        edge_by_id = {row["edge_id"]: row for row in edges}
        if member_numbers != sorted(order) or len(edge_by_id) != len(edges):
            raise MigrationRefused("source_requirements")
        frontier = db.execute("SELECT position,ticket_number FROM run_frontier WHERE run_id=? ORDER BY position",
                              (run["run_id"],)).fetchall()
        eligible = [number for number, item in zip(order, observation["tickets"])
                    if isinstance(item, dict) and isinstance(item.get("eligibility"), dict)
                    and item["eligibility"].get("eligible") is True]
        if ([row["position"] for row in frontier] != list(range(len(frontier)))
                or [row["ticket_number"] for row in frontier] != eligible or ticket not in eligible):
            raise MigrationRefused("source_selection")
        try:
            issues = []
            used_edges: set[str] = set()
            for position, number in enumerate(order):
                item = observation["tickets"][position]
                matching = [row for row in blocker_slots if row["ticket_position"] == position]
                if ([row["position"] for row in matching] != list(range(len(matching)))
                        or len(matching) != len(item["blocked_by"])):
                    raise MigrationRefused("source_requirements")
                blockers = []
                for slot, value in zip(matching, item["blocked_by"]):
                    edge = edge_by_id[slot["edge_id"]]
                    if (slot["number_present"] != 1 or edge["ticket_number"] != number
                            or edge["edge_id"] in used_edges):
                        raise MigrationRefused("source_requirements")
                    used_edges.add(edge["edge_id"])
                    blockers.append(Blocker(edge["blocker_number"], value["state"]))
                issues.append(Issue(number, item["title"], item["body"], item["state"],
                                    frozenset(item["labels"]), tuple(blockers)))
            if len(used_edges) != len(edges):
                raise MigrationRefused("source_requirements")
            requirements = Requirements(ParentIssue(run["parent_number"], title, body,
                                                    tuple(order), True), tuple(issues))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise MigrationRefused("source_requirements") from error
        parent_revision = Kernel._fingerprint({"title": title, "body": body})
        graph_revision = Kernel._fingerprint({
            "tickets": sorted(order),
            "dependencies": {str(issue.number): sorted(edge.number for edge in issue.blocked_by)
                             for issue in issues},
        })
        if (parent["revision"] != parent_revision
                or root["value_accepted_parent_spec_revision"] != parent_revision
                or graph["revision"] != graph_revision
                or root["value_accepted_ticket_graph_revision"] != graph_revision
                or any(item["content_revision"] != Kernel._fingerprint({
                    "title": issue.title, "body": issue.body})
                    for item, issue in zip(observation["tickets"], issues))):
            raise MigrationRefused("source_revision")
        base = _one(db, "SELECT * FROM run_condition_base WHERE run_id=?", (run["run_id"],))
        if (not base["value_sha"] or not base["value_branch"]
                or not isinstance(root["value_run_branch"], str)
                or not root["value_run_branch"].startswith(f"agent-run/{run['run_id']}/")):
            raise MigrationRefused("source_base")
        frozen_ids = {kind: root[field] for field, kind in (
            ("value_policy_snapshot", "policy_snapshot"),
            ("value_prompt_resources", "prompt_resources"),
            ("value_github_identity", "github_identity"),
            ("value_runner_github_identity", "runner_github_identity"))}
        frozen = {kind: _evidence_value(db, run["run_id"], evidence_id, kind)
                  for kind, evidence_id in frozen_ids.items()}
        query = _one(db, "SELECT presence FROM run_query_roots WHERE run_id=?", (run["run_id"],))
        mask = query["presence"]
        if (not isinstance(mask, bytes) or len(mask) != 1 or mask[0] & 0x80
                or mask[0] & 0x04 or counts.get("run_notification_config", 0)):
            # The old sorted seven-field mask puts notifications at bit 2.
            # A configured record needs a separate exact mapping; a present
            # bit with no record is corrupt under the old loader contract.
            raise MigrationRefused("unsupported_source_notifications")
        source_evidence = tuple({
            "evidence_id": row["evidence_id"], "kind": row["kind"], "version": row["version"],
            "value": _evidence_value(db, run["run_id"], row["evidence_id"], row["kind"]),
        } for row in db.execute("SELECT evidence_id,kind,version FROM work_evidence WHERE run_id=? ORDER BY evidence_id",
                               (run["run_id"],)))
        if len(source_evidence) != counts["work_evidence"] or db.execute(
            "SELECT 1 FROM work_evidence_materials m WHERE NOT EXISTS "
            "(SELECT 1 FROM work_evidence e WHERE e.material_id=m.material_id) LIMIT 1"
        ).fetchone() is not None:
            raise MigrationRefused("source_evidence")
        events = [dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run["run_id"],))]
        if ([event["value_kind"] for event in events] != ["run_status", "ticket_phase"]
                or events[0]["value_status"] != "starting" or events[1]["value_phase"] != "pending"
                or events[1]["value_ticket"] != ticket):
            raise MigrationRefused("unsupported_source_history")
        return emit(db, run["run_id"], run["repository"], run["parent_number"], parent_revision,
                             graph_revision, _created_at_epoch(run["created_at"]), root["value_language"],
                             root["value_run_branch"], base["value_branch"], base["value_sha"], ticket, requirements,
                             _policy(frozen["policy_snapshot"]), frozen, frozen_ids,
                             {"enabled": False}, source_evidence, tuple(events))
    except sqlite3.Error as error:
        raise MigrationRefused("source_corrupt") from error
    finally:
        db.close()


def _initial_facts(path: Path) -> _InitialFacts:
    # Temporary consumer of the same checked SQL until the unique installer is
    # wired. The canonical decoder never constructs this legacy writer DTO.
    def legacy(db: sqlite3.Connection, *values: Any) -> _InitialFacts:
        return _InitialFacts(*values)
    return _read_initial(path, legacy)


def _git(root: Path, *args: str) -> str:
    try:
        result = LocalGit(root).run(*args)
    except (OSError, KernelError, subprocess.TimeoutExpired) as error:
        raise MigrationRefused("source_git") from error
    return result.stdout.strip()


def convert_initial_v6(snapshot: SourceSnapshot, destination: Path, *, repository_root: Path,
                       source_ref: str, checkout: Path, final_checkout: Path,
                       development_profile: Profile, review_profile: Profile,
                       publication_profile: Profile,
                       additional_workspaces: Mapping[int, tuple[str, Path]] | None = None) -> Kernel:
    """Convert an idle accepted graph; publish only after full copied-state validation.

    The caller supplies new, unused work identities because no old Ticket in
    this no-Attempt state has allocated a branch or worktree.
    """
    destination = destination.absolute()
    repository_root, checkout, final_checkout = (repository_root.resolve(), checkout.resolve(),
                                                  final_checkout.resolve())
    if destination.exists() or destination.is_symlink():
        raise MigrationRefused("destination_exists")
    if not source_ref.startswith("refs/heads/") or source_ref == "refs/heads/":
        raise MigrationRefused("new_source_ref")
    profiles = {"development": development_profile, "review": review_profile,
                "publication": publication_profile}
    if any(not profile.model or not profile.effort or profile.model == "configured-model"
           or profile.effort == "configured-effort" for profile in profiles.values()):
        raise MigrationRefused("new_profile_source")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".v6-convert-") as directory:
        captured = capture_v6(snapshot.path, Path(directory) / "source.sqlite3")
        if ((captured.sha256, captured.source_protocol, captured.schema_sha256, captured.table_rows)
                != (snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows)):
            raise MigrationRefused("snapshot_changed")
        facts = _initial_facts(captured.path)
        decoded = read_initial_import(replace(captured, source=snapshot.source), repository_root=repository_root,
            source_ref=source_ref, checkout=checkout, final_checkout=final_checkout,
            development_profile=development_profile, review_profile=review_profile,
            publication_profile=publication_profile, additional_workspaces=additional_workspaces)
        spec = decoded.spec
        subject = f"ticket:{facts.ticket_number}"
        workspaces = {delivery.issue: (delivery.source_ref, Path(delivery.checkout))
                      for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET}
        candidate = Path(directory) / "kernel.sqlite3"
        kernel = Kernel.create(candidate, spec, requirements=facts.requirements)
        with kernel.storage.transaction() as tx:
            run = tx.one("SELECT parent_revision,graph_revision,created_at FROM runs")
            if (run["parent_revision"], run["graph_revision"]) != (facts.parent_revision, facts.graph_revision):
                raise MigrationRefused("source_revision")
            tx.execute("UPDATE runs SET created_at=?", (facts.created_at,))
            # The old selection has no Ticket workspace or branch yet. Enter
            # the kernel's existing durable workspace effect so its normal
            # CAS, receipt, and unknown-outcome path allocates the new ref.
            for number in facts.requirements.parent.sub_issue_numbers:
                tx.execute("UPDATE subjects SET phase='workspace' WHERE id=?", (f"ticket:{number}",))
            tx.execute("DELETE FROM history")
            for event in facts.timeline:
                at = _created_at_epoch(event["value_at"])
                body = {"event_id": event["event_id"], "presence_hex": event["presence"].hex(),
                        "values": {key: value for key, value in event.items()
                                   if key.startswith("value_") and value is not None}}
                identity = event["event_id"]
                material = tx.put(subject, "v6_timeline_event", wire_json(body))
                tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                           "VALUES (?,?,?,?,1,?)", (subject, f"v6_{event['value_kind']}", identity, material, at))
            for kind, value in facts.frozen.items():
                tx.put(subject, f"v6_{kind}", wire_json(value))
            for evidence in facts.source_evidence:
                tx.put(subject, "v6_evidence", wire_json(evidence))
            tx.put(subject, "v6_import_identity", wire_json({
                "source_sha256": snapshot.sha256, "source_protocol": snapshot.source_protocol,
                "new_source_ref": source_ref, "new_checkout": str(checkout),
                "new_ticket_workspaces": {str(number): {"source_ref": ref, "checkout": str(path)}
                                          for number, (ref, path) in workspaces.items()},
                "new_final_checkout": str(final_checkout),
                "old_run_branch": facts.run_branch, "old_base_sha": facts.base_sha,
                "new_profile_source": "explicit-migration-input",
                "new_profiles": {key: asdict(value) for key, value in profiles.items()},
            }))
        status = kernel.status(subject)
        selected = next(issue for issue in facts.requirements.issues if issue.number == facts.ticket_number)
        if (status["phase"] != "workspace" or status["pending_attempt"] is not None
                or status["revision"] != Kernel._fingerprint({
                    "title": selected.title, "body": selected.body})):
            raise MigrationRefused("converted_state")
        if any(kernel.status(f"ticket:{number}")["phase"] != "workspace"
               for number in facts.requirements.parent.sub_issue_numbers):
            raise MigrationRefused("converted_state")
        if any(Path(str(candidate) + suffix).exists() and Path(str(candidate) + suffix).stat().st_size
               for suffix in ("-wal", "-journal")):
            raise MigrationRefused("converted_sidecar")
        try:
            os.link(candidate, destination)
        except FileExistsError as error:
            raise MigrationRefused("destination_exists") from error
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return Kernel(destination)


def read_initial_import(snapshot: SourceSnapshot, *, repository_root: Path,
                        source_ref: str, checkout: Path, final_checkout: Path,
                        development_profile: Profile, review_profile: Profile,
                        publication_profile: Profile,
                        additional_workspaces: Mapping[int, tuple[str, Path]] | None = None) -> ImportedRunFacts:
    """Decode a sealed idle source directly, observing only explicit new layouts.

    The legacy converter still has its writer until unique installation is
    connected. Its DTO is not an intermediate on this decoder path. SQL stage,
    graph, policy, notification and evidence checks are shared, not weakened.
    """
    validate_snapshot(snapshot)
    repository_root, checkout, final_checkout = (repository_root.resolve(), checkout.resolve(),
                                                 final_checkout.resolve())
    if not source_ref.startswith("refs/heads/") or source_ref == "refs/heads/":
        raise MigrationRefused("new_source_ref")
    profiles = {"development": development_profile, "review": review_profile,
                "publication": publication_profile}
    if any(not profile.model or not profile.effort or profile.model == "configured-model"
           or profile.effort == "configured-effort" for profile in profiles.values()):
        raise MigrationRefused("new_profile_source")

    def decode(db: sqlite3.Connection, run_id: str, repository: str, parent_number: int,
               parent_revision: str, graph_revision: str, created_at: float, language: str,
               run_branch: str, base_branch: str, base_sha: str, ticket_number: int,
               requirements: Requirements, policy: Policy, frozen: dict[str, Any],
               frozen_ids: dict[str, str], notifications: dict[str, Any],
               source_evidence: tuple[dict[str, Any], ...],
               timeline: tuple[dict[str, Any], ...]) -> ImportedRunFacts:
        _require_no_v6_notification_journal(snapshot, run_id)
        additional = additional_workspaces or {}
        expected_additional = set(requirements.parent.sub_issue_numbers) - {ticket_number}
        if set(additional) != expected_additional:
            raise MigrationRefused("new_ticket_workspaces")
        workspaces = {ticket_number: (source_ref, checkout), **{
            number: (ref, path.resolve()) for number, (ref, path) in additional.items()}}
        if (len({ref for ref, _ in workspaces.values()}) != len(workspaces)
                or len({path for _, path in workspaces.values()}) != len(workspaces)):
            raise MigrationRefused("new_ticket_workspaces")
        if _git(repository_root, "rev-parse", "--show-toplevel") != str(repository_root):
            raise MigrationRefused("source_git")
        for ref in (*[value[0] for value in workspaces.values()],
                    f"refs/heads/{run_branch}", f"refs/heads/{base_branch}"):
            _git(repository_root, "check-ref-format", ref)
        if _git(repository_root, "rev-parse", "--verify", f"refs/heads/{run_branch}^{{commit}}") != base_sha:
            raise MigrationRefused("source_git")
        if _git(repository_root, "rev-parse", "--verify", f"refs/heads/{base_branch}^{{commit}}") != base_sha:
            raise MigrationRefused("source_git")
        tree = _git(repository_root, "rev-parse", f"{base_sha}^{{tree}}")
        if not tree:
            raise MigrationRefused("source_git")
        common = (repository_root / ".git").resolve()
        for ref, path in workspaces.values():
            if not ref.startswith("refs/heads/") or ref == "refs/heads/":
                raise MigrationRefused("new_source_ref")
            if _git(repository_root, "for-each-ref", "--format=%(refname)", ref):
                raise MigrationRefused("new_source_ref_exists")
            if (not path.is_dir() or path == repository_root
                    or _git(path, "rev-parse", "--show-toplevel") != str(path)
                    or _git(path, "rev-parse", "HEAD") != base_sha):
                raise MigrationRefused("new_checkout")
            if _git(path, "status", "--porcelain"):
                raise MigrationRefused("new_checkout_dirty")
            common_value = Path(_git(path, "rev-parse", "--git-common-dir") or "")
            found_common = (common_value if common_value.is_absolute() else path / common_value).resolve()
            if found_common != common:
                raise MigrationRefused("new_checkout")
        if final_checkout == repository_root or final_checkout in {path for _, path in workspaces.values()} or final_checkout.exists():
            raise MigrationRefused("new_final_checkout")
        profile_document = {"preset": None, "profiles": {
            role: {"model": profile.model, "reasoning_effort": profile.effort,
                   "reference": None, "provenance": {"source": "explicit-migration-input"}}
            for role, profile in profiles.items()}}
        inputs = CreationInputs(
            profile_document, frozen["prompt_resources"], frozen["github_identity"],
            frozen["runner_github_identity"], notifications,
            {"source": "v6-offline-conversion", "source_sha256": snapshot.sha256,
             "frozen_evidence_ids": frozen_ids,
             "profiles": "explicit-migration-input",
             "notifications": "old-not-configured"},
        )
        spec = RunSpec(run_id, repository, parent_number, parent_revision,
                       graph_revision,
                       (*tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "source-verified",
                                        Commit(base_sha, tree or ""), workspaces[number][0],
                                        f"refs/heads/{run_branch}", checkout=str(workspaces[number][1]),
                                        owned_root=str(workspaces[number][1].parent), common_git_dir=str(common))
                               for number in requirements.parent.sub_issue_numbers),
                        Delivery(f"final:{run_id}", DeliveryKind.FINAL_RUN, parent_number,
                                 parent_revision, Commit(base_sha, tree or ""),
                                 f"refs/heads/{run_branch}", f"refs/heads/{base_branch}",
                                 checkout=str(final_checkout), owned_root=str(final_checkout.parent),
                                 common_git_dir=str(common))),
                       replace(policy, development_profile=profiles["development"],
                               review_profile=profiles["review"], publication_profile=profiles["publication"]),
                       language, str(repository_root), inputs)
        run_key = SourceRecord("runs", (run_id,))
        graph = _one(db, "SELECT s.version_id FROM run_graph_snapshots s JOIN run_root r "
                     "ON r.run_id=s.run_id AND r.value_graph_snapshot_id=s.snapshot_id WHERE s.run_id=?", (run_id,))
        selected = _one(db, "SELECT * FROM work_job_slots WHERE run_id=?", (run_id,))
        subjects = []
        for number in requirements.parent.sub_issue_numbers:
            key = SourceRecord("run_graph_members", (run_id, graph["version_id"], number))
            aliases = () if number != ticket_number else (
                SourceRecord("work_jobs", (run_id, selected["job_id"])),
                SourceRecord("work_job_slots", (run_id, selected["container"], selected["slot"])),
                SourceRecord("work_job_layout", (run_id,)))
            resource = ImportedResource(owner=key, kind="ticket-workspace", disposition="new-unused",
                source_ref=workspaces[number][0], expected_oid=None, checkout=workspaces[number][1],
                workspace_oid=base_sha, owned_root=workspaces[number][1].parent, common_git_dir=common,
                before_head=Path(_git(workspaces[number][1], "rev-parse", "--path-format=absolute",
                                      "--git-path", "HEAD")).read_bytes(),
                checkout_archive_sha256=None, checkout_index_sha256=None,
                head_evidence=(), cleanup_pending=False)
            subjects.append(ImportedSubject(subject=f"ticket:{number}", issue=number, generation=1,
                record=key, aliases=aliases, kind="unallocated", historical_base=None, budgets=(), attempts=(),
                candidate=None, integration=None, accepted_review=None, completion=None, resources=(resource,)))
        history = tuple(ImportedHistoryEvent(
            record=SourceRecord("run_timeline_events", (run_id, event["event_id"])),
            subject=f"ticket:{ticket_number}", generation=1, kind=f"v6_{event['value_kind']}",
            identity=event["event_id"], created_at=_created_at_epoch(event["value_at"]),
            body=wire_json({"event_id": event["event_id"], "presence_hex": event["presence"].hex(),
                "values": {key: value for key, value in event.items()
                           if key.startswith("value_") and value is not None}})) for event in timeline)
        # Exact authenticated source text; the evidence ID is its identity,
        # never a material purpose or permission to consume an unknown fact.
        documents = tuple((row["evidence_id"], row["body"].encode("utf-8")) for row in db.execute(
            "SELECT e.evidence_id,m.body FROM work_evidence e JOIN work_evidence_materials m "
            "ON m.material_id=e.material_id WHERE e.run_id=? ORDER BY e.evidence_id", (run_id,)))
        source = ImportSource(snapshot=snapshot, mode="copied",
            scope="single-ticket" if len(subjects) == 1 else "selected-graph", task=None,
            checkout_uuid=_one(db, "SELECT value_checkout_identity FROM run_root WHERE run_id=?", (run_id,))[0],
            profile_document=None, external_readproof=None, oracle_documents=documents)
        run_resource = ImportedResource(owner=run_key, kind="run-ref", disposition="existing",
            source_ref="refs/heads/" + run_branch, expected_oid=base_sha, checkout=None, workspace_oid=None,
            owned_root=None, common_git_dir=common, before_head=None, checkout_archive_sha256=None,
            checkout_index_sha256=None, head_evidence=(SourceRecord("run_condition_base", (run_id,)),),
            cleanup_pending=False)
        return ImportedRunFacts(source=source, spec=spec, requirements=requirements,
            observed_requirements=requirements, created_at=created_at, subjects=tuple(subjects),
            run_resource=run_resource, history=history)

    decoded = _read_initial(snapshot.path, decode)
    validate_snapshot(snapshot)
    return decoded
