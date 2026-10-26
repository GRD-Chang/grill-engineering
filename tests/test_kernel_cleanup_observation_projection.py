"""Current native cleanup: fresh source authority without unrelated Delivery I/O."""
from pathlib import Path
from dataclasses import replace

import pytest

from agent_run.core.operation_contract import LocalCleanupRead, LocalCleanupObservation, ReadUnavailable, RequirementsRead
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.errors import KernelError
from agent_run.model import ReadWait
from agent_run.model import DeliveryKind
from support.cleanup_scenarios import _native_scenario


def test_local_cleanup_does_not_read_delivery_inside_write_authority(tmp_path, monkeypatch):
    s = _native_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    make_ports, execute = s.make_ports, s.port.execute
    active = [False]
    source_reads, calls, mutations = [], [], []
    from agent_run.adapters.git_objects import LocalGit
    original_run = LocalGit.run

    def run(git, *arguments, **options):
        if active[0] and (arguments[:2] == ("worktree", "remove")
                         or arguments[:3] == ("update-ref", "--no-deref", "-d")):
            mutations.append(arguments[:2])
        return original_run(git, *arguments, **options)

    monkeypatch.setattr(LocalGit, "run", run)

    def ports(binding):
        result = make_ports(binding)
        source, delivery = result.source, result.delivery

        def fresh_source(parent):
            if active[0]:
                source_reads.append(parent)
            return source(parent)

        def fresh_delivery(request):
            if active[0]:
                raise ReadUnavailable("github_read_failed", "Unrelated Delivery403")
            return delivery(request)

        result.source, result.delivery = fresh_source, fresh_delivery
        return result

    def physical(effect, authority):
        if effect.operation not in {"cleanup_worktree", "cleanup_local_ref"}:
            return execute(effect, authority)
        assert s.kernel.status(effect.subject)["phase"] == "complete"
        active[0] = True
        before = len(source_reads)
        try:
            result = execute(effect, authority)
        finally:
            active[0] = False
        calls.append((effect.operation, len(source_reads) - before, result.disposition))
        assert effect_state(s, effect)[:2] == ("sent", 1)
        return result

    monkeypatch.setattr(s, "make_ports", ports)
    monkeypatch.setattr(s.port, "execute", physical)
    assert s.run().kind == "complete"
    assert calls == [("cleanup_worktree", 3, "target"), ("cleanup_local_ref", 2, "target")]
    assert mutations == [("worktree", "remove"), ("update-ref", "--no-deref")]
    assert not Path(s.delivery.checkout).exists()
    assert s.kernel.status(s.delivery.key)["resource_cleanup"]["state"] == "complete"


class CleanupBoundary(Exception):
    pass


def pending_cleanup(tmp_path, monkeypatch, operation="cleanup_worktree", *, sibling=False):
    """Stop at a real committed intent; never seed or edit persisted state."""
    if sibling:
        from support.offline import GraphScenario
        from support.requirements_scenarios import independent_source
        s = GraphScenario(tmp_path / "scenario", independent_source())
    else:
        s = _native_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
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


def effect_state(s, effect):
    with s.kernel.storage.transaction(write=False) as tx:
        return tuple(tx.one("SELECT status,attempts,payload_id FROM effects WHERE id=?", (effect.effect_id,)))


@pytest.mark.parametrize("window", [1, 2, 3], ids=["before_lock", "after_lock", "before_command"])
@pytest.mark.parametrize("change", ["stop", "owner"])
def test_each_local_cleanup_guard_rejects_public_revocation(tmp_path, monkeypatch, window, change):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    prepare_count, writes = [], []
    from agent_run.adapters.git_objects import LocalGit
    import subprocess
    original = LocalGit.run
    original_spawn, spawned = subprocess.Popen, []

    def spawn(command, *arguments, **options):
        if isinstance(command, (tuple, list)) and any(
                list(command[index:index + 2]) == ["worktree", "remove"] for index in range(len(command) - 1)):
            spawned.append(command)
        return original_spawn(command, *arguments, **options)

    def run(git, *arguments, **options):
        if arguments[:2] == ("worktree", "remove"):
            # This is command preparation; the real before_start guard below
            # must reject before a process can remove the checkout.
            writes.append(arguments)
        return original(git, *arguments, **options)

    def prepare():
        final = authority.prepare()
        prepare_count.append(1)
        if len(prepare_count) == window:
            s.kernel.control("stop", "stop-at-cleanup")
            if change == "owner":
                s.kernel.confirm_executor_exit(s.execution)
                s.kernel.control("resume", "resume-after-exit")
                s.kernel.claim_executor("replacement-owner")
        return final

    monkeypatch.setattr(LocalGit, "run", run)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(KernelError):
        execute(effect, WriteAuthority(prepare))
    assert len(prepare_count) == window
    assert len(writes) == (1 if window == 3 else 0)
    assert spawned == []  # Preparing the command is not a physical removal.
    assert Path(s.delivery.checkout).exists()
    assert s.kernel.status(effect.subject)["phase"] == "complete"
    assert s.kernel.status(effect.subject)["resource_cleanup"]["stage"] == 0
    assert effect_state(s, effect)[1] == (0 if window == 1 else 1)


def test_requirements_403_keeps_original_intent_without_dispatch(tmp_path, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    before = effect_state(s, effect)

    def denied(parent):
        raise ReadUnavailable("github_read_failed", "Requirements403")

    monkeypatch.setattr(ports, "source", denied)
    with pytest.raises(ReadWait) as caught:
        execute(effect, authority)
    assert caught.value.step.kind == "waiting"
    assert effect_state(s, effect) == before
    assert Path(s.delivery.checkout).exists()


@pytest.mark.parametrize("window", ["source", "boundary"])
def test_cleanup_retains_original_absolute_read_deadline(tmp_path, monkeypatch, window):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    now = [100.25]
    s.kernel.clock = lambda: now[0]
    source = ports.source

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "original outage")

    monkeypatch.setattr(ports, "source", unavailable)
    with pytest.raises(ReadWait):
        authority.prepare()
    now[0] = 105.25

    def slow(parent):
        result = source(parent)
        if window == "source":
            now[0] = 701.25
        return result

    monkeypatch.setattr(ports, "source", slow)
    if window == "source":
        with pytest.raises(ReadWait) as caught:
            authority.prepare()
    else:
        final = authority.prepare()
        now[0] = 701.25
        with pytest.raises(ReadWait) as caught:
            final()
    assert caught.value.step.reason == "supervision_timeout"
    assert effect_state(s, effect)[1] == 0
    with s.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
    assert Path(s.delivery.checkout).exists()


@pytest.mark.parametrize("change", ["reopen", "graph"])
def test_latest_requirements_still_apply_original_cleanup_gates(tmp_path, monkeypatch, change):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    source = ports.source

    def changed(parent):
        result = source(parent)
        if change == "graph":
            return replace(result, parent=replace(result.parent, sub_issue_numbers=()))
        return replace(result, issues=tuple(replace(issue, state="OPEN") for issue in result.issues))

    monkeypatch.setattr(ports, "source", changed)
    with pytest.raises(KernelError):
        execute(effect, authority)
    assert s.kernel.source_status()["blocked"] == "unsupported_scope_change"
    assert effect_state(s, effect)[1] == 0
    assert Path(s.delivery.checkout).exists()


def test_projection_cannot_be_rebound_to_other_effects_or_observation_types(tmp_path, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    request = s.kernel.begin_effect_observation(effect)
    assert isinstance(request, LocalCleanupRead)
    source = ports.source(request.source.parent_number)
    observation = s.kernel.accept_local_cleanup_requirements(request, source)
    assert isinstance(observation, LocalCleanupObservation)
    before = effect_state(s, effect)
    for operation in ("cleanup_remote_ref", "workspace", "validation_cleanup", "validation_checkout",
                      "candidate", "integrate", "publication_commit", "merge", "abandon_worktree"):
        foreign = replace(effect, operation=operation)
        with pytest.raises(KernelError):
            s.kernel.begin_effect_observation(foreign)
        with pytest.raises(KernelError, match="local_cleanup_observation_binding"):
            s.kernel.accept_local_cleanup_requirements(LocalCleanupRead(foreign, request.source), source)
        with pytest.raises(KernelError, match="local_cleanup_observation_binding"):
            s.kernel.authorize_effect_observation(foreign, observation, observation.fence, prepared=True, inspections=None)
    for malformed in (request.source, LocalCleanupRead(effect, None),
                      LocalCleanupRead(effect, replace(request.source, fence=None))):
        with pytest.raises(KernelError, match="local_cleanup_observation_binding"):
            s.kernel.accept_local_cleanup_requirements(malformed, source)
    with pytest.raises(KernelError, match="local_cleanup_observation_binding"):
        s.kernel.authorize_effect_observation(effect, LocalCleanupObservation(effect, None), None, prepared=True, inspections=None)
    with pytest.raises(KernelError, match="effect_observation_binding"):
        s.kernel.authorize_effect_observation(effect, None, observation.fence, prepared=True, inspections=None)
    assert effect_state(s, effect) == before


def test_remote_cleanup_still_requests_complete_delivery(tmp_path, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch, "cleanup_remote_ref")
    assert isinstance(s.kernel.begin_effect_observation(effect), RequirementsRead)

    def denied(request):
        raise ReadUnavailable("github_read_failed", "Required remote Delivery403")

    monkeypatch.setattr(ports, "delivery", denied)
    before = effect_state(s, effect)
    with pytest.raises(ReadWait):
        execute(effect, authority)
    assert effect_state(s, effect) == before
    assert s.port.remote_refs()[effect.source_ref] == effect.before_sha


def test_lost_cleanup_response_reads_original_intent_without_repeating_write(tmp_path, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch)
    roles = list(s.role_calls)
    budgets = s.kernel.status(effect.subject)["budgets"]
    before = effect_state(s, effect)
    result = execute(effect, authority)
    assert result.disposition == "target" and not Path(s.delivery.checkout).exists()
    # The physical response is lost before Core receives it. Re-entry must use
    # the committed sent intent, not call execute or create a replacement.
    sent = effect_state(s, effect)
    assert sent == ("sent", 1, before[2])
    mutations = []
    original = s.port.execute

    def no_repeat(current, authorization):
        if current.effect_id == effect.effect_id:
            mutations.append(current.effect_id)
        return original(current, authorization)

    monkeypatch.setattr(s.port, "execute", no_repeat)
    assert s.run().kind == "complete"
    assert mutations == []
    assert effect_state(s, effect) == ("confirmed", 1, before[2])
    assert s.role_calls == roles and s.kernel.status(effect.subject)["budgets"] == budgets


def test_sibling_requirement_refresh_keeps_existing_gate_rules(tmp_path, monkeypatch):
    s, effect, authority, ports, execute = pending_cleanup(tmp_path, monkeypatch, sibling=True)
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
    from test_kernel_cleanup_adapter import _checkout

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
