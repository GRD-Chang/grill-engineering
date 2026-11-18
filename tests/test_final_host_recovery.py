"""Final operation recovery over real Core/Git and synthetic Host witnesses."""
from dataclasses import replace
import time

import pytest

from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation


def _managed_approval(scenario, tmp_path):
    kernel = scenario.kernel
    kernel.control("stop", "retire-fixture")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("original-host", kind="run", command=("controlled-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    observation = HostObservation("running", request.unit_name, "original", "/host", "boot",
        42, "start", usage_ready=True)
    assert kernel.observe_host(request, observation) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    assert scenario.run().kind == "approval"
    assert kernel.control("approve", "original-approval", subject=scenario.delivery.key,
                          world=scenario.world()) == "pending"
    return kernel, request, observation


def _replacement(scenario, tmp_path, observation):
    kernel = scenario.kernel
    request = kernel.admit_host("replacement-host", kind="run", command=("controlled-host",),
        environment_carrier=tmp_path / "replacement-carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, replace(observation, unit_name=request.unit_name,
        invocation_id="replacement", main_pid=43, start_token="replacement-start")) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)


@pytest.mark.parametrize("operation", ["merge", "close"])
def test_explicit_resume_reconciles_original_final_action_and_lost_external_receipt(
    parent_at_approval, tmp_path, operation,
):
    scenario = parent_at_approval
    kernel, host, observation = _managed_approval(scenario, tmp_path)
    before = kernel.status(scenario.delivery.key)
    with kernel.storage.transaction(write=False) as tx:
        original_attempts = [tuple(row) for row in tx.all("SELECT id,result_id,thread_id FROM attempts ORDER BY rowid")]
        grant = tx.one("SELECT grant_id FROM subjects WHERE id=?", (scenario.delivery.key,))[0]
    scenario.port.lose_response.add(operation)
    assert scenario.run().kind == "waiting"
    pending = kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == operation and pending["status"] == "sent"
    assert sum(write["operation"] == operation for write in scenario.port.writes()) == 1
    ended = replace(observation, disposition="exited", exit_status=137,
                    ended_at=time.time(), usage_released=True)
    assert kernel.observe_host(host, ended) == "exited"
    assert kernel.reconcile_final_host_exit() == "original-approval"
    assert kernel.reconcile_final_host_exit() is None
    assert kernel.resume_subject() == scenario.delivery.key
    assert kernel.status(scenario.delivery.key)["pending_write"] == pending
    assert kernel.control("resume", "resume-original-approval", subject=scenario.delivery.key) == "pending"
    assert kernel.control("resume", "resume-original-approval", subject=scenario.delivery.key) == "pending"
    _replacement(scenario, tmp_path, observation)
    assert scenario.run().kind == "complete"
    after = kernel.status(scenario.delivery.key)
    assert after["budgets"] == before["budgets"]
    assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "close" for write in scenario.port.writes()) == 1
    with kernel.storage.transaction(write=False) as tx:
        assert [tuple(row) for row in tx.all("SELECT id,result_id,thread_id FROM attempts ORDER BY rowid")] == original_attempts
        assert tx.one("SELECT count(*) FROM grants")[0] == 1
        assert tx.one("SELECT grant_id FROM subjects WHERE id=?", (scenario.delivery.key,))[0] == grant
        assert tx.one("SELECT status FROM actions WHERE id='original-approval'")[0] == "failed"
        assert tx.one("SELECT status FROM actions WHERE id='resume-original-approval'")[0] == "complete"
    for command in ("approve", "resume"):
        assert kernel.completed_final_action(command) == {
            "action_id": "resume-original-approval", "action": "resume", "subject": scenario.delivery.key,
            "status": "action_complete",
        }


@pytest.mark.parametrize("proof", ["running", "unknown", "foreign", "unreleased"])
def test_final_action_cannot_be_reconciled_without_complete_original_host_release(
    parent_at_approval, tmp_path, proof,
):
    kernel, host, observation = _managed_approval(parent_at_approval, tmp_path)
    if proof != "running":
        ended = replace(observation, disposition="unknown" if proof == "unknown" else "exited",
            invocation_id="foreign" if proof == "foreign" else observation.invocation_id,
            exit_status=137, ended_at=time.time(), usage_released=proof != "unreleased")
        kernel.observe_host(host, ended)
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.reconcile_final_host_exit()
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT final_action FROM runs")[0] == "original-approval"
        assert tx.one("SELECT status FROM actions WHERE id='original-approval'")[0] == "pending"
        assert tx.one("SELECT count(*) FROM grants")[0] == 1
    assert kernel.completed_final_action("approve") is None


@pytest.mark.parametrize("prepare_cleanup", [False, True])
def test_completed_final_readback_requires_necessary_resource_cleanup(graph_final_at_approval, tmp_path, prepare_cleanup):
    scenario = graph_final_at_approval
    kernel, host, observation = _managed_approval(scenario, tmp_path)
    for _ in range(30):
        scenario.step()
        if kernel.status(scenario.delivery.key)["phase"] == "complete":
            break
    else:
        pytest.fail("Final operation did not close its Issue")
    generation = kernel.status(scenario.delivery.key)["generation"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT state FROM resource_cleanup WHERE subject=? AND generation=?",
                      (scenario.delivery.key, generation))[0] != "complete"
        assert tx.one("SELECT status FROM actions WHERE id='original-approval'")[0] == "pending"
    assert kernel.completed_final_action("approve") is None
    assert kernel.resume_subject() == scenario.delivery.key  # No Gate/Effect is needed to identify retained cleanup.
    with pytest.raises(KernelError, match='final_approval_noncancellable'):
        kernel.stop_task('must-not-stop-before-first-cleanup')
    if prepare_cleanup:
        scenario.step()
    else:
        # close consumed, but no cleanup Effect has even been prepared yet.
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT effect_id FROM resource_cleanup WHERE subject=? AND generation=?",
                          (scenario.delivery.key, generation))[0] is None
            assert not tx.rows("SELECT code FROM gates WHERE subject=?", (scenario.delivery.key,))
    ended = replace(observation, disposition="exited", exit_status=137,
                    ended_at=time.time(), usage_released=True)
    assert kernel.observe_host(host, ended) == "exited"
    assert kernel.reconcile_final_host_exit() == 'original-approval'
    assert kernel.resume_subject() == scenario.delivery.key
    assert kernel.control("resume", "resume-final-cleanup", subject=scenario.delivery.key) == "pending"
    _replacement(scenario, tmp_path, observation)
    assert scenario.run().kind == "complete"
    assert kernel.completed_final_action("approve") == {
        "action_id": "resume-final-cleanup", "action": "resume", "subject": scenario.delivery.key,
        "status": "action_complete",
    }
    assert sum(write["operation"] == "merge" and write["subject"] == scenario.delivery.key
               for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "close" and write["subject"] == scenario.delivery.key
               for write in scenario.port.writes()) == 1


def test_final_cleanup_resumes_each_effect_free_stage_without_repeating_delivery(graph_final_at_approval, tmp_path):
    scenario = graph_final_at_approval
    with scenario.kernel.storage.transaction(write=False) as tx:
        sibling = tx.one("SELECT id FROM subjects WHERE kind='ticket' ORDER BY issue LIMIT 1")[0]
    assert scenario.kernel.control("stop", "preserve-sibling-boundary", subject=sibling) == "applied"
    kernel, host, observation = _managed_approval(scenario, tmp_path)
    for _ in range(30):
        scenario.step()
        if kernel.status(scenario.delivery.key)["phase"] == "complete":
            break
    else:
        pytest.fail("Final operation did not close its Issue")
    before = kernel.status(scenario.delivery.key)
    with kernel.storage.transaction(write=False) as tx:
        original_attempts = [tuple(row) for row in tx.all("SELECT id,result_id,thread_id FROM attempts ORDER BY rowid")]
        grant = tx.one("SELECT grant_id FROM subjects WHERE id=?", (scenario.delivery.key,))[0]
        sibling = tx.one("SELECT id FROM subjects WHERE kind='ticket'")[0]
    for stage in range(len(kernel.CLEANUP)):
        # Each prior Effect has been consumed; interruption is before the next
        # Effect is allocated. No failure Gate is manufactured for this gap.
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT stage,state,effect_id FROM resource_cleanup WHERE subject=? AND generation=?",
                         (scenario.delivery.key, before["generation"]))
            assert tuple(row) == (stage, "pending", None)
            assert not tx.rows("SELECT code FROM gates WHERE subject=?", (scenario.delivery.key,))
        if stage == 0:
            for proof in ("running", "unknown", "unreleased"):
                if proof == "unknown":
                    kernel.observe_host(host, HostObservation("unknown", host.unit_name))
                elif proof == "unreleased":
                    kernel.observe_host(host, replace(observation, disposition="exited", exit_status=0,
                        ended_at=time.time(), usage_released=False))
                with pytest.raises(KernelError, match="executor_exit_unproven"):
                    kernel.reconcile_final_host_exit()
                with pytest.raises(KernelError, match="final_approval_noncancellable"):
                    kernel.control("resume", f"must-not-take-over-{proof}", subject=scenario.delivery.key)
            # The exact stored exit time must remain unchanged when its usage
            # release arrives after the initial process-exit observation.
            with kernel.storage.transaction(write=False) as tx:
                ended_at = tx.one("SELECT ended_at FROM hosts WHERE action_id='original-host'")[0]
        else:
            ended_at = time.time()
        assert kernel.observe_host(host, replace(observation, disposition="exited", exit_status=0,
            ended_at=ended_at, usage_released=True)) == "exited"
        kernel.reconcile_final_host_exit()
        assert kernel.resume_subject() == scenario.delivery.key
        action = f"cleanup-stage-{stage}"
        host = kernel.admit_host(action, kind="resume", subject=scenario.delivery.key,
            command=("controlled-host",), environment_carrier=tmp_path / f"carrier-{stage}", preflight=lambda: None)
        assert kernel.dispatch_host(host)
        observation = replace(observation, unit_name=host.unit_name, invocation_id=action,
                              main_pid=43 + stage, start_token=action)
        assert kernel.observe_host(host, observation) == "running"
        with pytest.raises(KernelError, match='final_approval_noncancellable'):
            kernel.stop_task(f'must-not-stop-cleanup-{stage}')
        scenario.execution = Execution(host.run_id, host.owner, host.epoch)
        for _ in range(12):
            scenario.step()
            with kernel.storage.transaction(write=False) as tx:
                cleanup = tx.one("SELECT stage,effect_id FROM resource_cleanup WHERE subject=? AND generation=?",
                                 (scenario.delivery.key, before["generation"]))
                if tuple(cleanup) == (stage + 1, None):
                    break
        else:
            pytest.fail(f"Cleanup stage {stage} did not settle")
    assert scenario.run().kind == "complete"
    assert kernel.status(scenario.delivery.key)["budgets"] == before["budgets"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT grant_id FROM subjects WHERE id=?", (scenario.delivery.key,))[0] == grant
        assert [tuple(row) for row in tx.all("SELECT id,result_id,thread_id FROM attempts ORDER BY rowid")] == original_attempts
        assert tx.one("SELECT count(*) FROM grants")[0] == 1
        assert tx.one("SELECT code FROM gates WHERE subject=?", (sibling,))[0] == "operator_stopped"
    for operation in ("merge", "close"):
        assert sum(write["operation"] == operation and write["subject"] == scenario.delivery.key
                   for write in scenario.port.writes()) == 1


def test_retained_completed_action_without_gate_can_resume_pending_cleanup(graph_final_at_approval, tmp_path):
    scenario = graph_final_at_approval
    kernel, host, observation = _managed_approval(scenario, tmp_path)
    for _ in range(30):
        scenario.step()
        if kernel.status(scenario.delivery.key)['phase'] == 'complete':
            break
    # Model the retained same-protocol boundary written before Action completion
    # was extended through cleanup. Use the sole control writer; keep the real
    # approved grant, consumed close and all cleanup facts unchanged.
    with kernel.storage.transaction() as tx:
        run = tx.one('SELECT * FROM runs')
        kernel._finish_final_success(tx, run, scenario.delivery.key)
        assert tx.one('SELECT final_action FROM runs')[0] is None
        assert tx.one('SELECT effect_id FROM resource_cleanup WHERE subject=?', (scenario.delivery.key,))[0] is None
    assert kernel.observe_host(host, replace(observation, disposition='exited', exit_status=0,
        ended_at=time.time(), usage_released=True)) == 'exited'
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT code FROM gates WHERE subject=?', (scenario.delivery.key,))
    assert kernel.resume_subject() == scenario.delivery.key
    assert kernel.control('resume', 'resume-retained-cleanup', subject=scenario.delivery.key) == 'pending'
    _replacement(scenario, tmp_path, observation)
    assert scenario.run().kind == 'complete'
    assert kernel.completed_final_action('resume')['action_id'] == 'resume-retained-cleanup'


def test_final_cleanup_failure_releases_action_and_resume_reobserves_same_intent(
    graph_final_at_approval, tmp_path,
):
    from pathlib import Path
    scenario = graph_final_at_approval
    kernel, host, observation = _managed_approval(scenario, tmp_path)
    for _ in range(30):
        scenario.step()
        if kernel.status(scenario.delivery.key)['phase'] == 'complete':
            break
    uncommitted = Path(kernel.delivery(scenario.delivery.key).checkout) / 'retained-uncommitted.txt'
    uncommitted.write_text('A real local edit must prevent deletion.\n')
    assert scenario.run(cut='cleanup').kind in {'paused', 'waiting'}
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT final_action FROM runs')[0] is None
        assert tx.one("SELECT status FROM actions WHERE id='original-approval'")[0] == 'failed'
        assert tx.one('SELECT outcome FROM resource_cleanup WHERE subject=?', (scenario.delivery.key,))[0] == 'third'
    assert uncommitted.read_text() == 'A real local edit must prevent deletion.\n'
    uncommitted.unlink()  # Restore only this test's injected local change.
    assert kernel.observe_host(host, replace(observation, disposition='exited', exit_status=0,
        ended_at=time.time(), usage_released=True)) == 'exited'
    assert kernel.control('resume', 'retry-refused-cleanup', subject=scenario.delivery.key) == 'pending'
    _replacement(scenario, tmp_path, observation)
    assert scenario.run().kind == 'complete'
    for operation in ('merge', 'close'):
        assert sum(write['operation'] == operation and write['subject'] == scenario.delivery.key
                   for write in scenario.port.writes()) == 1
