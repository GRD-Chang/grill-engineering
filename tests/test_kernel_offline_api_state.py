"""Raw state transitions must satisfy the real retirement/recovery protocol."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind
from support.offline import Scenario
from support.offline_api import ExternalObjects, SyntheticGitHub


def test_retirement_and_recovery_use_persisted_external_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subtests: pytest.Subtests,
) -> None:
    for kind, operation in ((DeliveryKind.PARENT_ONLY, 'retire_pr'), (DeliveryKind.TICKET, 'reopen_ticket')):
        with subtests.test(operation=operation):
            scenario = Scenario(tmp_path / operation, kind)
            kernel, subject = scenario.kernel, scenario.delivery.key
            assert scenario.run().kind == ('approval' if kind == DeliveryKind.PARENT_ONLY else 'complete')
            prior_pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
            assert prior_pr is not None
            if operation == 'retire_pr':
                scenario.requirements = replace(scenario.requirements,
                    parent=replace(scenario.requirements.parent, body='Clarified delivery requirement.'))
                world = scenario.world()
                replacement = replace(scenario.delivery, source_ref=scenario.delivery.source_ref + '-g2',
                    checkout=scenario.delivery.checkout + '-g2', base=world.target)
                assert kernel.control('requeue', 'replace-original', subject=subject,
                                      replacement=replacement, world=world) == 'pending'
            else:
                assert scenario.port.issue_state(scenario.delivery.issue) == 'CLOSED'
                assert kernel.control('abandon', 'recover-original',
                    resource_inspections=scenario.port.inspect_resources(kernel.generation_resources())) == 'pending'
            observed = []
            execute = scenario.port.execute

            def record(effect, authorize):
                result = execute(effect, authorize)
                if effect.operation == operation:
                    observed.append((effect, result))
                return result

            with monkeypatch.context() as patch:
                patch.setattr(scenario.port, 'execute', record)
                for _ in range(30):
                    scenario.step()
                    if observed:
                        break
            assert len(observed) == 1, 'The normal runner did not reach the physical operation'
            effect, result = observed[0]
            assert result.disposition == 'target'
            objects = ExternalObjects(scenario.port.database)
            if operation == 'retire_pr':
                pr = objects.pr(prior_pr.number)
                assert pr is not None and pr['state'] == 'closed' and pr['merged'] is False
                events = objects.events(prior_pr.number)
                assert len(events) == 1 and events[0]['event'] == 'closed'
                assert events[0]['actor']['login'] == 'offline-publisher'
                assert result.pr is not None and result.pr.state == 'CLOSED'
            else:
                api = SyntheticGitHub(scenario.port.git, scenario.port.database, effect.repository,
                    physical=scenario.port.physical, written=lambda *args: None, read_faults=set())
                issue = api.object(f'repos/{effect.repository}/issues/{scenario.delivery.issue}')
                events = objects.events(scenario.delivery.issue)
                assert [event['event'] for event in events] == ['closed', 'reopened']
                assert issue['state'] == 'open'
                assert issue['updated_at'] == events[-1]['created_at'] == result.raw['updated_at']
                assert result.raw['reopened_event']['id'] == events[-1]['id']
                before = objects.snapshot()
                assert scenario.port.reconcile(effect).disposition == 'target'
                assert ExternalObjects(scenario.port.database).snapshot() == before
