"""Synthetic external facts at the native runner's physical adapter seam."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from agent_run.contracts.write_authority import FinalAdmission, WriteAuthority

from agent_run.model import DeliveryKind, Effect
from agent_run.errors import KernelError
from support.offline import Scenario
from agent_run.core.operation_contract import DeliveryRead, OperationBinding, OperationPorts, ReadUnavailable


def bound_ports(scenario: Scenario) -> OperationPorts:
    # Test-owned explicit creation input, representing Core's frozen binding.
    assert scenario.spec.inputs is not None
    return scenario.make_ports(OperationBinding(scenario.port.git.root, scenario.spec.repository,
        scenario.spec.parent_issue, scenario.spec.inputs))


@pytest.mark.parametrize('kind', [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_offline_creation_has_real_source_before_the_runner_starts(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = Scenario(tmp_path / 'delivery', kind)
    source = bound_ports(scenario).source(1)
    assert source.parent.number == 1 and source.parent.title == 'Parent spec'
    if kind == DeliveryKind.TICKET:
        assert source.parent.sub_issue_numbers == (2,)
        assert [(issue.number, issue.state, issue.labels) for issue in source.issues] == [
            (2, 'OPEN', frozenset({'ready-for-agent'}))]
        assert scenario.kernel.source_status()['frontier'] == ['ticket:2']
        assert scenario.kernel.source_status()['final_subject'] is None
    else:
        assert source.parent.sub_issue_numbers == () and source.issues == ()
    assert scenario.kernel.status(scenario.delivery.key)['pending_attempt'] is None
    assert scenario.kernel.source_status()['observed_parent_revision'].startswith('sha256:')


def test_neutral_input_remains_the_actual_requirement_source(tmp_path: Path) -> None:
    script = json.loads((Path(__file__).parent / 'data/kernel/normal-ticket-v1.json').read_text())
    scenario = Scenario(tmp_path / 'neutral', DeliveryKind.TICKET, neutral_script=script)
    source = bound_ports(scenario).source(1)
    assert source.parent.title == 'Parent spec'
    assert source.parent.sub_issue_numbers == (3,)
    assert source.issues[0].title == 'Deliver one ticket'
    assert source.issues[0].body == 'Implement the requested behavior.\n\n## Acceptance criteria\n\n- It works.'
    assert source.issues[0].labels == frozenset({'ready-for-agent'})
    scenario.requirements = replace(source, parent=replace(source.parent, body='Independent source edit.'))
    assert bound_ports(scenario).source(1).parent.body == 'Independent source edit.'


def test_delivery_adapter_reads_real_refs_and_scripted_checks_without_world_versions(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'physical', DeliveryKind.TICKET, checks=('fail', 'pass'))
    port = scenario.port
    base = port.remote_commit(scenario.delivery.target_ref)
    assert base is not None
    first = port.external_remote_append(scenario.delivery.target_ref, {'first.txt': 'first\n'}, 'first')
    effect = Effect('external-pr', 'ticket:2', 'pr_write', scenario.spec.repository,
        scenario.delivery.target_ref, 'refs/heads/main', base.sha, first.sha, first, base,
        None, 'offline-publisher', {'title': 'Independent PR', 'body': 'Observed external body.'}, scenario.execution)
    prepared, authorized = [], []
    def prepare() -> FinalAdmission:
        prepared.append(True)
        return lambda: authorized.append(True)
    acknowledgement = port.execute(effect, WriteAuthority(prepare))
    # Preparation grants nothing; final admission follows exact readback before the single POST.
    assert prepared == [True]
    assert authorized == [True] and acknowledgement is not None
    assert sum(row['operation'] == 'pr_write' for row in port.writes()) == 1
    assert acknowledgement.pr is not None
    request = DeliveryRead(effect.source_ref, effect.target_ref, acknowledgement.pr.number, (base.sha,))
    operations = bound_ports(scenario)
    observed = operations.delivery(request)
    assert observed.source == first and observed.target == base
    assert observed.checks is not None and observed.checks.result == 'fail'
    assert observed.publisher_identity == {'source': 'host'}
    assert (base.sha, first.sha) in observed.ancestry
    with pytest.raises(ReadUnavailable, match='checks head'):
        operations.delivery(replace(request, check_head=base.sha))
    second = port.external_remote_append(effect.source_ref, {'second.txt': 'second\n'}, 'second')
    observed = operations.delivery(request)
    assert observed.source == second
    assert observed.checks is not None and observed.checks.result == 'pass'


def test_physical_ports_refuse_another_workspace_binding(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'binding', DeliveryKind.PARENT_ONLY)
    assert scenario.spec.inputs is not None
    with pytest.raises(KernelError, match='synthetic_source_binding'):
        scenario.make_ports(OperationBinding(tmp_path / 'another', scenario.spec.repository, 1, scenario.spec.inputs))
