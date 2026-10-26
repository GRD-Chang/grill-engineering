"""Publication freezes two output ranges without changing the Review boundary."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from agent_run.cli.main import main
from agent_run.config.resources import builtin_resources
from agent_run.contracts.artifact import role_schema
from agent_run.core.kernel import Kernel
from agent_run.core.prompts.role import render_role_prompt
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Role
from support.core_operations import final_scenario
from support.offline import Scenario, fixture_inputs


@pytest.mark.parametrize("kind,language", [
    (DeliveryKind.TICKET, "zh"), (DeliveryKind.PARENT_ONLY, "en"), (DeliveryKind.FINAL_RUN, "zh"),
])
def test_publication_ranges_reach_runtime_and_public_preview_from_real_work(tmp_path, monkeypatch, capsys, language, kind):
    inputs = replace(fixture_inputs(), prompt_resources=builtin_resources(language))
    if kind == DeliveryKind.FINAL_RUN:
        scenario = final_scenario(tmp_path / "final", inputs=inputs, language=language)
    else:
        scenario = Scenario(tmp_path / "delivery", kind, checks=("fail", "fail", "none") if kind == DeliveryKind.TICKET else ("fail", "none"))
        spec = replace(scenario.spec, inputs=inputs, language=language)
        scenario.kernel = Kernel.create(tmp_path / "configured.sqlite3", spec, requirements=scenario.requirements)
        scenario.execution = scenario.kernel.claim_executor("publication-scope-owner")
    original = scenario.agent
    publications = []

    def agent(call):
        if call.role == Role.PUBLICATION:
            with scenario.kernel.storage.transaction(write=False) as tx:
                job = tx.one("SELECT * FROM subjects WHERE id=?", (call.subject,))
                target = scenario.port.remote_commit(job["target_ref"])
                expected = {
                    "commit_range": None if kind == DeliveryKind.FINAL_RUN else {
                        "base_sha": job["source_sha"], "head_sha": job["integrated_sha"] or job["candidate_sha"]},
                    "pr_range": {"base_sha": target.sha, "head_sha": call.context["reviewed_commit"]},
                    "existing_pr": None,
                }
                observed = scenario.port.pull_request_by_ref(job["source_ref"], job["target_ref"])
                if observed is not None:
                    expected["existing_pr"] = {**asdict(observed),
                        "url": f"https://github.com/{observed.repository}/pull/{observed.number}"}
                assert call.context["publication_context"] == expected
                saved = json.loads(tx.get(tx.one("SELECT context_id FROM attempts WHERE id=?", (call.attempt_id,))[0], call.subject))
                assert saved["publication_context"] == expected
            prompt = render_role_prompt(call)
            visible = json.dumps({"publication_context": expected}, ensure_ascii=False, indent=2, sort_keys=True)
            assert visible in prompt
            facts = {field: call.context[field] for field in ("acceptance_identity", "ticket_completions", "ci_evidence")
                     if field in call.context}
            if facts:
                assert json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) in prompt
            assert ("整个 PR" if language == "zh" else "entire PR") in prompt
            if kind == DeliveryKind.FINAL_RUN:
                assert ("不用于创建提交" if language == "zh" else "not used to create a commit") in prompt
            else:
                assert ("本次提交" if language == "zh" else "this commit") in prompt
            request = tmp_path / "publication-request.json"
            request.write_text(json.dumps({**call.context, "thread_id": call.reported_thread,
                "_invocation_mode": "new-thread" if call.mode == "fresh" else "new-attempt"}))
            role = "final-publication" if kind == DeliveryKind.FINAL_RUN else "publication"
            assert main(["prompts", "preview", "--role", role, "--request", str(request), "--json"]) == 0
            preview = json.loads(capsys.readouterr().out)
            assert preview["prompt"] == prompt and preview["schema"] == role_schema("publication")
            if not publications:
                missing = dict(call.context)
                missing.pop("publication_context")
                with pytest.raises(KernelError, match="publication_scope_unavailable"):
                    render_role_prompt(replace(call, context=missing))
                broken = deepcopy(call.context)
                broken["publication_context"]["pr_range"] = None
                with pytest.raises(ValueError, match="Publication scope"):
                    render_role_prompt(replace(call, context=broken))
                wrong_kind = deepcopy(call.context)
                wrong_kind["publication_context"]["commit_range"] = (
                    dict(expected["pr_range"]) if kind == DeliveryKind.FINAL_RUN else None)
                with pytest.raises(ValueError, match="Publication scope"):
                    render_role_prompt(replace(call, context=wrong_kind))
                for field in ("commit_range", "pr_range"):
                    if expected[field] is not None:
                        bad = deepcopy(call.context)
                        bad["publication_context"][field]["head_sha"] = "different-work"
                        with pytest.raises(ValueError, match="Publication scope"):
                            render_role_prompt(replace(call, context=bad))
            publications.append((call, expected))
        return original(call)

    monkeypatch.setattr(scenario, "agent", agent)
    assert scenario.run().kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    assert len(publications) == ({DeliveryKind.TICKET: 3, DeliveryKind.PARENT_ONLY: 2, DeliveryKind.FINAL_RUN: 1}[kind])
    assert publications[0][1]["existing_pr"] is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        rows = tx.rows("SELECT payload_id,receipt_id FROM effects WHERE subject=? AND operation='publication_commit' ORDER BY rowid",
                       (scenario.delivery.key,))
        payloads = [json.loads(tx.get(row[0], scenario.delivery.key)) for row in rows]
        published = [json.loads(tx.get(row[1], scenario.delivery.key))["commit"] for row in rows]
        assert len(published) == len(publications)
        for index, (call, scope) in enumerate(publications):
            saved = json.loads(tx.get(tx.one("SELECT context_id FROM attempts WHERE id=?", (call.attempt_id,))[0], call.subject))
            assert saved["publication_context"] == scope
            assert published[index]["tree"] == scenario.port.git.commit(scope["pr_range"]["head_sha"]).tree
            if scope["commit_range"] is None:
                assert payloads[index]["reuse_sha"] == scope["pr_range"]["head_sha"] == published[index]["sha"]
            else:
                assert scenario.port.git.parents(published[index]["sha"])[0] == scope["commit_range"]["base_sha"]
    for index, ((_, prior), (_, scope)) in enumerate(zip(publications, publications[1:])):
        assert scope["commit_range"]["base_sha"] == published[index]["sha"]
        assert scope["pr_range"]["base_sha"] == prior["pr_range"]["base_sha"]
        assert scope["commit_range"] != scope["pr_range"]
        assert scope["existing_pr"]["head"]["sha"] == published[index]["sha"]
        assert scope["existing_pr"]["body"] and scope["existing_pr"]["title"]
