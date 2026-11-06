"""Publication keeps its actual backend context and immutable execution binding."""
from pathlib import Path

from support.core_operations import CoreOperations, final_scenario
from agent_run.model import AgentCall, DeliveryKind, Role, wire_json
from support.offline import Scenario, review_result
from agent_run.core.prompts.role import render_role_prompt
from support.native_runtime import local_codex
from support.core_operations import next_call, prepare_scripted_agent
from support.worker_facts import worker_fixture, start_worker, finish_worker, consume_local_result


def finish_call(scenario: Scenario, call: AgentCall) -> None:
    scenario.kernel.receive(call, scenario.agent(call))


def test_publication_reuses_development_binding_after_future_profiles_change(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    scenario.kernel.configure_profiles("future-development", overrides={"development_model": "future-development-model"})
    reviewer = next_call(scenario, Role.REVIEW)
    assert reviewer.thread_id != development.thread_id and reviewer.reported_thread is None and reviewer.mode == "fresh"
    finish_call(scenario, reviewer)
    # Capture owns preparation -> credentials -> admission.
    native = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    publication = native.until(lambda step: step.preparation is not None).preparation
    assert publication is not None and publication.role == Role.PUBLICATION
    assert publication.thread_id == development.thread_id
    assert publication.reported_thread == "offline-" + development.thread_id
    assert publication.mode == "resume" and publication.role == Role.PUBLICATION
    assert publication.profile == development.profile and publication.profile_revision == development.profile_revision == 1
    assert publication.profile_reference == development.profile_reference is None
    # Later configuration must not change this already allocated Publication.
    scenario.kernel.configure_profiles("separate-publication", overrides={"publication_model": "independent-publication"})
    assert publication.context["role"] == "publication" and publication.context["readonly"] is True
    assert scenario.review_count == 1
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["requirements"]["developments_used"] == 1
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


def publication_fixture(root):
    """Explicit preceding returns/receipts; Core alone makes Publication ready."""
    kernel, operations = worker_fixture(root)
    development = prepare_scripted_agent(kernel, operations.once()).call
    process = start_worker(kernel, operations, development)
    kernel.bind_thread(development, "original-development-backend")
    finish_worker(kernel, development, process)
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, development.subject, "candidate")
    consume_local_result(kernel, operations, development.subject, "validation_checkout")
    review = prepare_scripted_agent(kernel, operations.once()).call
    process = start_worker(kernel, operations, review)
    finish_worker(kernel, review, process, wire_json(review_result("pass")))
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, review.subject, "validation_cleanup")
    consume_local_result(kernel, operations, review.subject, "integrate")
    assert operations.once().kind == "progress"
    assert kernel.status(development.subject)["phase"] == "publish"
    operations.prepare_agents = True
    return kernel, operations, development


def test_independent_publication_is_fresh_even_when_its_model_matches_development(tmp_path: Path) -> None:
    kernel, operations, development = publication_fixture(tmp_path / "consumer")
    kernel.configure_profiles("independent-publication", overrides={"publication_model": development.profile.model})
    publication = operations.once().call
    assert publication.thread_id != development.thread_id
    assert publication.reported_thread is None and publication.mode == "fresh"
    assert publication.profile.model == development.profile.model and publication.profile_revision == 2


def test_existing_independent_publication_thread_precedes_a_new_development_reference(tmp_path: Path) -> None:
    kernel, operations, _ = publication_fixture(tmp_path / "consumer")
    kernel.configure_profiles("independent-publication", overrides={"publication_model": "original-publication-model"})
    first = operations.once().call
    assert first.mode == "fresh" and first.profile_revision == 2
    kernel.bind_thread(first, "original-publication-backend")
    kernel.configure_profiles("future-economy", preset="economy")
    # Prior publication relation is the consumer input. CI retry production is
    # covered separately; this selector must retain the original binding.
    with kernel.storage.transaction(write=False) as tx:
        run, job = kernel._owned(tx, first.subject, operations.runner.execution)
        assert kernel._publication_thread(tx, run, job) == first.thread_id
        thread = tx.one("SELECT * FROM threads WHERE id=?", (first.thread_id,))
        profile, _ = kernel._thread_profile(tx, thread)
        assert thread["profile_revision"] == 2 and profile == first.profile
        assert profile.model == "original-publication-model"


def test_explicit_new_thread_keeps_the_publication_attempt_and_uses_the_new_profile(tmp_path: Path) -> None:
    kernel, operations, _ = publication_fixture(tmp_path / "consumer")
    first = operations.once().call
    kernel.receive(first, b'{"result_kind":"human_blocker","commit_message":null,"pr_title":null,"pr_body_markdown":null,"human_blockers":["Need release wording"]}')
    assert operations.once().reason == "human_blocker"
    kernel.configure_profiles("next-publication", overrides={"publication_model": "replacement-publication-model"})
    before = kernel.status(first.subject)["budgets"]
    kernel.control("resume", "replace-publication-thread", subject=first.subject, new_thread=True)
    resumed = operations.once().call
    assert resumed.attempt_id == first.attempt_id and resumed.invocation_id != first.invocation_id
    assert resumed.thread_id != first.thread_id and resumed.mode == "fresh" and resumed.reported_thread is None
    assert resumed.profile_revision == 2 and resumed.profile.model == "replacement-publication-model"
    assert kernel.status(first.subject)["budgets"] == before


def test_explicit_new_publication_thread_before_allocation_is_bound_once(tmp_path: Path) -> None:
    kernel, operations, development = publication_fixture(tmp_path / "consumer")
    assert kernel.status(development.subject)["pending_attempt"] is None
    kernel.control("stop", "pause-before-publication", subject=development.subject)
    kernel.control("resume", "new-publication-context", subject=development.subject, new_thread=True,
                   message="Use a fresh publication context")
    publication = operations.once().call
    assert publication.thread_id != development.thread_id and publication.mode == "fresh"
    assert publication.reported_thread is None
    kernel.control("resume", "new-publication-context", subject=development.subject, new_thread=True,
                   message="Use a fresh publication context")
    assert len([event for event in kernel.history(development.subject) if event["kind"] == "human_response"]) == 1
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT development_thread FROM subjects WHERE id=?", (development.subject,))[0] == development.thread_id


def test_development_context_fallback_stops_at_the_fourth_publication_attempt(monkeypatch) -> None:
    from support.repository_scope import insert
    from support.run_scoped_kernel import effect_runs

    # Original role-count relations are selector inputs, not synthetic CI
    # transitions. A real native allocation/resume is retained above.
    with effect_runs(monkeypatch) as (db, kernels):
        kernel = kernels["A"]
        insert(db, "threads", {"run_id": "A", "id": "development", "subject": "parent:1",
            "role": "development", "profile_revision": 1, "backend_id": "original-development"})
        db.execute("UPDATE subjects SET development_thread='development' WHERE run_id='A'")
        for ordinal in range(1, 5):
            with kernel.storage.transaction(write=False) as tx:
                run = tx.one("SELECT * FROM runs WHERE id='A'")
                job = tx.one("SELECT * FROM subjects WHERE run_id='A'")
                assert kernel._publication_thread(tx, run, job) == ("development" if ordinal < 4 else None)
            insert(db, "attempts", {"run_id": "A", "id": str(ordinal), "subject": "parent:1", "generation": 1,
                "ordinal": ordinal, "role": "publication", "purpose": "requirements", "window": 1,
                "parent_revision": "r", "graph_revision": "g", "revision": "r", "base_sha": "a" * 40,
                "completion_revision": "no-tickets", "context_id": "profile", "status": "consumed", "thread_id": "development"})


def test_final_publication_uses_a_new_thread_after_integration_development(tmp_path: Path) -> None:
    scenario = final_scenario(tmp_path / "scenario", reviews=("fail", "pass"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    development = next_call(scenario, Role.DEVELOPMENT)
    finish_call(scenario, development)
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.thread_id != development.thread_id
    assert publication.mode == "fresh" and publication.reported_thread is None
