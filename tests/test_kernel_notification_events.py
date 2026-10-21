"""Pure native event projection uses real committed Core producers."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run_kernel import Issue, Kernel, ParentIssue, Requirements, Step
from agent_run_kernel.notification_events import events
from agent_run_kernel.offline import GraphScenario


def _project(kernel: Kernel, mode: str) -> list[dict[str, Any]]:
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
                 "language": run["language"], "created_at": run["created_at"]}
        frozen = deepcopy(state)
        result = [event for row in tx.all("SELECT * FROM history ORDER BY position")
                  for event in events(tx, dict(row), state, mode)]
        assert state == frozen
        return result


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_role_events_require_real_start_and_consumption_and_do_not_duplicate_first_start(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    original_handle = scenario.handle

    def handle(step: Step) -> Step:
        if step.kind != "agent" or step.call is None:
            return original_handle(step)
        # The offline Agent normally provides only a result. This projection
        # test also supplies explicit synthetic process observations through
        # the real Core producers. It does not claim a real Worker subprocess.
        call = step.call
        process = {"pid": 21, "pgid": 21, "start_token": "synthetic-process",
                   "host_pid": 10, "host_start_token": "synthetic-host"}
        assert scenario.kernel.dispatch_agent(call)
        scenario.kernel.agent_started(call, process)
        scenario.kernel.receive(call, scenario.agent(call))
        scenario.kernel.agent_exited(call, process, 0)
        return step

    monkeypatch.setattr(scenario, "handle", handle)
    consumed: list[str] = []
    for _ in range(100):
        step = scenario.step()
        if step.kind == "agent":
            # The explicit synthetic Agent seam recorded dispatch/start/raw/exit.
            # The next real Core advance has not consumed the result.
            assert step.call is not None
            before = _project(scenario.kernel, mode)
            assert sum(event["kind"] == "ticket_started" for event in before) == 1
            assert not any(event["kind"] == "stage_start" and event.get("role") == "development" for event in before)
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id for event in before)
            assert scenario.step().kind == "progress"
            after = _project(scenario.kernel, mode)
            ended = [event for event in after if event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id]
            assert len(ended) == (1 if mode == "detailed" else 0)
            if ended:
                assert ended[0]["role"] == step.call.role.value
                consumed.append(ended[0]["role"])
                if step.call.role.value == "publication":
                    assert ended[0]["status_code"] == "publication_artifact"
                    assert not any(event["kind"] == "pr_created" for event in after)
        elif step.kind == "complete":
            break
        else:
            assert step.kind not in {"paused", "waiting"}, step
    else:
        raise AssertionError("The real Ticket pipeline did not finish")
    projected = _project(scenario.kernel, mode)
    assert sum(event["kind"] == "ticket_started" for event in projected) == 1
    assert sum(event["kind"] == "ticket_completed" for event in projected) == 1
    assert sum(event["kind"] == "pr_created" for event in projected) == (1 if mode == "detailed" else 0)
    assert consumed == (["development", "review", "publication"] if mode == "detailed" else [])
    assert [event["role"] for event in projected if event["kind"] == "stage_start"] == (
        ["review", "publication"] if mode == "detailed" else [])
    assert _project(scenario.kernel, mode) == projected
