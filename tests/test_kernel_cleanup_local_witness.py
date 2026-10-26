"""A detached workspace's local marker is independent of the published ref."""
from pathlib import Path

import pytest

from support.cleanup_scenarios import prepared_generation_two_cleanup


@pytest.mark.parametrize("foreign_move", [False, True])
def test_generation_two_cleanup_uses_the_original_local_marker_and_published_remote_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_move: bool,
) -> None:
    scenario, original, replacement, original_marker, cleanups = prepared_generation_two_cleanup(tmp_path, monkeypatch, foreign_move)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert scenario.run().kind == "complete"
    cleanup = kernel.status(subject)["resource_cleanup"]
    pr = scenario.port.pull_request_by_ref(replacement.source_ref, replacement.target_ref)
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
