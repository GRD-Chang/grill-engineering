"""Finite cleanup fixtures; test callers retain execution and resource oracles."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from agent_run.model import DeliveryKind, Effect
from support.core_operations import final_scenario
from support.offline import GraphScenario, Scenario
from support.requirements_scenarios import independent_source


def _native_scenario(root: Path, kind: DeliveryKind) -> GraphScenario:
    if kind == DeliveryKind.FINAL_RUN:
        return final_scenario(root)
    source = independent_source()
    if kind == DeliveryKind.PARENT_ONLY:
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=()), issues=())
    else:
        # Isolate one delivery's cleanup. A global runner may otherwise start
        # the independent sibling when the completed subject's cleanup blocks.
        first = source.issues[0]
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=(first.number,)), issues=(first,))
    return GraphScenario(root, source)


def _local_head(scenario: Scenario, ref: str) -> str | None:
    output = scenario.port.git.run("for-each-ref", "--format=%(refname)%09%(objectname)", ref).stdout
    return next((line.partition("\t")[2] for line in output.splitlines()
                 if line.partition("\t")[0] == ref), None)


def prepared_generation_two_cleanup(tmp_path: Path, monkeypatch, foreign_move: bool):
    scenario = GraphScenario(tmp_path / "scenario", independent_source())
    kernel, subject = scenario.kernel, scenario.delivery.key
    for _ in range(20):
        if scenario.step().call is not None:
            break
    else:
        raise AssertionError("Original generation never ran Development")
    original = kernel.delivery(subject)
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body + "\nClarified requirement") if issue.number == original.issue else issue
        for issue in scenario.requirements.issues))
    world = scenario.world()
    replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                          checkout=str(Path(original.checkout).with_name("ticket-g2")))
    assert kernel.requeue(subject, "new-generation", replacement, world) == "applied"
    assert kernel.status(subject)["generation"] == 2
    original_marker = scenario.port.git.commit(original.source_ref).sha
    actual_execute = scenario.port.execute
    cleanups: list[Effect] = []
    marker: list[str] = []

    def execute(effect: Effect, authorize=None):
        if effect.operation in kernel.CLEANUP:
            cleanups.append(effect)
            with kernel.storage.transaction(write=False) as tx:
                workspace = tx.one("SELECT * FROM effects WHERE subject=? AND generation=2 AND operation='workspace' AND status='confirmed'", (subject,))
                intent = json.loads(tx.get(workspace["payload_id"], subject))
            assert intent["create_ref"] is True and workspace["source_ref"] == replacement.source_ref
            marker[:] = [workspace["before_sha"]]
            pr = scenario.port.pull_request_by_ref(replacement.source_ref, replacement.target_ref)
            assert pr is not None and pr.head.sha != marker[0]
            if effect.operation == "cleanup_worktree":
                # The native workspace creates this marker once and checks out
                # detached; publishing a SHA to the remote cannot move it.
                assert scenario.port.git.commit(replacement.source_ref).sha == marker[0]
            elif effect.operation == "cleanup_local_ref":
                assert effect.before_sha == effect.payload["expected_head"] == marker[0]
                if foreign_move:
                    scenario.port.git.run("update-ref", replacement.source_ref, pr.head.sha, marker[0])
            else:
                assert effect.before_sha == effect.payload["expected_head"] == pr.head.sha
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    return scenario, original, replacement, original_marker, cleanups
