"""Actual source observation feeds the original first Run-ref empty CAS."""
from agent_run.core.operation_runner import observe_control
from pathlib import Path
from dataclasses import replace
import json

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import RunSpec
from agent_run.cli.lifecycle import creation_resources
from agent_run.adapters.source import read_requirements
from agent_run.adapters.github import GhGitHubReader
from test_kernel_remote_effects import _fixture, _source_kernel, _publish_pr
from support.core_operations import CoreOperations


def test_real_empty_run_ref_is_observed_and_created_before_a_ticket_workspace(tmp_path: Path, monkeypatch) -> None:
    port, git, seed, state, calls = _fixture(tmp_path, monkeypatch)
    document = json.loads(state.read_text())
    document["source_graph"] = {"parent": {"number": 1, "title": "Parent", "body": "Both changes", "sub_issues": [2]},
        "issues": {"2": {"number": 2, "title": "Ticket", "body": "Exact change", "state": "OPEN", "labels": ["ready-for-agent"]}}}
    state.write_text(json.dumps(document))
    reader = GhGitHubReader(port.repository, working_directory=port.root, runner_identity=port.identity)
    source = read_requirements(reader, 1)
    from agent_run.contracts.agent_profiles import resolve_profiles
    from agent_run.config.resources import resolve_resources
    from agent_run.model import CreationInputs
    inputs = CreationInputs(resolve_profiles(preset="economy"), resolve_resources("en"),
        {"source": "host"}, {"source": "host"}, {"enabled": False}, {"source": "explicit-test"})
    resources = creation_resources("new-run", source, seed.base, "refs/heads/main", port.root.resolve())
    spec = RunSpec("new-run", port.repository, 1, "not-authority", "not-authority", resources,
                   language="en", repository_root=str(port.root.resolve()), inputs=inputs)
    kernel = Kernel.create(tmp_path / "new-run/kernel.sqlite3", spec, requirements=source)
    execution = kernel.claim_executor("creator")
    operations = CoreOperations(kernel, execution, lambda binding: port)
    world = operations.runner.observe("ticket:2")
    assert world.target is None and world.source is None and world.pr is None and world.checks is None
    assert not Path(resources[0].checkout).exists()
    execute = port.execute
    sent, authorized = [], []

    def lost_response(effect, authorize):
        assert effect.operation == "run_branch"
        sent.append(effect)

        def original_authority():
            authorize()
            authorized.append(effect.effect_id)

        actual = execute(effect, original_authority)
        assert actual.disposition == "target" and actual.commit == seed.base
        raise OSError("Original Run-ref CAS completed but its reply was lost")

    monkeypatch.setattr(port, "execute", lost_response)
    interrupted = operations.once()
    assert interrupted.call is None and interrupted.reason == "original_write_readback_required"
    effect, = sent
    assert effect.source_ref == resources[-1].source_ref and effect.target_ref == "refs/heads/main"
    assert effect.payload["expected_remote_sha"] is None and effect.after_sha == seed.base.sha
    assert authorized == [effect.effect_id]
    assert git.run("ls-remote", "--refs", "origin", effect.source_ref).stdout.split()[0] == seed.base.sha
    reopened = Kernel(kernel.storage.path)
    original_read = port.reconcile
    readbacks = []

    def exact_readback(original):
        readbacks.append(original)
        return original_read(original)

    monkeypatch.setattr(port, "execute", lambda *args: pytest.fail("lost reply must settle without a second write"))
    monkeypatch.setattr(port, "reconcile", exact_readback)
    restarted = CoreOperations(reopened, execution, lambda binding: port)
    assert restarted.once().kind == "progress"
    assert readbacks == [effect]
    assert restarted.runner.observe("ticket:2").target == seed.base
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
    with pytest.raises(KernelError) as error:
        observe_control(kernel, "parent:1", lambda binding: port)
    assert error.value.code == "github_observation_pending"
    observations = [json.loads(line) for line in calls.read_text().splitlines()][before:]
    assert not any("--method" in call for call in observations)
