"""Only a proved obsolete read may yield to the existing executor loop."""
from threading import Event, Thread

import pytest

from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run_kernel.executor import _Executor
from agent_run_kernel.model import KernelError
from agent_run_kernel.source_reading import read_requirements
from test_kernel_remote_effects import _change, _fixture, _source_kernel


def test_executor_discards_a_source_read_superseded_during_real_external_io(tmp_path, monkeypatch):
    import agent_run_kernel.remote_effects as external

    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch)
    kernel = _source_kernel(tmp_path, port, push)
    driver = _Executor(kernel, kernel.claim_executor("native-reader"), {})
    before = kernel.source_status()
    reached, release = Event(), Event()
    outcomes, failures = [], []

    def delayed(reader, parent):
        result = read_requirements(reader, parent)
        reached.set()
        assert release.wait(10), "bounded old read release"
        return result

    monkeypatch.setattr(external, "read_requirements", delayed)

    def advance():
        try:
            outcomes.append(driver.advance("parent:1"))
        except Exception as error:
            failures.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert reached.wait(10), "real source read reached the barrier"
        _change(state, source_body="New authoritative requirement")
        reader = GhGitHubReader(port.repository, working_directory=port.root, runner_identity={"source": "host"})
        current = kernel.refresh(lambda: read_requirements(reader, 1))
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert failures == []
    assert len(outcomes) == 1 and outcomes[0].kind == "waiting"
    assert outcomes[0].reason == "observation_superseded"
    assert outcomes[0].call is None and outcomes[0].effect is None
    assert current["observed_parent_revision"] != before["observed_parent_revision"]
    assert kernel.source_status() == current
    # Discarding yields once; a subsequent ordinary observation reads current facts.
    monkeypatch.setattr(external, "read_requirements", read_requirements)
    assert driver.observe("parent:1").parent_revision == current["observed_parent_revision"]
    import json
    assert not any("--method" in json.loads(line) for line in calls.read_text().splitlines())


@pytest.mark.parametrize("code", ["source_observation_changed", "source_subject_changed", "upstream_unavailable"])
def test_executor_preserves_unproved_or_upstream_read_errors(tmp_path, monkeypatch, code):
    import agent_run_kernel.remote_effects as external

    port, _git, push, _state, _calls = _fixture(tmp_path, monkeypatch)
    kernel = _source_kernel(tmp_path, port, push)
    driver = _Executor(kernel, kernel.claim_executor("native-reader"), {})
    error = GitHubReadError(code, "actual upstream failure") if code == "upstream_unavailable" else KernelError(code)

    def failed(_reader, _parent):
        raise error

    monkeypatch.setattr(external, "read_requirements", failed)
    if isinstance(error, GitHubReadError):
        step = driver.advance("parent:1")
        assert step.kind == "waiting" and step.reason == "github_convergence"
        assert step.call is None and step.effect is None
        assert kernel.status("parent:1")["gates"] == []
        return
    with pytest.raises(type(error)) as raised:
        driver.advance("parent:1")
    assert raised.value is error
