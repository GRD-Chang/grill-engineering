"""Creation defaults become native facts; existing Threads never read new defaults."""

import json
from pathlib import Path

from agent_run.prompt_resources import RESOURCE_KEYS
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, RunSpec, World
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.offline import Scenario


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
    kernel = Kernel.create(path, spec)
    now = [100.0]
    kernel.clock = lambda: now[0]
    execution = kernel.claim_executor("owner")
    world = World("parent-v1", "graph-v1", "ticket-v1", base)
    first = kernel.advance(delivery.key, execution, world).call
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
    recovered = kernel.advance(delivery.key, execution, world).call
    assert recovered is not None and recovered.thread_id == first.thread_id and recovered.profile_revision == 1
    assert recovered.profile == first.profile and recovered.deadline_at == 109.0
    assert recovered.context["language"] == "en" and recovered.context["_prompt_resources"] == resources
    assert kernel.dispatch_agent(recovered)
    next_process = {**process, "pid": 22, "pgid": 22, "start_token": "two"}
    kernel.agent_started(recovered, next_process)
    kernel.agent_exited(recovered, next_process, 1)
    assert kernel.advance(delivery.key, execution, world).kind == "paused"
    kernel.control("resume", "new-thread", subject=delivery.key, new_thread=True)
    changed = kernel.advance(delivery.key, execution, world).call
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
    for _ in range(40):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.call is not None and step.call.role == "publication":
            call = step.call
            assert call.profile.model == "selected-development" and call.profile_revision == revision
            assert call.profile_reference == "development"
            assert call.context["language"] == "zh" and call.context["repository_root"] == str(scenario.port.git.root.resolve())
            break
        scenario.handle(step)
    else:
        raise AssertionError("Publication was not allocated")
    scenario.kernel.configure_profiles("separate-publication", overrides={"publication_model": "independent-publication"})
    # A later revision cannot mutate the already-authorized Publication call.
    assert scenario.kernel.dispatch_agent(call)
    scenario.kernel.guard_agent(call)


def test_publication_failure_observation_survives_successful_exact_readback(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    scenario.port.lose_response.add("pr_write")
    assert scenario.run().kind == "approval"
    assert scenario.port.pull_request(scenario.delivery.key) is not None
    with scenario.kernel.storage.transaction(write=False) as tx:
        failures = tx.rows("SELECT e.operation,e.subject,f.* FROM effect_failures f JOIN effects e ON e.id=f.effect")
        assert len(failures) == 1 and failures[0]["operation"] == "pr_write"
        assert failures[0]["dispatch_serial"] == 1
        evidence = json.loads(tx.get(failures[0]["evidence"], failures[0]["subject"]))
        assert evidence["type"] == "LostResponse"
        assert tx.one("SELECT sum(attempts) FROM effects")[0] > len(failures)
        assert tx.one("SELECT status FROM effects WHERE id=?", (failures[0]["effect"],))[0] == "confirmed"
