"""An unchanged original PR description still needs current write authority."""

from agent_run.contracts.write_authority import WriteAuthority
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.errors import KernelError
from test_kernel_remote_effects import _fixture, _publish_pr


@pytest.mark.parametrize("mode", ["current", "stopped", "changed_head", "changed_base", "foreign", "stale_base"])
def test_exact_original_pr_edit_noop_authorizes_and_rechecks_live_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    port, git, push, state, calls = _fixture(tmp_path, monkeypatch)
    original_effect, original = _publish_pr(port, push)
    revised = git.make_commit(original.head.tree, (original.head.sha,), "Repair the original PR head")
    git.run("push", "origin", revised.sha + ":" + push.source_ref)
    document = json.loads(state.read_text())
    document["prs"][0]["head"]["sha"] = revised.sha
    state.write_text(json.dumps(document))
    effect = replace(original_effect, effect_id="original-pr-unchanged-description", pr_number=original.number,
                     before_sha=revised.sha, after_sha=revised.sha, candidate=revised,
                     payload={"title": original.title, "body": original.body,
                              "prior": {"title": original.title, "body": original.body}})
    prepared: list[str] = []
    authorized: list[str] = []

    def move_base() -> None:
        later = git.make_commit(push.base.tree, (push.base.sha,), "Independent base append")
        git.run("push", "origin", later.sha + ":" + push.target_ref)
        current = json.loads(state.read_text())
        current["prs"][0]["base"]["sha"] = later.sha
        state.write_text(json.dumps(current))

    def authorize() -> None:
        authorized.append(mode)
        if mode == "stopped":
            raise KernelError("executor_fenced")

    def prepare():
        prepared.append(mode)
        if mode == "changed_base":
            move_base()
        elif mode == "changed_head":
            later = git.make_commit(revised.tree, (revised.sha,), "Independent source append")
            git.run("push", "origin", later.sha + ":" + push.source_ref)
            current = json.loads(state.read_text())
            current["prs"][0]["head"]["sha"] = later.sha
            state.write_text(json.dumps(current))
        elif mode == "foreign":
            current = json.loads(state.read_text())
            current["prs"][0]["user"]["login"] = "foreign-author"
            state.write_text(json.dumps(current))
        return authorize

    # Reconciliation is a pure observation, even before this effect dispatches.
    assert port.reconcile(effect).disposition == "target"
    if mode == "stale_base":
        move_base()
    if mode == "stopped":
        with pytest.raises(KernelError, match="executor_fenced"):
            port.execute(effect, WriteAuthority(prepare))
    else:
        result = port.execute(effect, WriteAuthority(prepare))
        assert result.disposition == ("target" if mode == "current" else "third")
        if mode == "current":
            assert result.pr is not None and result.pr.number == original.number and result.pr.head == revised
            assert port.reconcile(effect) == result
    assert authorized == ([mode] if mode in {"current", "stopped"} else [])
    assert prepared == ([] if mode == "stale_base" else [mode])
    observed_calls = [json.loads(line) for line in calls.read_text().splitlines()]
    writes = [call for call in observed_calls if "--method" in call]
    assert len(writes) == 1 and "POST" in writes[0] and writes[0][1].endswith("/pulls")
