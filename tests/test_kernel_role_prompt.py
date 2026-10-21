"""Frozen request rendering reaches a real CLI without rereading personal input."""

import json
import os
from pathlib import Path
import sys

import pytest

from agent_run.prompt_resources import builtin_resources, personal_method_directory
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import AgentCall, Commit, Delivery, DeliveryKind, Kernel, KernelError, Role, RunSpec, World
from agent_run_kernel.codex_execution import execute_codex
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.offline import Scenario, review_result
from agent_run_kernel.role_prompt import render_role_prompt


def frozen_call(tmp_path: Path) -> tuple[Kernel, AgentCall, World, UserDefaultsStore]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    base = Commit("base", "tree")
    world = World("parent-v1", "graph-v1", "ticket-v1", base)
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
    kernel = Kernel.create(tmp_path / "state" / "kernel.sqlite3", spec)
    call = kernel.advance(delivery.key, kernel.claim_executor("prompt-owner"), world).call
    assert call is not None
    return kernel, call, world, defaults


def test_frozen_development_body_reaches_cli_and_repair_selects_only_its_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "personal"))
    kernel, call, world, defaults = frozen_call(tmp_path)
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
assert sys.stdin.read() == {prompt!r}
print(json.dumps({{'type': 'thread.started', 'thread_id': 'prompt-thread'}}), flush=True)
""")
    executable.chmod(0o700)
    credential = tmp_path / "hosts.yml"
    credential.write_text("github.com:\n  oauth_token: fixture-only\n")
    credential.chmod(0o600)
    result = execute_codex(kernel, call, executable=str(executable), prompt=prompt,
                           environment={"PATH": os.defpath, "HOME": str(tmp_path)}, credential_file=credential,
                           hidden_paths=(), authorize_source=lambda: None, timeout=5)
    assert result is not None and result.raw == b"" and result.returncode == 0
    repair = kernel.advance(call.subject, call.execution, world).call
    assert repair is not None and repair.mode == "output-repair"
    repaired_prompt = render_role_prompt(repair)
    assert repair.context["contract_error"] in repaired_prompt
    assert "Frozen development method" not in repaired_prompt
    assert "Unrelated acceptance method must not leak" not in repaired_prompt
    assert repair.invocation_id == call.invocation_id and repair.deadline_at == call.deadline_at
    assert call.context["_prompt_resources"] == repair.context["_prompt_resources"]


def test_call_without_frozen_resources_is_rejected_before_dispatch(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket", base, "source", "target")
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", RunSpec("run", "example/repository", 1, "parent", "graph", (delivery,)))
    call = kernel.advance("ticket", kernel.claim_executor("owner"), World("parent", "graph", "ticket", base)).call
    assert call is not None
    with pytest.raises(KernelError, match="prompt_resources_unavailable"):
        render_role_prompt(call)
    with kernel.storage.transaction(write=False) as tx:
        output = tx.one("SELECT dispatch_state,process_identity FROM invocation_outputs WHERE invocation=?", (call.invocation_id,))
        assert tuple(output) == ("allocated", None)


def run_prompt_call(kernel: Kernel, call: AgentCall, tmp_path: Path, raw: bytes, *, edit: bool = False) -> str:
    prompt = render_role_prompt(call)
    executable = tmp_path / "prompt-codex"
    executable.write_text(f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
assert sys.stdin.read() == {prompt!r}
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
                           hidden_paths=(), authorize_source=lambda: None, timeout=5)
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
    scenario.kernel = Kernel.create(tmp_path / "state" / "kernel.sqlite3", spec)
    scenario.execution = scenario.kernel.claim_executor("prompt-owner")
    first = None
    reviewed = False
    for _ in range(30):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.call is None:
            assert step.kind in {"effect", "progress"}, step
            scenario.handle(step)
            continue
        call = step.call
        if call.role == Role.REVIEW:
            assert first is not None and not reviewed
            prompt = run_prompt_call(scenario.kernel, call, tmp_path, json.dumps(review_result("fail")).encode())
            context = call.context["review_context"]
            assert context["full_range"]["candidate_sha"] in prompt
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
    kernel, first, world, _ = frozen_call(tmp_path)
    blocker = b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Provide the external decision."]}'
    run_prompt_call(kernel, first, tmp_path, blocker)
    assert kernel.advance(first.subject, first.execution, world).reason == "human_blocker"
    first_response = "Previous human decision.\r\n"
    kernel.control("resume", "human-one", subject=first.subject, message=first_response)
    second = kernel.advance(first.subject, first.execution, world).call
    assert second is not None and second.context["semantic_continuation"] is True
    second_prompt = run_prompt_call(kernel, second, tmp_path, blocker)
    assert json.dumps(first_response, ensure_ascii=False)[1:-1] in second_prompt
    assert "Frozen development method" not in second_prompt and "Frozen repair method" not in second_prompt
    assert kernel.advance(first.subject, first.execution, world).reason == "human_blocker"
    latest = "Latest human decision: retain exact formatting.\r\n"
    kernel.control("resume", "human-two", subject=first.subject, message=latest)
    third = kernel.advance(first.subject, first.execution, world).call
    assert third is not None and third.attempt_id == first.attempt_id and third.thread_id == first.thread_id
    assert third.context["human_responses"] == [first_response, latest]
    prompt = run_prompt_call(kernel, third, tmp_path,
                             b'{"result_kind":"development","summary":"Applied human decision.","human_blockers":null}')
    assert json.dumps(latest, ensure_ascii=False)[1:-1] in prompt and "Previous human decision" not in prompt
    assert kernel.status(first.subject)["budgets"]["requirements"]["developments_used"] == 1


@pytest.mark.parametrize("kind,route", [
    (DeliveryKind.TICKET, "accepted"), (DeliveryKind.PARENT_ONLY, "accepted"),
    (DeliveryKind.FINAL_RUN, "accepted"), (DeliveryKind.TICKET, "fallback"), (DeliveryKind.TICKET, "ci"),
])
def test_publication_prompt_consumes_only_the_actual_authorized_evidence(
    tmp_path: Path, kind: DeliveryKind, route: str,
) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, checks=("fail", "none") if route == "ci" else ("none",))
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"},
                       policy={"ticket_review_rounds": 1} if route == "fallback" else {},
                       github={"worker_source": "pat", "pat": {"env": "SELECTED_WORKER_TOKEN"}, "runner_source": "host"})
    resources = builtin_resources("en")
    resources["methods/publishing"] = "Frozen publishing method\n" + resources["methods/publishing"]
    spec = freeze_creation(scenario.spec, defaults, prompt_resources=resources)
    scenario.kernel = Kernel.create(tmp_path / "state" / "kernel.sqlite3", spec)
    scenario.execution = scenario.kernel.claim_executor("prompt-owner")
    for _ in range(80):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.call is None:
            assert step.kind in {"effect", "progress"}, step
            scenario.handle(step)
            continue
        call = step.call
        if call.role == Role.DEVELOPMENT:
            run_prompt_call(scenario.kernel, call, tmp_path,
                            b'{"result_kind":"development","summary":"Implemented the allocated work.","human_blockers":null}', edit=True)
        elif call.role == Role.REVIEW:
            run_prompt_call(scenario.kernel, call, tmp_path,
                            json.dumps(review_result("fail" if route == "fallback" else "pass")).encode())
        else:
            raw = b'{"result_kind":"publication","commit_message":"feat: implement allocated work","pr_title":"feat: implement allocated work","pr_body_markdown":"Implemented and validated the allocated work.","human_blockers":null}'
            prompt = run_prompt_call(scenario.kernel, call, tmp_path, raw)
            if route == "ci" and "ci_repair_publication_context" not in call.context:
                continue
            field = {"accepted": "acceptance_artifact", "fallback": "fallback_publication_context",
                     "ci": "ci_repair_publication_context"}[route]
            assert isinstance(call.context[field], dict)
            assert all(other == field or other not in call.context for other in (
                "acceptance_artifact", "fallback_publication_context", "ci_repair_publication_context",
            ))
            assert json.dumps(call.context[field], ensure_ascii=False, indent=2, sort_keys=True) in prompt
            assert "Frozen publishing method" in prompt
            assert "_prompt_resources" not in prompt and "SELECTED_WORKER_TOKEN" not in prompt
            if route != "accepted":
                assert call.context[field]["current_candidate_has_additional_review"] is False
            return
    raise AssertionError("The actual publication route was not allocated")
