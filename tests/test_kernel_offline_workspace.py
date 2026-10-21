"""The graph transport names unallocated Ticket resources without creating them."""
from pathlib import Path

from agent_run_kernel.model import DeliveryKind
from agent_run_kernel.offline import GraphScenario
from test_kernel_requirements import independent_source


def test_graph_scenario_leaves_ticket_resources_for_the_first_workspace_effect(tmp_path: Path) -> None:
    scenario = GraphScenario(tmp_path / "graph", independent_source())
    refs = scenario.port.git.run("for-each-ref", "--format=%(refname)").stdout.splitlines()
    assert refs == ["refs/heads/main", "refs/heads/run"]
    assert scenario.port.remote_refs() == {ref: scenario.initial.sha for ref in refs}
    tickets = [delivery for delivery in scenario.spec.deliveries if delivery.kind == DeliveryKind.TICKET]
    assert len(tickets) == 2
    for delivery in tickets:
        assert delivery.checkout and not Path(delivery.checkout).exists()
        assert delivery.source_ref not in refs
        assert delivery.base == scenario.initial
    registry = scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    assert [field for field in registry if field.startswith("worktree ")] == [f"worktree {scenario.port.git.root}"]
    assert scenario.port.writes() == []
