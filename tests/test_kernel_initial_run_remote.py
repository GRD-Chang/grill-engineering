"""Actual source observation feeds the original first Run-ref empty CAS."""
from pathlib import Path
from dataclasses import replace
import json

import pytest

from agent_run.github import GitHubReadError
from agent_run_kernel import Kernel, RunSpec
from agent_run_kernel.lifecycle_cli import creation_resources
from agent_run_kernel.source_reading import read_requirements
from agent_run.github import GhGitHubReader
from tests.test_kernel_remote_effects import _fixture, _source_kernel, _publish_pr


def test_real_empty_run_ref_is_observed_and_created_before_a_ticket_workspace(tmp_path: Path, monkeypatch) -> None:
    port, git, seed, state, calls = _fixture(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document["source_graph"] = {"parent": {"number": 1, "title": "Parent", "body": "Both changes", "sub_issues": [2]},
        "issues": {"2": {"number": 2, "title": "Ticket", "body": "Exact change", "state": "OPEN", "labels": ["ready-for-agent"]}}}
    state.write_text(json.dumps(document))
    reader = GhGitHubReader(port.repository, working_directory=port.root, runner_identity=port.identity)
    source = read_requirements(reader, 1)
    from agent_run.agent_profiles import resolve_profiles
    from agent_run.prompt_resources import resolve_resources
    from agent_run_kernel.model import CreationInputs
    inputs = CreationInputs(resolve_profiles(preset="economy"), resolve_resources("en"),
        {"source": "host"}, {"source": "host"}, {"enabled": False}, {"source": "explicit-test"})
    resources = creation_resources("new-run", source, seed.base, "refs/heads/main", port.root.resolve())
    spec = RunSpec("new-run", port.repository, 1, "not-authority", "not-authority", resources,
                   language="en", repository_root=str(port.root.resolve()), inputs=inputs)
    kernel = Kernel.create(tmp_path / "new-run/kernel.sqlite3", spec, requirements=source)
    execution = kernel.claim_executor("creator")
    world = port.observe(kernel, "ticket:2")
    assert world.target is None and world.source is None and world.pr is None and world.checks is None
    assert not Path(resources[0].checkout).exists()
    step = kernel.advance("ticket:2", execution, world)
    assert step.effect is not None and step.effect.operation == "run_branch" and step.call is None
    effect = step.effect
    assert effect.source_ref == resources[-1].source_ref and effect.target_ref == "refs/heads/main"
    assert effect.payload["expected_remote_sha"] is None and effect.after_sha == seed.base.sha
    authorized = []

    def authorize() -> None:
        assert kernel.dispatch(effect, execution, port.observe(kernel, "ticket:2"))
        kernel.guard_effect(effect, execution, port.observe(kernel, "ticket:2"))
        authorized.append(effect.effect_id)

    actual = port.execute(effect, authorize)
    assert actual.disposition == "target" and actual.commit == seed.base
    assert authorized == [effect.effect_id]
    assert git.run("ls-remote", "--refs", "origin", effect.source_ref).stdout.split()[0] == seed.base.sha
    reopened = Kernel(kernel.storage.path)
    assert reopened.advance("ticket:2", execution).effect == effect
    readback = port.execute(effect, lambda: pytest.fail("lost reply must settle without a second write"))
    assert reopened.reconcile(effect, readback, execution).kind == "progress"
    assert port.observe(reopened, "ticket:2").target == seed.base
    assert not Path(resources[0].checkout).exists()
    with reopened.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 0
        assert tx.one("SELECT attempts FROM effects WHERE id=?", (effect.effect_id,))[0] == 1


def test_existing_pr_with_missing_remote_target_remains_an_inconsistent_observation(tmp_path: Path, monkeypatch) -> None:
    port, git, seed, _state, calls = _fixture(tmp_path, monkeypatch)
    # Delete the PR's Run target while the repository's default branch remains
    # valid. Missing default metadata is a separate earlier read failure.
    run_ref = "refs/heads/agent-run/existing/run"
    git.run("push", "origin", seed.base.sha + ":" + run_ref)
    seed = replace(seed, target_ref=run_ref)
    _publish_pr(port, seed)
    kernel = _source_kernel(tmp_path, port, seed)
    git.run("push", "origin", ":" + run_ref)
    before = len(calls.read_text().splitlines())
    with pytest.raises(GitHubReadError) as error:
        port.observe(kernel, "parent:1")
    assert error.value.code == "github_observation_pending"
    observations = [json.loads(line) for line in calls.read_text().splitlines()][before:]
    assert not any("--method" in call for call in observations)
