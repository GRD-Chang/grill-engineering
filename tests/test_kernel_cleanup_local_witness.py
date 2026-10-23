"""A detached workspace's local marker is independent of the published ref."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run_kernel import Effect
from support.offline import GraphScenario
from test_kernel_requirements import independent_source


@pytest.mark.parametrize("foreign_move", [False, True])
def test_generation_two_cleanup_uses_the_original_local_marker_and_published_remote_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_move: bool,
) -> None:
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
            pr = scenario.port.pull_request(subject, replacement.source_ref)
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
    assert scenario.run().kind == "complete"
    cleanup = kernel.status(subject)["resource_cleanup"]
    pr = scenario.port.pull_request(subject, replacement.source_ref)
    assert pr is not None and pr.merged is not None
    assert cleanup["source_sha"] == pr.head.sha
    assert scenario.port.git.commit(original.source_ref).sha == original_marker
    if foreign_move:
        assert cleanup["stage"] == 1 and cleanup["outcome"] == "third"
        assert scenario.port.git.commit(replacement.source_ref).sha == pr.head.sha
        assert scenario.port.remote_refs()[replacement.source_ref] == pr.head.sha
        assert [effect.operation for effect in cleanups] == ["cleanup_worktree", "cleanup_local_ref"]
    else:
        assert cleanup["state"] == "complete"
        assert scenario.port.ref_commit(replacement.source_ref) is None
        assert replacement.source_ref not in scenario.port.remote_refs()
        assert [effect.operation for effect in cleanups] == list(kernel.CLEANUP)
