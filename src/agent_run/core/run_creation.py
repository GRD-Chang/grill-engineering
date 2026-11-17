"""Freeze a complete Run on one transaction, independently of runtime adapters."""
from dataclasses import asdict, replace
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from agent_run.contracts.agent_profiles import resolve_profiles, validate_profile_options
from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.contracts.prompt import validate_resources
from agent_run.core.budget_policy import budget_limits, requires_review
from agent_run.core.source_snapshot import fingerprint, source_revisions, record_source
from agent_run.errors import KernelError
from agent_run.model import Delivery, DeliveryKind, Policy, Purpose, Requirements, Role, RunSpec, wire_json
from agent_run.storage.run_scope import RunTransaction, run_statement, transaction_scope
from agent_run.storage.sqlite import Transaction


def encode_policy(policy: Policy) -> dict[str, Any]:
    values = asdict(policy)
    for role in Role:
        del values[role.value + "_profile"]
    return values


def creation_profiles(spec: RunSpec) -> dict[str, Any]:
    if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
        raise ValueError("one or more uniquely identified deliveries are required")
    if spec.inputs is None:
        raise KernelError("creation_inputs_required")
    profiles = dict(spec.inputs.profiles)
    validate_profile_options(preset=spec.inputs.profiles.get("preset"))
    checked = resolve_profiles(current=spec.inputs.profiles)
    if any(field not in spec.inputs.profiles["profiles"][role.value]
           or spec.inputs.profiles["profiles"][role.value][field] != checked["profiles"][role.value][field]
           for role in Role for field in ("model", "reasoning_effort", "reference")):
        raise KernelError("creation_profile_binding_changed")
    if spec.language is None:
        raise KernelError("creation_language_required")
    validate_resources(dict(spec.inputs.prompt_resources))
    validate_identity_snapshot(spec.inputs.worker_identity)
    validate_identity_snapshot(spec.inputs.runner_identity)
    return profiles


def create_run(tx: Transaction, spec: RunSpec, profiles: Mapping[str, Any], *,
                requirements: Requirements,
                policy_values: Mapping[str, Any] | None = None, clock: Callable[[], float] | None = None) -> None:
    """Freeze one complete Run on the caller's original writer; no physical I/O."""
    clock = time.time if clock is None else clock
    policy = encode_policy(spec.policy) if policy_values is None else policy_values
    if requirements.parent.number != spec.parent_issue:
        raise KernelError("parent_identity")
    parent_revision, graph_revision = source_revisions(requirements)
    spec = replace(spec, parent_revision=parent_revision, graph_revision=graph_revision)
    tickets = {delivery.issue for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET}
    if tickets != set(requirements.parent.sub_issue_numbers):
        raise KernelError("delivery_scope")
    if len([delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN]) != bool(tickets):
        raise KernelError("final_target_required")
    if tickets:
        final = next(delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN)
        if any(delivery.target_ref != final.source_ref or delivery.base != final.base
               for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET):
            raise KernelError("run_branch_identity")
    tx.execute("INSERT INTO runs(id,repository,parent_issue,parent_revision,graph_revision,accepted_parent_revision,concurrency,policy,language,repository_root,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (spec.run_id, spec.repository, spec.parent_issue, spec.parent_revision,
                spec.graph_revision, spec.parent_revision, spec.policy.concurrency, wire_json(policy),
                spec.language, None if spec.repository_root is None else str(Path(spec.repository_root).resolve()), clock()))
    tx = RunTransaction(tx, spec.run_id)
    for delivery in spec.deliveries:
        if delivery.kind == DeliveryKind.FINAL_RUN:
            tx.execute(*run_statement(tx, "INSERT INTO final_target(id,source_ref,target_ref,checkout,owned_root,common_git_dir,run_id) VALUES (?,?,?,?,?,?,?)",
                (delivery.key, delivery.source_ref, delivery.target_ref, delivery.checkout, delivery.owned_root, delivery.common_git_dir), run_last=True))
            continue
        issue = next((issue for issue in requirements.issues if issue.number == delivery.issue), None)
        revision = spec.parent_revision if delivery.kind == DeliveryKind.PARENT_ONLY else (
            "unread" if issue is None else fingerprint({"title": issue.title, "body": issue.body}))
        delivery = replace(delivery, revision=revision, dependencies=())
        add_delivery(tx, spec.run_id, spec.policy, delivery)
        if delivery.owned_root:
            tx.execute(*run_statement(tx, "UPDATE subjects SET phase='workspace' WHERE id=? AND run_id=?", (delivery.key,), run_last=True))
    owner = tx.one(*run_statement(tx, "SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1", (), run_last=True))[0]
    material = tx.put(owner, "profile_revision", wire_json({**profiles, "source": {"operation": "create"}}))
    tx.execute(*run_statement(tx, 'INSERT INTO profile_revisions(id,subject,material_id,created_at,run_id) VALUES (1,?,?,?,?)',
        (owner, material, clock()), run_last=True))
    if spec.inputs is not None:
        values: dict[str, Any] = {name: dict(getattr(spec.inputs, name)) for name in (
            "prompt_resources", "worker_identity", "runner_identity", "notifications", "provenance")}
        values["repository_remote"] = spec.inputs.repository_remote
        material = tx.put(owner, "creation_inputs", wire_json(values))
        tx.execute("INSERT INTO run_inputs VALUES (?,?,?)", (spec.run_id, owner, material))
    observed = record_source(tx, requirements, initial=True)
    assert not isinstance(observed, bool)
    tx.execute(*run_statement(tx, 'UPDATE runs SET blocked=? WHERE id=?', (observed.graph_problem,), run_last=True))
    job = tx.one(*run_statement(tx, 'SELECT id,generation,phase,purpose FROM subjects WHERE run_id=? ORDER BY rowid LIMIT 1', (), run_last=True))
    status = wire_json({"stopped": False, "blocked": observed.graph_problem, "gates": [], "attempt_status": None}).decode()
    tx.execute(*run_statement(tx, 'INSERT INTO history(subject,kind,identity,generation,created_at,phase,purpose,status,run_id) VALUES (?,?,?,?,?,?,?,?,?)',
        (job["id"], "source_refreshed", str(observed.sequence), job["generation"], clock(), job["phase"], job["purpose"], status), run_last=True))


def add_delivery(tx: Transaction, run_id: str, policy: Policy, delivery: Delivery) -> None:
    selected = transaction_scope(tx)
    if run_id != selected.run_id:
        raise KernelError("allocation_binding")
    final = delivery.kind == DeliveryKind.FINAL_RUN
    tx.execute("""INSERT INTO subjects(
        id,run_id,kind,issue,revision,phase,purpose,source_ref,target_ref,
        base_sha,base_tree,source_sha,candidate_sha,candidate_tree,completion_revision,checkout,workspace_sha,owned_root,common_git_dir)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (delivery.key, run_id, delivery.kind.value, delivery.issue, delivery.revision,
                "integration" if final else "development", Purpose.INTEGRATION.value if final else Purpose.REQUIREMENTS.value,
                delivery.source_ref, delivery.target_ref, delivery.base.sha, delivery.base.tree,
                delivery.base.sha, delivery.base.sha if final else None, delivery.base.tree if final else None,
                delivery.completion_revision, delivery.checkout, delivery.base.sha, delivery.owned_root, delivery.common_git_dir))
    limits = budget_limits(policy, delivery.kind)
    for purpose, (development_limit, review_limit) in limits.items():
        review_required = requires_review(policy, delivery.kind, purpose)
        tx.execute(*run_statement(tx, 'INSERT INTO budgets(subject,purpose,window,development_limit,review_limit,review_required,run_id) VALUES (?,?,1,?,?,?,?)',
            (delivery.key, purpose.value, development_limit, review_limit, review_required), run_last=True))
    tx.execute(*run_statement(tx, 'INSERT INTO resync_allowances(subject,generation,allowance,run_id) VALUES (?,1,?,?)',
        (delivery.key, policy.resync_limit), run_last=True))
