"""Notification query commands select their exact Run, never a numeric Parent."""
import json
import shlex

import pytest

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.cli.main import main
from agent_run.core.kernel import Kernel
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.notifications.event_projection import completion_context, event_state
from agent_run.queries.read_facts import read_boundaries
from support.offline import fixture_inputs


@pytest.mark.parametrize("run_id", ["parent-one", "a name; keep literal", "--literal-run"])
def test_completion_query_reaches_exact_run_among_numeric_and_other_parent_tasks(tmp_path, capsys, run_id):
    workspace = ManagedWorkspace.for_repository("example/project")
    workspace.repository_root.mkdir(parents=True)

    def create(identity, parent):
        delivery = Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "requirement",
            Commit("base", "tree"), "refs/heads/source", "refs/heads/main")
        spec = RunSpec(identity, workspace.repository, parent, "parent", "graph", (delivery,),
                       repository_root=str(workspace.repository_root), language="zh", inputs=fixture_inputs())
        return Kernel.create_task(workspace.state_root / identity / "kernel.sqlite3", spec,
                                  requirements=Requirements(ParentIssue(parent, "Parent", "Scope"), ()))

    wanted = create(run_id, 1)
    other = create("1", 7)
    with wanted.storage.transaction(write=False) as tx:
        event = completion_context(read_boundaries(tx, 0.0), event_state(tx))
    command = shlex.split(event["status_query"])
    assert command[0:2] == ["agent-run", "status"]
    # --json is inserted before the literal selector delimiter, if present.
    assert main([command[1], "--json", *command[2:]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == run_id
    assert result["parent"]["number"] == 1
    assert result["repository"] == workspace.repository
    assert other.storage.path != wanted.storage.path
