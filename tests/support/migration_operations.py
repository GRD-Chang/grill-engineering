"""Physical source setup and bounded driving for imported native Runs.

The caller supplies unchanged old GitHub facts. Nothing here decodes a Run,
selects a Ticket, edits native records, or generates an oracle expectation.
"""
from typing import Any, Mapping

from agent_run_kernel import Requirements
from agent_run_kernel.offline import Scenario
from support.core_operations import CoreOperations


def configure_source(scenario: Scenario, external: Mapping[str, Any] | Requirements) -> None:
    scenario.requirements = external if isinstance(external, Requirements) else Requirements.from_wire(external)
    scenario.check_heads = {}


def admit_final(scenario: Scenario) -> str:
    if scenario.kernel.source_status()['final_subject'] is None:
        operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
        operations.until(lambda step: scenario.kernel.source_status()['final_subject'] is not None,
                         agent_capacity=0)
    subject = scenario.kernel.source_status()['final_subject']
    assert isinstance(subject, str)
    return subject
