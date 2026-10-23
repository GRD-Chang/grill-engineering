"""Publication keeps its actual backend context and immutable execution binding."""
from pathlib import Path

import pytest

from support.core_operations import CoreOperations, scripted_return, final_scenario
from agent_run_kernel import AgentCall, DeliveryKind, Role
from support.offline import Scenario
from agent_run_kernel.role_prompt import render_role_prompt
from test_kernel_codex_transport import local_codex


def next_call(scenario: Scenario, role: Role) -> AgentCall:
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    for _ in range(160):
        step = operations.once()
        if step.call is not None and step.call.role == role:
            return step.call
        assert step.kind not in {"paused", "waiting", "complete", "approval"}, step
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
    raise AssertionError("The requested role never became ready")


def finish_call(scenario: Scenario, call: AgentCall) -> None:
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    scenario.kernel.receive(call, scenario.agent(call))


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_publication_reuses_development_binding_after_future_profiles_change(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = Scenario(tmp_path / "scenario", kind)
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    scenario.kernel.configure_profiles("future-development", overrides={"development_model": "future-development-model"})
    reviewer = next_call(scenario, Role.REVIEW)
    assert reviewer.thread_id != development.thread_id and reviewer.reported_thread is None and reviewer.mode == "fresh"
    finish_call(scenario, reviewer)
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.thread_id == development.thread_id
    assert publication.reported_thread == "offline-" + development.thread_id
    assert publication.mode == "resume" and publication.role == Role.PUBLICATION
    assert publication.profile == development.profile and publication.profile_revision == development.profile_revision == 1
    assert publication.context["role"] == "publication" and publication.context["readonly"] is True
    assert scenario.review_count == 1
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["requirements"]["developments_used"] == 1
    if kind == DeliveryKind.TICKET:
        # This crosses the real process adapter and validates the actual argv,
        # thread.started binding and final wire file without a remote service.
        raw = b'{"result_kind":"publication","commit_message":"feat: delivered","pr_title":"Delivered","pr_body_markdown":"Validated delivery.","human_blockers":null}'
        result = local_codex(scenario.kernel, publication, tmp_path, f"""\
assert sys.argv[1:4] == ['exec', 'resume', '--json']
assert sys.argv[5] == {publication.reported_thread!r}
assert sys.argv[sys.argv.index('--model') + 1] == {development.profile.model!r}
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert 'commit_message' in schema['properties'] and 'summary' not in schema['properties']
print(json.dumps({{'type': 'thread.started', 'thread_id': {publication.reported_thread!r}}}), flush=True)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({raw!r})
""")
        assert result is not None and result.returncode == 0 and result.raw == raw
        assert operations.once().kind == "progress"
        assert scenario.kernel.status(publication.subject)["phase"] == "publication_commit"
    assert render_role_prompt(publication)


def test_independent_publication_is_fresh_even_when_its_model_matches_development(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    scenario.kernel.configure_profiles("independent-publication", overrides={"publication_model": development.profile.model})
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.thread_id != development.thread_id
    assert publication.reported_thread is None and publication.mode == "fresh"
    assert publication.profile.model == development.profile.model and publication.profile_revision == 2


def test_existing_independent_publication_thread_precedes_a_new_development_reference(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("fail", "none"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    scenario.kernel.configure_profiles("independent-publication", overrides={"publication_model": "original-publication-model"})
    first = next_call(scenario, Role.PUBLICATION)
    assert first.mode == "fresh" and first.profile_revision == 2
    finish_call(scenario, first)
    scenario.kernel.configure_profiles("future-economy", preset="economy")
    second = next_call(scenario, Role.PUBLICATION)
    assert second.thread_id == first.thread_id and second.reported_thread == "offline-" + first.thread_id
    assert second.mode == "resume" and second.profile_revision == 2 and second.profile == first.profile
    assert second.profile.model == "original-publication-model"


def test_explicit_new_thread_keeps_the_publication_attempt_and_uses_the_new_profile(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    first = next_call(scenario, Role.PUBLICATION)
    scenario.kernel.receive(first, b'{"result_kind":"human_blocker","commit_message":null,"pr_title":null,"pr_body_markdown":null,"human_blockers":["Need release wording"]}')
    assert operations.once().reason == "human_blocker"
    scenario.kernel.configure_profiles("next-publication", overrides={"publication_model": "replacement-publication-model"})
    before = scenario.kernel.status(first.subject)["budgets"]
    scenario.kernel.control("resume", "replace-publication-thread", subject=first.subject, new_thread=True)
    resumed = next_call(scenario, Role.PUBLICATION)
    assert resumed.attempt_id == first.attempt_id and resumed.invocation_id != first.invocation_id
    assert resumed.thread_id != first.thread_id and resumed.mode == "fresh" and resumed.reported_thread is None
    assert resumed.profile_revision == 2 and resumed.profile.model == "replacement-publication-model"
    assert scenario.kernel.status(first.subject)["budgets"] == before


def test_explicit_new_publication_thread_before_allocation_is_bound_once(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("fail", "none"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    for _ in range(80):
        if scenario.kernel.status(development.subject)["phase"] == "publish":
            break
        scenario.step()
    else:
        raise AssertionError("Publication never became ready")
    assert scenario.kernel.status(development.subject)["pending_attempt"] is None
    scenario.kernel.control("stop", "pause-before-publication", subject=development.subject)
    scenario.kernel.control("resume", "new-publication-context", subject=development.subject, new_thread=True,
                            message="Use a fresh publication context")
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.thread_id != development.thread_id and publication.mode == "fresh"
    assert publication.reported_thread is None
    scenario.kernel.control("resume", "new-publication-context", subject=development.subject, new_thread=True,
                            message="Use a fresh publication context")
    assert len([event for event in scenario.kernel.history(development.subject) if event["kind"] == "human_response"]) == 1
    finish_call(scenario, publication)
    subsequent_development = next_call(scenario, Role.DEVELOPMENT)
    assert subsequent_development.thread_id == development.thread_id
    assert subsequent_development.reported_thread == "offline-" + development.thread_id
    assert subsequent_development.mode == "resume" and subsequent_development.profile == development.profile


def test_development_context_fallback_stops_at_the_fourth_publication_attempt(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("fail", "fail", "fail", "none"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    for ordinal in range(1, 5):
        publication = next_call(scenario, Role.PUBLICATION)
        if ordinal < 4:
            assert publication.thread_id == development.thread_id
            assert publication.mode == "resume" and publication.reported_thread == "offline-" + development.thread_id
        else:
            assert publication.thread_id != development.thread_id
            assert publication.mode == "fresh" and publication.reported_thread is None
        finish_call(scenario, publication)


def test_final_publication_uses_a_new_thread_after_integration_development(tmp_path: Path) -> None:
    scenario = final_scenario(tmp_path / "scenario", reviews=("fail", "pass"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.thread_id != development.thread_id
    assert publication.mode == "fresh" and publication.reported_thread is None
