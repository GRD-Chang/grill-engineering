"""Creation defaults become native facts; existing Threads never read new defaults."""


from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.contracts.prompt import RESOURCE_KEYS
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.core.operation_contract import ReadUnavailable
from support.offline import Scenario
from agent_run.queries.run import read
from support.core_operations import CoreOperations, native_agent_fixture, scripted_return
from support.remote_effects import _actual_publication_push


def test_explicit_frozen_profiles_keep_their_actual_source_without_a_preset(tmp_path: Path) -> None:
    defaults = UserDefaultsStore(tmp_path / "user-defaults.json")
    defaults.configure(language="en")
    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket-v1", base, "refs/heads/source", "refs/heads/target")
    spec = freeze_creation(RunSpec("run", "example/project", 1, "parent-v1", "graph-v1", (delivery,)), defaults)
    assert spec.inputs is not None
    profiles = {"preset": None, "profiles": {
        role: {"model": role + "-explicit-model", "reasoning_effort": "high", "reference": None,
               "provenance": {"source": "explicit-migration-input", "preset": None, "role": role}}
        for role in ("development", "review", "publication")
    }}
    kernel, ports = native_agent_fixture(tmp_path / "kernel.sqlite3", replace(spec, inputs=replace(spec.inputs, profiles=profiles)))
    execution = kernel.claim_executor("owner")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    call = operations.once().call
    assert call is not None and call.profile.model == "development-explicit-model"
    with kernel.storage.transaction(write=False) as tx:
        revision = tx.one("SELECT p.* FROM threads t JOIN profile_revisions p ON p.id=t.profile_revision WHERE t.id=?", (call.thread_id,))
        frozen = json.loads(tx.get(revision["material_id"], revision["subject"]))
    assert frozen["preset"] is None
    assert frozen["profiles"] == profiles["profiles"]


def test_creation_inputs_and_thread_revisions_survive_defaults_and_explicit_configure(tmp_path: Path) -> None:
    defaults = UserDefaultsStore(tmp_path / "settings" / "user-defaults.json")
    defaults.configure(language="en", profile={"preset": "economy", "development_model": "first-model", "development_effort": "high"},
                       policy={"integration_resync_limit": 0, "ticket_ci_development_limit": 2,
                               "ticket_ci_review": True, "invocation_deadlines": {"development": 7}},
                       github={"worker_source": "pat", "pat": {"env": "FIXED_WORKER_TOKEN"}, "runner_source": "host"},
                       notifications={"enabled": False, "mode": "detailed"})
    resources = {key: "Frozen prompt: " + key for key in RESOURCE_KEYS}
    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket-v1", base, "refs/heads/source", "refs/heads/target")
    spec = freeze_creation(RunSpec("run", "example/project", 1, "parent-v1", "graph-v1", (delivery,), repository_root=str(tmp_path)),
                           defaults, prompt_resources=resources)
    path = tmp_path / "kernel.sqlite3"
    kernel, ports = native_agent_fixture(path, spec)
    now = [100.0]
    kernel.clock = lambda: now[0]
    execution = kernel.claim_executor("owner")
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    first = operations.once().call
    assert first is not None and first.profile.model == "first-model" and first.profile_revision == 1
    assert first.context["language"] == "en" and first.context["_prompt_resources"] == resources
    assert first.context["github_identity"] == {"source": "pat", "pat": {"env": "FIXED_WORKER_TOKEN"}}
    assert first.deadline_at == 107.0 and first.deadline_seconds == 7.0
    assert kernel.status(delivery.key)["resync"]["limit"] == 0
    assert kernel.status(delivery.key)["budgets"]["ci"]["review_required"] is True
    defaults.configure(language="zh", profile={"development_model": "personal-later-model"})
    revision = kernel.configure_profiles("configure-once", overrides={"development_model": "explicit-later-model"})
    assert revision == kernel.configure_profiles("configure-once", overrides={"development_model": "explicit-later-model"}) == 2
    assert kernel.dispatch_agent(first)
    kernel.bind_thread(first, "backend-1")
    process = {"pid": 21, "pgid": 21, "start_token": "one", "host_pid": 10, "host_start_token": "host"}
    kernel.agent_started(first, process)
    kernel.observe_worker_activity(first, "turn-started", active=True, observed_at=100.1, evidence={"event": "turn.started"})
    kernel.observe_worker_activity(first, "turn-ended", active=False, observed_at=100.7, evidence={"event": "turn.failed"})
    diagnostic = {"event": "turn.failed", "codex_error_info": "execution_failure", "message": "Worker turn failed"}
    kernel.agent_exited(first, process, 1, diagnostic=diagnostic)
    now[0] = 102.0
    recovered = operations.once().call
    assert recovered is not None and recovered.thread_id == first.thread_id and recovered.profile_revision == 1
    assert recovered.profile == first.profile and recovered.deadline_at == 109.0
    assert recovered.context["language"] == "en" and recovered.context["_prompt_resources"] == resources
    assert kernel.dispatch_agent(recovered)
    next_process = {**process, "pid": 22, "pgid": 22, "start_token": "two"}
    kernel.agent_started(recovered, next_process)
    kernel.agent_exited(recovered, next_process, 1)
    assert operations.once().kind == "paused"
    kernel.control("resume", "new-thread", subject=delivery.key, new_thread=True)
    changed = operations.once().call
    assert changed is not None and changed.profile_revision == 2 and changed.thread_id != first.thread_id
    assert changed.profile.model == "explicit-later-model" and changed.context["language"] == "en"
    assert changed.attempt_id == first.attempt_id
    with kernel.storage.transaction(write=False) as tx:
        frozen = tx.one("SELECT * FROM run_inputs")
        inputs = json.loads(tx.get(frozen["material_id"], frozen["subject"]))
        assert inputs["notifications"]["mode"] == "detailed" and inputs["runner_identity"] == {"source": "host"}
        assert tx.one("SELECT action_id FROM invocations WHERE id=?", (changed.invocation_id,))[0] == "new-thread"
        assert tx.one("SELECT count(*) FROM profile_revisions")[0] == 2
        activity = tx.rows("SELECT * FROM worker_activity ORDER BY observed_at")
        assert [(row["active"], row["observed_at"]) for row in activity] == [(1, 100.1), (0, 100.7)]
        assert all(row["invocation"] == first.invocation_id for row in activity)
        failure = tx.one("SELECT start_evidence FROM invocation_outputs WHERE invocation=?", (first.invocation_id,))[0]
        assert json.loads(tx.get(failure, delivery.key))["diagnostic"] == diagnostic
        paused = tx.one("SELECT phase,purpose,status FROM history WHERE kind='paused' AND identity='execution_failed'")
        assert (paused["phase"], paused["purpose"]) == ("development", "requirements")
        assert json.loads(paused["status"])["gates"] == ["execution_failed"]


def test_publication_call_retains_its_actual_development_reference_revision(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    revision = scenario.kernel.configure_profiles("choose-profile", preset="economy", overrides={
        "development_model": "selected-development", "publication_from_development": True,
    })
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = None
    for _ in range(40):
        step = operations.once()
        if step.call is not None and step.call.role == "development":
            development = step.call
        if step.call is not None and step.call.role == "publication":
            call = step.call
            assert development is not None
            assert call.thread_id == development.thread_id and call.reported_thread == "offline-" + development.thread_id
            assert call.mode == "resume" and call.profile == development.profile
            assert call.profile.model == "selected-development" and call.profile_revision == revision
            assert call.profile_reference == development.profile_reference is None
            assert call.context["language"] == "zh" and call.context["repository_root"] == str(scenario.port.git.root.resolve())
            break
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
    else:
        raise AssertionError("Publication was not allocated")
    scenario.kernel.configure_profiles("separate-publication", overrides={"publication_model": "independent-publication"})
    # A later revision cannot mutate the already-authorized Publication call.
    assert scenario.kernel.dispatch_agent(call)
    scenario.kernel.guard_agent(call)


@pytest.mark.parametrize("command_exit", [0, 7], ids=["actual-zero", "actual-nonzero"])
def test_publication_failure_observation_survives_successful_exact_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_exit: int,
) -> None:
    scenario, effect = _actual_publication_push(tmp_path, monkeypatch, command_exit)
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM effects WHERE id=?", (effect.effect_id,))[0] == "observed"
        failures = tx.rows("SELECT e.operation,e.subject,f.* FROM effect_failures f JOIN effects e ON e.id=f.effect")
        assert len(failures) == (1 if command_exit else 0)
        if command_exit:
            assert failures[0]["operation"] == "push" and failures[0]["effect"] == effect.effect_id
            assert failures[0]["dispatch_serial"] == 1
            evidence = json.loads(tx.get(failures[0]["evidence"], failures[0]["subject"]))
            assert evidence["returncode"] == command_exit
            assert "actual transport command exited nonzero" in evidence["message"]
        assert tx.one("SELECT sum(attempts) FROM effects")[0] > len(failures)


def test_publication_unknown_response_retains_the_original_intent_without_inventing_an_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    scenario.port.lose_response.add("pr_write")
    make_ports = scenario.make_ports
    exceptions = []
    def capture_ports(binding):
        ports = make_ports(binding)
        execute = ports.execute
        def capture(effect, authorize):
            try:
                return execute(effect, authorize)
            except ReadUnavailable as error:
                exceptions.append((effect.effect_id, effect.dispatch_serial,
                                   getattr(error, "write_returncode", None)))
                raise
        ports.execute = capture
        return ports
    monkeypatch.setattr(scenario, "make_ports", capture_ports)
    unresolved = scenario.run()
    assert unresolved.kind == "waiting" and unresolved.reason == "github_convergence"
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending is not None and pending["operation"] == "pr_write" and pending["status"] == "sent"
    assert exceptions == [(pending["id"], 1, None)]
    diagnostic = {"code": "write_response_unavailable", "message": "pr_write took effect; response lost"}
    assert read(scenario.kernel.storage.path, "status").data["supervision_windows"][0]["latest_observation"] == diagnostic
    assert unresolved.retry_at is not None
    now[0] = unresolved.retry_at
    assert scenario.run().kind == "approval"
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref) is not None
    assert sum(row["operation"] == "pr_write" for row in scenario.port.writes()) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        failures = tx.rows("SELECT e.operation,e.subject,f.* FROM effect_failures f JOIN effects e ON e.id=f.effect")
        assert failures == []  # No physical command response proved a nonzero exit.
        assert tx.one("SELECT sum(attempts) FROM effects")[0] > len(failures)
        assert tx.one("SELECT attempts,status FROM effects WHERE id=?", (pending["id"],))[:] == (1, "confirmed")
        observations = [json.loads(row[0]) for row in tx.rows(
            "SELECT body FROM material WHERE subject=? AND purpose='github_read_observation'", (scenario.delivery.key,))]
        assert diagnostic in observations
