"""Only a proved obsolete source operation may yield to the Core runner."""
from agent_run.core.operation_runner import OperationRunner
from dataclasses import replace
from threading import Event, Thread, current_thread

import pytest

from agent_run.errors import GitHubReadError
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Step
from support.core_operations import CoreOperations
from support.remote_effects import _change, _fixture, _source_kernel


def source_operations(tmp_path, port, push, monkeypatch):
    # Freeze the exact local fixture remote at fresh creation. This neither
    # changes the shared fixture nor fills a missing progressed Run input.
    import support.remote_effects as fixtures
    create = fixtures.create_kernel
    def bound(path, spec, **kwargs):
        return create(path, replace(spec, inputs=replace(spec.inputs, repository_remote="origin")), **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "create_kernel", bound)
        kernel = _source_kernel(tmp_path, port, push)
    execution = kernel.claim_executor("native-reader")
    return CoreOperations(kernel, execution, lambda binding: port)


def test_executor_discards_a_source_read_superseded_during_real_external_io(tmp_path, monkeypatch):
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    operations = source_operations(tmp_path, port, push, monkeypatch)
    kernel, runner = operations.runner.kernel, operations.runner
    before = kernel.source_status()
    reached, release = Event(), Event()
    outcomes, failures = [], []
    source_observation = port.source_observation

    def delayed(parent, query):
        result = source_observation(parent, query)
        if current_thread() is thread:
            reached.set()
            assert release.wait(10), "bounded old read release"
        return result

    monkeypatch.setattr(port, "source_observation", delayed)

    def run():
        try:
            outcomes.append(operations.once())
        except Exception as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert reached.wait(10), "real source read reached the barrier"
        _change(state, source_body="New authoritative requirement")
        current_runner = OperationRunner(kernel, runner.execution, lambda binding: port)
        current_runner.observe("parent:1")
        current = kernel.source_status()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert failures == []
    assert len(outcomes) == 1 and outcomes[0].kind == "waiting"
    assert outcomes[0].reason == "automatic_step_superseded"
    assert isinstance(outcomes[0], Step)
    assert outcomes[0].call is None
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM effects')
    assert current["observed_parent_revision"] != before["observed_parent_revision"]
    assert kernel.source_status() == current
    monkeypatch.setattr(port, "source_observation", source_observation)
    assert runner.observe("parent:1").parent_revision == current["observed_parent_revision"]
    import json
    assert not any("--method" in json.loads(line) for line in calls.read_text().splitlines())


@pytest.mark.parametrize("code", ["source_observation_changed", "source_subject_changed", "upstream_unavailable"])
def test_executor_preserves_unproved_or_upstream_read_errors(tmp_path, monkeypatch, code):
    import agent_run.adapters.remote_observation as external

    port, _git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    operations = source_operations(tmp_path, port, push, monkeypatch)
    kernel = operations.runner.kernel
    error = GitHubReadError(code, "actual upstream failure") if code == "upstream_unavailable" else KernelError(code)

    def failed(_observation, _parent):
        raise error

    # Inject inside the actual observation boundary and its error adapter.
    monkeypatch.setattr(external._SourceObservation, "read", failed)
    if isinstance(error, GitHubReadError):
        step = operations.once()
        assert step.kind == "waiting" and step.reason == "github_convergence"
        assert isinstance(step, Step)
        assert step.call is None
        with kernel.storage.transaction(write=False) as tx:
            assert not tx.rows('SELECT id FROM effects')
        import json
        assert not _calls.exists() or not any(
            "--method" in json.loads(line) for line in _calls.read_text().splitlines())
        assert kernel.status("parent:1")["gates"] == []
        return
    with pytest.raises(type(error)) as raised:
        operations.once()
    assert raised.value is error
