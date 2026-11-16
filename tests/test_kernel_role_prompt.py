"""Frozen request rendering reaches a real CLI without rereading personal input."""

from support.repository_fixture import create_kernel

import json
import os
from pathlib import Path
import sys

import pytest

from dataclasses import replace

from agent_run.config.resources import builtin_resources, personal_method_directory
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import AgentPreparation, Step, Commit, Delivery, DeliveryKind, Role, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.contracts.artifact import role_schema
from agent_run.runtime.codex import execute_codex
from agent_run.config.creation import freeze_creation
from support.offline import Scenario, review_result
from agent_run.core.prompts.role import render_role_prompt
from support.core_operations import CoreOperations, native_agent_fixture, observed_call, prepare_scripted_agent, scripted_return


def frozen_call(tmp_path: Path) -> tuple[Kernel, AgentPreparation, CoreOperations, UserDefaultsStore]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket-v1", base,
                        "refs/heads/source", "refs/heads/target", checkout=str(checkout))
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"},
                       github={"worker_source": "pat", "pat": {"env": "SELECTED_WORKER_TOKEN"}, "runner_source": "host"})
    resources = builtin_resources("en")
    resources["methods/development"] = "Frozen development method\n" + resources["methods/development"]
    resources["methods/repair"] = "Frozen repair method\n" + resources["methods/repair"]
    resources["methods/acceptance"] = "Unrelated acceptance method must not leak\n" + resources["methods/acceptance"]
    spec = freeze_creation(RunSpec("prompt-run", "example/repository", 1, "parent-v1", "graph-v1", (delivery,)),
                           defaults, prompt_resources=resources)
    kernel, ports = native_agent_fixture(tmp_path / "state" / "kernel.sqlite3", spec)
    operations = CoreOperations(kernel, kernel.claim_executor("prompt-owner"), lambda binding: ports, prepare_agents=False)
    call = operations.once().preparation
    assert call is not None
    return kernel, call, operations, defaults


def test_frozen_development_body_reaches_cli_and_repair_selects_only_its_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "personal"))
    kernel, call, operations, defaults = frozen_call(tmp_path)
    defaults.path.write_text("{malformed current settings must not be read")
    personal = personal_method_directory("en")
    personal.mkdir(parents=True)
    (personal / "development.md").write_bytes(b"\xff")
    prompt = render_role_prompt(call)
    assert "Frozen development method" in prompt
    assert "Unrelated acceptance method must not leak" not in prompt
    assert "https://github.com/example/repository/issues/1" in prompt
    assert "https://github.com/example/repository/issues/2" in prompt
    assert "_prompt_resources" not in prompt and "SELECTED_WORKER_TOKEN" not in prompt
    executable = tmp_path / "local-codex"
    executable.write_text(f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
assert sys.stdin.read() == {prompt!r}
assert json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text()) == {role_schema(call.role.value)!r}
print(json.dumps({{'type': 'thread.started', 'thread_id': 'prompt-thread'}}), flush=True)
""")
    executable.chmod(0o700)
    credential = tmp_path / "hosts.yml"
    credential.write_text("github.com:\n  oauth_token: fixture-only\n")
    credential.chmod(0o600)
    result = execute_codex(kernel, call, executable=str(executable), prompt=prompt,
                           environment={"PATH": os.defpath, "HOME": str(tmp_path)}, credential_file=credential,
                           hidden_paths=(), authorize_source=lambda admitted: None, timeout=5)
    assert result is not None and result.raw == b"" and result.returncode == 0
    repair = operations.once().preparation
    assert repair is not None and repair.mode == "output-repair"
    repaired_prompt = render_role_prompt(repair)
    assert repair.context["contract_error"] in repaired_prompt
    assert "Frozen development method" not in repaired_prompt
    assert "Unrelated acceptance method must not leak" not in repaired_prompt
    assert credential.read_bytes()  # The renderer test explicitly owns this synthetic readiness.
    actual_repair = prepare_scripted_agent(kernel, Step("preparing", repair.subject, preparation=repair)).call
    assert actual_repair is not None
    assert actual_repair.invocation_id == observed_call(kernel, call).invocation_id
    assert actual_repair.deadline_at == observed_call(kernel, call).deadline_at
    assert call.context["_prompt_resources"] == repair.context["_prompt_resources"]


def test_call_without_frozen_resources_is_rejected_before_dispatch(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket", base, "source", "target")
    kernel, ports = native_agent_fixture(tmp_path / "kernel.sqlite3", RunSpec("run", "example/repository", 1, "parent", "graph", (delivery,)))
    call = CoreOperations(kernel, kernel.claim_executor("owner"), lambda binding: ports, prepare_agents=False).once().preparation
    # Missing resources is a renderer input contract, not authorization to run
    # an unconfigured Core. Exercise it on the immutable call before dispatch.
    assert call is not None
    call = replace(call, context={key: value for key, value in call.context.items() if key != "_prompt_resources"})
    assert call is not None
    with pytest.raises(KernelError, match="prompt_resources_unavailable"):
        render_role_prompt(call)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT invocation FROM invocation_outputs")
        assert tx.one("SELECT count(*) FROM waits WHERE kind='agent_preparation'")[0] == 1


def run_prompt_call(kernel: Kernel, call: AgentPreparation, tmp_path: Path, raw: bytes, *, edit: bool = False) -> str:
    prompt = render_role_prompt(call)
    executable = tmp_path / "prompt-codex"
    executable.write_text(f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
assert sys.stdin.read() == {prompt!r}
assert json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text()) == {role_schema(call.role.value)!r}
if {call.reported_thread!r} is not None:
    assert sys.argv[1:4] == ['exec', 'resume', '--json']
    assert sys.argv[5] == {call.reported_thread!r}
print(json.dumps({{'type': 'thread.started', 'thread_id': {call.reported_thread or 'cli-' + call.thread_id!r}}}), flush=True)
if {edit!r}:
    Path('implementation.txt').write_text({call.attempt_id!r})
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_bytes({raw!r})
""")
    executable.chmod(0o700)
    credential = tmp_path / "hosts.yml"
    credential.write_text("github.com:\n  oauth_token: fixture-only\n")
    credential.chmod(0o600)
    result = execute_codex(kernel, call, executable=str(executable), prompt=prompt,
                           environment={"PATH": os.defpath, "HOME": str(tmp_path)}, credential_file=credential,
                           hidden_paths=(), authorize_source=lambda admitted: None, timeout=5)
    assert result is not None and result.returncode == 0 and result.raw == raw
    return prompt


def test_same_thread_new_development_attempt_keeps_complete_repair_method(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"},
                       github={"worker_source": "pat", "pat": {"env": "SELECTED_WORKER_TOKEN"}, "runner_source": "host"})
    resources = builtin_resources("en")
    resources["methods/repair"] = "Frozen full repair method\n" + resources["methods/repair"]
    spec = freeze_creation(scenario.spec, defaults, prompt_resources=resources)
    scenario.kernel = create_kernel(tmp_path / "state" / "kernel.sqlite3", spec, requirements=scenario.requirements)
    scenario.execution = scenario.kernel.claim_executor("prompt-owner")
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    first = None
    reviewed = False
    for _ in range(30):
        step = operations.once()
        if step.preparation is None:
            assert step.kind in {"effect", "progress"}, step
            continue
        call = step.preparation
        if call.role == Role.REVIEW:
            assert first is not None and not reviewed
            prompt = run_prompt_call(scenario.kernel, call, tmp_path, json.dumps(review_result("fail")).encode())
            context = call.context["review_context"]
            assert context["review_range"]["candidate_sha"] in prompt
            reviewed = True
        elif call.role == Role.DEVELOPMENT:
            raw = b'{"result_kind":"development","summary":"Implemented the allocated task.","human_blockers":null}'
            prompt = run_prompt_call(scenario.kernel, call, tmp_path, raw, edit=True)
            if first is None:
                first = call
                assert call.context["semantic_continuation"] is False
            else:
                assert reviewed and call.attempt_id != first.attempt_id
                assert call.thread_id == first.thread_id and call.mode == "resume"
                assert call.context["semantic_continuation"] is False
                assert "Frozen full repair method" in prompt
                assert call.context["repair_source"] == "acceptance"
                assert json.dumps(call.context["acceptance_artifact"], ensure_ascii=False, indent=2, sort_keys=True) in prompt
                return
        else:
            raise AssertionError("Publication was allocated before the directed repair")
    raise AssertionError("The real D / failed Review / D sequence did not finish")


def test_human_continuation_renders_only_the_latest_original_response(tmp_path: Path) -> None:
    kernel, first, operations, _ = frozen_call(tmp_path)
    blocker = b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Provide the external decision."]}'
    run_prompt_call(kernel, first, tmp_path, blocker)
    assert operations.once().reason == "human_blocker"
    first_response = "Previous human decision.\r\n"
    kernel.control("resume", "human-one", subject=first.subject, message=first_response)
    second = operations.once().preparation
    assert second is not None and second.context["semantic_continuation"] is True
    second_prompt = run_prompt_call(kernel, second, tmp_path, blocker)
    assert json.dumps(first_response, ensure_ascii=False)[1:-1] in second_prompt
    assert "Frozen development method" not in second_prompt and "Frozen repair method" not in second_prompt
    assert operations.once().reason == "human_blocker"
    latest = "Latest human decision: retain exact formatting.\r\n"
    kernel.control("resume", "human-two", subject=first.subject, message=latest)
    third = operations.once().preparation
    assert third is not None and third.attempt_id == first.attempt_id and third.thread_id == first.thread_id
    assert third.context["human_responses"] == [first_response, latest]
    prompt = run_prompt_call(kernel, third, tmp_path,
                             b'{"result_kind":"development","summary":"Applied human decision.","human_blockers":null}')
    assert json.dumps(latest, ensure_ascii=False)[1:-1] in prompt and "Previous human decision" not in prompt
    assert kernel.status(first.subject)["budgets"]["requirements"]["developments_used"] == 1


def test_publication_prompt_consumes_only_the_actual_authorized_evidence(tmp_path: Path) -> None:
    # Other scopes and fallback rendering use the actual allocated inputs in
    # test_kernel_prompt_facts; keep one complete CLI/CI Publication connection.
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("fail", "none"))
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"},
                       github={"worker_source": "pat", "pat": {"env": "SELECTED_WORKER_TOKEN"}, "runner_source": "host"})
    resources = builtin_resources("en")
    resources["methods/publishing"] = "Frozen publishing method\n" + resources["methods/publishing"]
    spec = freeze_creation(scenario.spec, defaults, prompt_resources=resources)
    scenario.kernel = create_kernel(tmp_path / "state" / "kernel.sqlite3", spec, requirements=scenario.requirements)
    scenario.execution = scenario.kernel.claim_executor("prompt-owner")
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    for _ in range(80):
        step = operations.once()
        if step.preparation is None:
            assert step.kind in {"effect", "progress"}, step
            continue
        call = step.preparation
        if call.role == Role.DEVELOPMENT:
            run_prompt_call(scenario.kernel, call, tmp_path,
                            b'{"result_kind":"development","summary":"Implemented the allocated work.","human_blockers":null}', edit=True)
        elif call.role == Role.REVIEW:
            run_prompt_call(scenario.kernel, call, tmp_path,
                            json.dumps(review_result("pass")).encode())
        else:
            raw = b'{"result_kind":"publication","commit_message":"feat: implement allocated work","pr_title":"feat: implement allocated work","pr_body_markdown":"Implemented and validated the allocated work.","human_blockers":null}'
            prompt = run_prompt_call(scenario.kernel, call, tmp_path, raw)
            if "ci_repair_publication_context" not in call.context:
                continue
            field = "ci_repair_publication_context"
            assert isinstance(call.context[field], dict)
            assert all(other == field or other not in call.context for other in (
                "acceptance_artifact", "fallback_publication_context", "ci_repair_publication_context",
            ))
            assert json.dumps(call.context[field], ensure_ascii=False, indent=2, sort_keys=True) in prompt
            assert "Frozen publishing method" in prompt
            assert "_prompt_resources" not in prompt and "SELECTED_WORKER_TOKEN" not in prompt
            assert call.context[field]["current_candidate_has_additional_review"] is False
            return
    raise AssertionError("The actual publication route was not allocated")
