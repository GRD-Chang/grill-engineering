"""Finite native Requirements scenarios; each call reads fresh fixture data."""
from support.repository_fixture import create_kernel
from dataclasses import replace
import json
from pathlib import Path

from agent_run.core.kernel import Kernel
from agent_run.model import Commit, Delivery, DeliveryKind, Policy, Requirements, RunSpec


def native_input() -> dict:
    return json.loads((Path(__file__).parent.parent / "data/kernel/graph-refresh-v1.json").read_text())


def source_run(path: Path, requirements: Requirements | None = None) -> tuple[Kernel, Requirements]:
    source = requirements or Requirements.from_wire(native_input()["initial_requirements"])
    base = Commit("a" * 40, "b" * 40)
    deliveries = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "reader-untrusted-revision", base,
                                f"refs/heads/ticket-{number}", "refs/heads/run") for number in source.parent.sub_issue_numbers)
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "reader-untrusted-revision", base, "refs/heads/run", "refs/heads/main")
    spec = RunSpec("native-run", "example/project", 1, "untrusted-parent", "untrusted-graph", (*deliveries, final), Policy(concurrency=2))
    return create_kernel(path, spec, requirements=source), source


def independent_source() -> Requirements:
    source = Requirements.from_wire(native_input()["initial_requirements"])
    return replace(source, issues=tuple(replace(issue, blocked_by=()) for issue in source.issues))
