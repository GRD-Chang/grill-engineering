"""Current native cleanup: fresh source authority without unrelated Delivery I/O."""
from support.effect_boundary import effect_state
from pathlib import Path
from dataclasses import replace

import pytest

from agent_run.core.operation_contract import ReadUnavailable, RequirementsRead
from agent_run.errors import KernelError
from agent_run.model import ReadWait


class CleanupBoundary(Exception):
    pass


def pending_cleanup(s, monkeypatch, operation="cleanup_worktree"):
    """Stop at the committed cleanup intent before the tested observation."""
    make_ports, execute = s.make_ports, s.port.execute
    captured = []
    physical_ports = []

    def ports(binding):
        result = make_ports(binding)
        physical_ports[:] = [result]
        return result

    def boundary(effect, authority):
        if effect.operation == operation:
            captured.append((effect, authority))
            raise CleanupBoundary
        return execute(effect, authority)

    monkeypatch.setattr(s, "make_ports", ports)
    monkeypatch.setattr(s.port, "execute", boundary)
    with pytest.raises(CleanupBoundary):
        s.run()
    monkeypatch.setattr(s.port, "execute", execute)
    assert len(captured) == 1
    effect, authority = captured[0]
    return s, effect, authority, physical_ports[0], execute


def test_reopened_ticket_still_blocks_original_cleanup(graph_ticket_before_merge, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(graph_ticket_before_merge, monkeypatch)
    source = ports.source

    def changed(parent):
        result = source(parent)
        return replace(result, issues=tuple(replace(issue, state="OPEN") for issue in result.issues))

    monkeypatch.setattr(ports, "source", changed)
    with pytest.raises(KernelError):
        execute(effect, authority)
    assert s.kernel.source_status()["blocked"] == "unsupported_scope_change"
    assert effect_state(s, effect)[1] == 0
    assert Path(s.delivery.checkout).exists()


def test_remote_cleanup_still_requests_complete_delivery(graph_ticket_before_merge, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(graph_ticket_before_merge, monkeypatch, "cleanup_remote_ref")
    assert isinstance(s.kernel.begin_effect_observation(effect), RequirementsRead)

    def denied(request):
        raise ReadUnavailable("github_read_failed", "Required remote Delivery403")

    monkeypatch.setattr(ports, "delivery", denied)
    before = effect_state(s, effect)
    with pytest.raises(ReadWait):
        execute(effect, authority)
    assert effect_state(s, effect) == before
    assert s.port.remote_refs()[effect.source_ref] == effect.before_sha


def test_sibling_requirement_refresh_keeps_existing_gate_rules(tmp_path, monkeypatch):
    from support.offline import GraphScenario
    from support.requirements_scenarios import independent_source
    scenario = GraphScenario(tmp_path / "scenario", independent_source())
    s, effect, authority, ports, execute = pending_cleanup(scenario, monkeypatch)
    source = ports.source
    sibling = next(issue for issue in s.requirements.issues if f"ticket:{issue.number}" != effect.subject)
    with s.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts WHERE subject=?", (f"ticket:{sibling.number}",))

    def changed(parent):
        result = source(parent)
        return replace(result, issues=tuple(replace(issue, body=issue.body + "\nAdditional current requirement")
            if issue.number == sibling.number else issue for issue in result.issues))

    monkeypatch.setattr(ports, "source", changed)
    # Existing _refresh accepts a not-yet-started sibling's new requirement;
    # this projection must neither omit that sibling nor invent a new gate.
    assert execute(effect, authority).disposition == "target"
    assert s.kernel.source_status()["blocked"] is None
    with s.kernel.storage.transaction(write=False) as tx:
        revision = tx.one("SELECT revision FROM issue_facts WHERE issue=?", (sibling.number,))[0]
        assert tx.one("SELECT body FROM requirement_versions WHERE issue=? AND revision=?", (sibling.number, revision))[0].endswith("Additional current requirement")


def test_unreadable_worktree_registry_remains_unknown_without_authorization(git_repo, tmp_path, monkeypatch):
    from agent_run.adapters.cleanup import cleanup_worktree
    from agent_run.adapters.git_objects import LocalGit
    from support.cleanup_scenarios import _checkout

    git = LocalGit(git_repo)
    checkout, effect = _checkout(git, tmp_path)
    original, authorized = LocalGit.run, []

    def unreadable(reader, *arguments, **options):
        if arguments[:2] == ("worktree", "list"):
            raise KernelError("git_operation_failed", "Private registry read unavailable")
        return original(reader, *arguments, **options)

    git.authorize = lambda: authorized.append(effect.effect_id)
    monkeypatch.setattr(LocalGit, "run", unreadable)
    assert cleanup_worktree(git, effect).disposition == "unknown"
    assert checkout.exists() and authorized == []
