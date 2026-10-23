"""The original Core Action retires its PR through actual GitHub transport I/O."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.model import Policy
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from agent_run.adapters.remote import RemoteEffects
from test_kernel_remote_effects import _fixture
from test_kernel_requirements import independent_source


def test_original_requeue_action_closes_once_after_real_remote_ack_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path / "scenario", independent_source(), policy=Policy(concurrency=2))
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-offline")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "prepare-managed")
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    for _ in range(100):
        scenario.step()
        if kernel.status(subject)["phase"] == "checks":
            break
    else:
        raise AssertionError("The original delivery never published its PR")
    original = kernel.delivery(subject)
    prior = scenario.port.pull_request_by_ref(original.source_ref, original.target_ref)
    assert prior is not None and prior.state == "OPEN" and prior.actor is not None
    scenario.requirements = replace(scenario.requirements, issues=tuple(
        replace(issue, body=issue.body + "\nClarified requirement") if issue.number == original.issue else issue
        for issue in scenario.requirements.issues))
    world = scenario.world()
    replacement = replace(original, source_ref=original.source_ref + "-g2", base=world.target,
                          checkout=str(Path(original.checkout).with_name("replacement-g2")))
    assert kernel.control_host(request, "requeue", "replace-original", subject=subject,
                               world=world, replacement=replacement) == "pending"
    transport_root = tmp_path / "transport"
    transport_root.mkdir()
    _fixture_port, _git, _push, state, calls = _fixture(transport_root, monkeypatch,
        repository=prior.repository, actor=prior.actor)
    bare = scenario.port.physical.remote
    monkeypatch.setenv("REMOTE_TEST_BARE", str(bare))
    raw_pr = {"number": prior.number, "state": "open", "title": prior.title, "body": prior.body,
              "user": {"login": prior.actor}, "merged": False, "merge_commit_sha": None,
              "head": {"sha": prior.head.sha, "ref": prior.source_ref.removeprefix("refs/heads/"),
                       "repo": {"full_name": prior.repository}},
              "base": {"sha": prior.base.sha, "ref": prior.target_ref.removeprefix("refs/heads/"),
                       "repo": {"full_name": prior.repository}}}
    state.write_text(json.dumps({"prs": [raw_pr], "comments": [], "events": [],
                                 "issue_state": "open", "lose_operation": "pr"}))
    remote = RemoteEffects(scenario.port.git.root, prior.repository,
                           runner_identity={"source": "host"}, remote=str(bare))

    make_ports = scenario.make_ports
    effects, results = [], []
    authorizations: list[int] = []

    def transport_ports(binding):
        ports = make_ports(binding)
        delivery, execute = ports.delivery, ports.execute

        def read(request):
            facts = delivery(request)
            return replace(facts, pr=remote.pull_request(prior.number))

        def write(effect, authorize):
            if effect.operation != "retire_pr":
                return execute(effect, authorize)
            effects.append(effect)
            assert effect.payload["retirement_intent"]["control_action"] == effect.payload["control_action"]

            def guarded():
                authorize()
                observed_calls = [json.loads(line) for line in calls.read_text().splitlines()]
                authorizations.append(sum("--method" in call for call in observed_calls))

            result = remote.execute(effect, guarded)
            results.append(result)
            return result

        ports.delivery, ports.execute = read, write
        ports.prepare_retirement = remote.prepare_retirement
        return ports

    monkeypatch.setattr(scenario, "make_ports", transport_ports)
    runner = scenario.runner()
    assert runner.run_once().kind == "progress"
    assert len(effects) == len(results) == 1
    effect, result = effects[0], results[0]
    control_action = effect.payload["control_action"]
    assert control_action == "replace-original"
    assert result.disposition == "target" and result.pr is not None and result.pr.state == "CLOSED"
    assert result.raw["write_returncode"] == 1
    assert authorizations == [0, 1]
    assert kernel.status(subject)["generation"] == 1
    assert runner.run_once().kind == "progress"
    assert kernel.status(subject)["generation"] == 2
    again = remote.execute(effect, lambda: pytest.fail("The original closed receipt must remain read-only"))
    assert again.disposition == "target" and again.pr == result.pr
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    writes = [call for call in actual if "--method" in call]
    assert len(writes) == 2
    journal = json.loads(state.read_text())
    assert len(journal["comments"]) == len(journal["events"]) == 1
    assert journal["comments"][0]["body"] == effect.payload["retirement_intent"]["comment_body"]
    assert journal["events"][0]["event"] == "closed"
